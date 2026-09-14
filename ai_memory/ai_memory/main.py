"""Entry point: wires settings, store, MQTT, HTTP/MCP server and the scheduler."""
import asyncio
import logging
import os
import shutil
from datetime import timezone

import uvicorn

from .config import LEGACY_DATA_DIR, load_settings
from .context import AppContext
from .mqtt_bridge import MqttBridge
from .scheduler import Scheduler
from .server import build_app
from .store import Store
from .summarizer import Summarizer

log = logging.getLogger("ai_memory")


def _tz(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        log.warning("Unknown timezone %r, using UTC", name)
        return timezone.utc


def _migrate_legacy(data_dir: str) -> None:
    """Pre-0.2 versions kept the notebook in /data. Copy it once into the config folder."""
    if os.path.isdir(os.path.join(data_dir, "days")) or not os.path.isdir(os.path.join(LEGACY_DATA_DIR, "days")):
        return
    log.info("Migrating notebook from %s to %s", LEGACY_DATA_DIR, data_dir)
    for name in ("days", "summaries", "rolling", "loops.json", "facts.json", "candidates.json"):
        src = os.path.join(LEGACY_DATA_DIR, name)
        dst = os.path.join(data_dir, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif os.path.isfile(src):
            shutil.copy2(src, dst)


async def main() -> None:
    settings = await load_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    os.makedirs(settings.data_dir, exist_ok=True)
    _migrate_legacy(settings.data_dir)

    summarizer = Summarizer(settings)
    log.info("AI Memory starting; assistant=%s user=%s tz=%s boundary=%02d:00 grace=%dd data=%s summarizer=%s",
             settings.assistant_name, settings.user_name, settings.timezone, settings.day_boundary_hour,
             settings.forget_grace_days, settings.data_dir, summarizer.description)

    store = Store(settings.data_dir, _tz(settings.timezone),
                  boundary_hour=settings.day_boundary_hour, grace_days=settings.forget_grace_days)
    ctx = AppContext(settings, store, summarizer)
    loop = asyncio.get_running_loop()

    async def on_command(payload: dict) -> None:
        cmd = str(payload.get("command") or "")
        if cmd == "refresh":
            ctx.refresh()
        elif cmd in ("rollover", "tick"):
            await scheduler.tick(force=(cmd == "rollover"))
        else:
            log.warning("Unknown MQTT command: %r", cmd)

    mqtt = MqttBridge(settings.mqtt, loop, ctx.ingest, on_connected=ctx.refresh, on_command=on_command,
                      assistant_name=settings.assistant_name)
    ctx.mqtt = mqtt
    scheduler = Scheduler(ctx)
    ctx.scheduler = scheduler
    mqtt.start()
    ctx.refresh()

    app = build_app(ctx)
    port = int(os.environ.get("AIMEM_PORT", "8099"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    try:
        await asyncio.gather(server.serve(), scheduler.run())
    finally:
        mqtt.stop()


if __name__ == "__main__":
    asyncio.run(main())
