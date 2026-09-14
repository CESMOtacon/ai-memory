"""Periodic work: loop expiry, idempotent rollover, rolling today summary, purge, heartbeat."""
import asyncio
import logging

from .context import AppContext

log = logging.getLogger(__name__)

TICK_SECONDS = 300
ROLLING_MIN_ENTRIES = 10
ROLLING_MIN_CHARS = 3500


class Scheduler:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self._warned_disabled = False
        self._lock = asyncio.Lock()

    async def run(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("Scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self, force: bool = False) -> None:
        async with self._lock:
            store = self.ctx.store
            expired = store.expire_loops()
            if expired:
                log.info("Expired %d loop(s)", len(expired))
            await self.rollover()
            await self.rolling(force=force)
            purged = store.purge_due()
            if purged:
                log.info("Purged %d forgotten entr(y/ies)", len(purged))
            self.ctx.refresh()

    # ---------------------------------------------------------- rollover
    async def rollover(self) -> None:
        store, summ = self.ctx.store, self.ctx.summarizer
        days = store.days_needing_summary()
        if not days:
            return
        if not summ.enabled:
            if not self._warned_disabled:
                log.warning("%d day(s) await summary but the summarizer is not available (%s)",
                            len(days), summ.description)
                self._warned_disabled = True
            return
        for day in days:
            entries = store.read_day(day)
            active, _ = store.digest_loops()
            log.info("Summarizing day %s (%d entries) with %s", day, len(entries), summ.description)
            try:
                result = await summ.summarize_day(day, entries, active)
            except Exception as e:  # noqa: BLE001
                log.error("Summary for %s failed, will retry later: %s", day, e)
                return
            if not result.yesterday_paragraph:
                log.error("Summary for %s came back empty, will retry later", day)
                return
            store.save_summary(day, {
                "yesterday_paragraph": result.yesterday_paragraph,
                "loops_to_open": result.loops_to_open,
                "loops_to_close": result.loops_to_close,
                "durable_candidates": result.durable_memory_candidates,
                "input_entry_ids": [e["id"] for e in entries],
                "backend": summ.description,
            })
            known = {lp["id"] for lp in active}
            for text in result.loops_to_open:
                try:
                    store.open_loop(text, source="summarizer", review=True)
                except ValueError:
                    pass
            for loop_id in result.loops_to_close:
                if loop_id in known:
                    store.close_loop(loop_id, status="done", reason="per day summary", source="summarizer")
            added = store.add_candidates(result.durable_memory_candidates, day)
            log.info("Day %s summarized; %d candidate(s) queued", day, len(added))
            self.ctx.refresh()

    # ----------------------------------------------------------- rolling
    async def rolling(self, force: bool = False) -> None:
        store, summ = self.ctx.store, self.ctx.summarizer
        if not summ.enabled:
            return
        day = store.today()
        entries = store.today_entries()
        roll = store.rolling(day)
        covered = set((roll or {}).get("covered_ids") or [])
        new = [e for e in entries if e["id"] not in covered]
        if not new:
            return
        chars = sum(len(e.get("text", "")) for e in new)
        if not force and len(new) < ROLLING_MIN_ENTRIES and chars < ROLLING_MIN_CHARS:
            return
        keep_raw = 0 if force else 3
        to_fold = new[: len(new) - keep_raw] if keep_raw else new
        if not to_fold:
            return
        try:
            text = await summ.summarize_rolling(day, (roll or {}).get("summary", ""), to_fold)
        except Exception as e:  # noqa: BLE001
            log.error("Rolling summary failed: %s", e)
            return
        if not text:
            log.error("Rolling summary came back empty")
            return
        store.save_rolling(day, text, to_fold[-1]["id"], sorted(covered | {e["id"] for e in to_fold}))
        log.info("Rolling summary updated for %s (%d entries folded)", day, len(to_fold))
