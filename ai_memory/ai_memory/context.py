"""Shared application context: store, summarizer, MQTT publisher, digest refresh."""
import asyncio
import hashlib
import json
import logging
import os
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
        # Persisted so a restart neither bumps "updated" nor republishes an identical block.
        self._state_path = os.path.join(settings.data_dir, "digest_state.json")
        self._state = self._load_state()
        self.last_check: Optional[str] = None  # operational: when the digest was last rebuilt/compared

    # ------------------------------------------------------------ state
    def _load_state(self) -> dict:
        try:
            with open(self._state_path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"hash": None, "updated": None}

    def _save_state(self) -> None:
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f)
        os.replace(tmp, self._state_path)

    @staticmethod
    def _hash(block: str) -> str:
        return hashlib.sha256(block.encode("utf-8")).hexdigest()

    # ----------------------------------------------------------- digest
    def build_digest(self) -> dict:
        d = digest_mod.build(self.store, user_name=self.settings.user_name,
                             assistant_name=self.settings.assistant_name)
        d["last_check"] = d.pop("updated")  # build time is operational, not semantic
        d["updated"] = self._state.get("updated")
        d["hash"] = self._hash(d["prompt_block"])
        self.last_digest = d
        return d

    def refresh(self, force: bool = False) -> dict:
        """Rebuild the digest; publish only if its prompt text changed (or force, e.g. on reconnect).

        Safe to call from any thread. The prompt block is deterministic from memory state, so an
        unchanged notebook yields byte-identical text and no publish, keeping the LLM prompt cache warm.
        """
        d = self.build_digest()
        self.last_check = d["last_check"]
        changed = d["hash"] != self._state.get("hash")
        if changed:
            self._state = {"hash": d["hash"], "updated": d["last_check"]}
            self._save_state()
            d["updated"] = self._state["updated"]
            log.info("Digest changed (%d chars); publishing", len(d["prompt_block"]))
        if (changed or force) and self.mqtt is not None:
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
