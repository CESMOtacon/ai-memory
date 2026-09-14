"""Shared application context: store, summarizer, MQTT publisher, digest refresh."""
import asyncio
import logging
from typing import Any, Dict, Optional

from . import digest as digest_mod
from .config import Settings
from .store import Store
from .summarizer import Summarizer

log = logging.getLogger(__name__)


class AppContext:
    def __init__(self, settings: Settings, store: Store, summarizer: Summarizer):
        self.settings = settings
        self.store = store
        self.summarizer = summarizer
        self.mqtt: Any = None  # set by main once the bridge exists
        self.scheduler: Any = None
        self.last_digest: Optional[dict] = None
        self._refresh_lock = asyncio.Lock()

    def build_digest(self) -> dict:
        d = digest_mod.build(self.store, user_name=self.settings.user_name,
                             assistant_name=self.settings.assistant_name)
        self.last_digest = d
        return d

    def refresh(self) -> dict:
        """Rebuild the digest and publish it. Safe to call from any thread."""
        d = self.build_digest()
        if self.mqtt is not None:
            try:
                self.mqtt.publish_digest(d)
            except Exception as e:  # noqa: BLE001
                log.warning("Digest publish failed: %s", e)
        return d

    async def ingest(self, payload: Dict[str, Any]) -> dict:
        """Accept an ingest payload from MQTT or HTTP."""
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        text = payload.get("text") or payload.get("message") or ""
        if not str(text).strip():
            raise ValueError("text is required")
        entry = self.store.add_entry(
            str(payload.get("type") or "exchange"),
            str(text),
            source=str(payload.get("source") or "mqtt"),
            conversation_id=payload.get("conversation_id"),
            meta=payload.get("meta") if isinstance(payload.get("meta"), dict) else None,
        )
        log.info("Ingested %s entry %s from %s", entry["type"], entry["id"], entry["source"])
        self.refresh()
        return entry
