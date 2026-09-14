"""MQTT: discovery sensor for the digest (retained) and the ingest/command topics."""
import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

DISCOVERY_TOPIC = "homeassistant/sensor/ai_memory/digest/config"
STATE_TOPIC = "ai_memory/digest/state"
ATTR_TOPIC = "ai_memory/digest/attributes"
AVAIL_TOPIC = "ai_memory/availability"
INGEST_TOPIC = "ai_memory/ingest"
COMMAND_TOPIC = "ai_memory/command"


class MqttBridge:
    def __init__(self, cfg: dict, loop: asyncio.AbstractEventLoop,
                 on_ingest: Callable[[dict], Awaitable[dict]],
                 on_connected: Optional[Callable[[], None]] = None,
                 on_command: Optional[Callable[[dict], Awaitable[None]]] = None,
                 assistant_name: str = "Assistant"):
        self.cfg = cfg
        self.loop = loop
        self.on_ingest = on_ingest
        self.on_connected = on_connected
        self.on_command = on_command
        self.assistant_name = assistant_name
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ai_memory")
        if cfg.get("username"):
            self.client.username_pw_set(cfg["username"], cfg.get("password") or "")
        self.client.will_set(AVAIL_TOPIC, "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=2, max_delay=60)
        self.connected = False

    @property
    def configured(self) -> bool:
        return bool(self.cfg.get("host"))

    def start(self) -> None:
        if not self.configured:
            log.warning("MQTT not configured; digest sensor will not be published")
            return
        host, port = self.cfg["host"], int(self.cfg.get("port") or 1883)
        log.info("Connecting to MQTT %s:%s as %s", host, port, self.cfg.get("username") or "(anon)")
        self.client.connect_async(host, port, keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            self.client.publish(AVAIL_TOPIC, "offline", retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ callbacks
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error("MQTT connect failed: %s", reason_code)
            return
        self.connected = True
        log.info("MQTT connected")
        client.subscribe([(INGEST_TOPIC, 1), (COMMAND_TOPIC, 1)])
        self.publish_discovery()
        client.publish(AVAIL_TOPIC, "online", retain=True)
        if self.on_connected:
            try:
                self.on_connected()
            except Exception as e:  # noqa: BLE001
                log.warning("on_connected failed: %s", e)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        log.warning("MQTT disconnected (%s); will reconnect", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"text": msg.payload.decode("utf-8", errors="replace")}
        if msg.topic == INGEST_TOPIC:
            fut = asyncio.run_coroutine_threadsafe(self.on_ingest(payload), self.loop)
        elif msg.topic == COMMAND_TOPIC and self.on_command:
            fut = asyncio.run_coroutine_threadsafe(self.on_command(payload), self.loop)
        else:
            return
        fut.add_done_callback(self._log_result)

    @staticmethod
    def _log_result(fut):
        try:
            fut.result()
        except Exception as e:  # noqa: BLE001
            log.error("MQTT message handling failed: %s", e)

    # ------------------------------------------------------------ publish
    def publish_discovery(self) -> None:
        payload = {
            "name": "Digest",
            "unique_id": "ai_memory_digest",
            "object_id": "ai_memory_digest",
            "state_topic": STATE_TOPIC,
            "json_attributes_topic": ATTR_TOPIC,
            "availability_topic": AVAIL_TOPIC,
            "icon": "mdi:notebook-outline",
            "device": {
                "identifiers": ["ai_memory"],
                "name": "AI Memory",
                "manufacturer": "AI Memory",
                "model": "Home Assistant add-on",
            },
        }
        self.client.publish(DISCOVERY_TOPIC, json.dumps(payload), retain=True)

    def publish_digest(self, d: dict) -> None:
        if not self.configured:
            return
        attrs = {
            "prompt_block": d["prompt_block"],
            "today_day": d["today_day"],
            "yesterday_day": d["yesterday_day"],
            "entries_today": d["entries_today"],
            "pending_candidates": d["pending_candidates"],
            "updated": d["updated"],
        }
        self.client.publish(ATTR_TOPIC, json.dumps(attrs, ensure_ascii=False), retain=True)
        self.client.publish(STATE_TOPIC, d["updated"], retain=True)
