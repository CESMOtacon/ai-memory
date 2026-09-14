"""Settings: add-on options, Supervisor-provided MQTT credentials, timezone."""
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict

import httpx

log = logging.getLogger(__name__)

OPTIONS_PATH = os.environ.get("AIMEM_OPTIONS", "/data/options.json")
# Persistent notebook lives in the add-on's config folder (/addon_configs/<slug> on the host),
# so it is visible, editable and included in backups. /data is the pre-0.2 location.
DATA_DIR = os.environ.get("AIMEM_DATA_DIR", "/config")
LEGACY_DATA_DIR = "/data"
SUPERVISOR = "http://supervisor"


@dataclass
class Settings:
    assistant_name: str = "Assistant"
    user_name: str = "the user"
    summarizer_backend: str = "ai_task"  # ai_task | anthropic | off
    ai_task_entity: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    day_boundary_hour: int = 5
    forget_grace_days: int = 7
    timezone: str = "UTC"
    log_level: str = "info"
    data_dir: str = DATA_DIR
    supervisor_token: str = ""
    mqtt: Dict[str, object] = field(default_factory=dict)  # host, port, username, password


def _load_options() -> dict:
    try:
        with open(OPTIONS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


async def _supervisor_get(path: str, token: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(SUPERVISOR + path, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()


async def load_settings() -> Settings:
    o = _load_options()
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    s = Settings(
        assistant_name=(o.get("assistant_name") or "Assistant").strip(),
        user_name=(o.get("user_name") or "the user").strip(),
        summarizer_backend=(o.get("summarizer_backend") or "ai_task").strip(),
        ai_task_entity=(o.get("ai_task_entity") or "").strip(),
        anthropic_api_key=(o.get("anthropic_api_key") or "").strip(),
        anthropic_model=o.get("anthropic_model") or "claude-opus-5",
        day_boundary_hour=int(o.get("day_boundary_hour", 5)),
        forget_grace_days=int(o.get("forget_grace_days", 7)),
        timezone=(o.get("timezone") or "").strip(),
        log_level=o.get("log_level") or "info",
        data_dir=DATA_DIR,
        supervisor_token=token,
    )
    if token:
        if not s.timezone:
            try:
                s.timezone = (await _supervisor_get("/core/api/config", token)).get("time_zone", "")
            except Exception as e:  # noqa: BLE001
                log.warning("Could not read HA timezone from Supervisor: %s", e)
        try:
            s.mqtt = (await _supervisor_get("/services/mqtt", token))["data"]
        except Exception as e:  # noqa: BLE001
            log.warning("Could not read MQTT service from Supervisor: %s", e)
    # Env overrides (local development)
    if os.environ.get("AIMEM_MQTT_HOST"):
        s.mqtt = {
            "host": os.environ["AIMEM_MQTT_HOST"],
            "port": int(os.environ.get("AIMEM_MQTT_PORT", "1883")),
            "username": os.environ.get("AIMEM_MQTT_USER", ""),
            "password": os.environ.get("AIMEM_MQTT_PASS", ""),
        }
    if not s.timezone:
        s.timezone = os.environ.get("TZ", "UTC")
    if os.environ.get("ANTHROPIC_API_KEY") and not s.anthropic_api_key:
        s.anthropic_api_key = os.environ["ANTHROPIC_API_KEY"]
    return s
