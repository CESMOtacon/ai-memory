"""Periodic work: loop expiry, idempotent rollover, rolling today summary, purge, heartbeat."""
import asyncio
import logging
import re
from typing import Iterable, List, Set

from .context import AppContext

log = logging.getLogger(__name__)

TICK_SECONDS = 300
ROLLING_MIN_ENTRIES = 10
ROLLING_MIN_CHARS = 3500

_STOP = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "has", "have", "had", "and", "or", "of",
    "to", "with", "for", "on", "in", "at", "by", "from", "his", "her", "their", "he", "she", "they", "it",
    "that", "this", "as", "into", "about", "up", "out", "own", "currently", "still", "also", "some",
    "named", "name", "called", "one", "two", "said", "says", "goes", "go", "get", "got", "does", "do",
}
# Phrasing that marks a pending activity rather than a standing fact.
_ACTIVITY = re.compile(
    r"\b(is|are|was|has been|currently|still)\s+(searching|looking|working|trying|planning|going|hunting|"
    r"hoping|waiting|preparing|seeking|aiming|attempting|job)\b|\b(plans?|needs?|wants?|intends?|hopes?|has|have)"
    r"\s+to\b|\bhunting\b|\bpending\b|\bto[- ]do\b|\bin progress\b|\bworking on\b", re.I)


def _content(text: str, extra_stop: Iterable[str] = ()) -> Set[str]:
    """Distinctive content stems: lowercase words, stopwords removed, crudely stemmed."""
    stop = _STOP | {w.lower() for w in extra_stop}
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in stop or len(w) < 3:
            continue
        out.add(w[:5] if len(w) > 5 else w)
    return out


def _is_duplicate(cand: str, known: str, names: Iterable[str]) -> bool:
    """Same fact in other words: shares >=2 distinctive stems and mostly contained in the known text."""
    a, b = _content(cand, names), _content(known, names)
    if len(a) < 2 or len(b) < 2:
        return False
    shared = len(a & b)
    coverage = shared / len(a)  # how much of the candidate the known text already says
    jaccard = shared / len(a | b)
    return (shared >= 2 and coverage >= 0.6) or (jaccard >= 0.5 and len(b) >= 3)


def _is_loopish(cand: str, loops: Iterable[str], names: Iterable[str]) -> bool:
    """A candidate that shares a distinctive word with a loop and is phrased as an activity is a task."""
    if not _ACTIVITY.search(cand):
        return False
    a = {t for t in _content(cand, names) if len(t) >= 4}
    return any(a & {t for t in _content(lp, names) if len(t) >= 4} for lp in loops)


def _is_duplicate_loop(proposal: str, existing: str, names: Iterable[str]) -> bool:
    a, b = _content(proposal, names), _content(existing, names)
    if not a or not b:
        return False
    shared = len(a & b)
    return shared / len(a | b) >= 0.5 or (shared / min(len(a), len(b)) >= 0.8 and min(len(a), len(b)) >= 2)


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
        names = [self.ctx.settings.user_name, self.ctx.settings.assistant_name]
        for day in days:
            entries = store.read_day(day)
            active, _ = store.digest_loops()
            all_loops = store.loops()
            facts = [f["text"] for f in store.facts()]
            pending = [c["text"] for c in store.candidates("pending")]
            rejected = [c["text"] for c in store.candidates("rejected")][-40:]
            approved = [c["text"] for c in store.candidates("approved")]
            log.info("Summarizing day %s (%d entries) with %s", day, len(entries), summ.description)
            try:
                result = await summ.summarize_day(day, entries, active, facts, pending, rejected)
            except Exception as e:  # noqa: BLE001
                log.error("Summary for %s failed, will retry later: %s", day, e)
                return
            if not result.yesterday_paragraph:
                log.error("Summary for %s came back empty, will retry later", day)
                return

            # Loops: skip proposals that duplicate an existing loop (any status, so a loop closed
            # yesterday is not reopened by the summary of that same day).
            loops_kept: List[str] = []
            for text in result.loops_to_open:
                if any(_is_duplicate_loop(text, lp["text"], names) for lp in all_loops):
                    log.info("Loop proposal skipped as duplicate: %r", text)
                    continue
                loops_kept.append(text)

            # Candidates: drop re-proposals of known facts/candidates and anything that mirrors a loop.
            known = facts + pending + rejected + approved
            loop_texts = [lp["text"] for lp in all_loops] + loops_kept
            cands_kept: List[str] = []
            for text in result.durable_memory_candidates:
                if any(_is_duplicate(text, k, names) for k in known):
                    log.info("Candidate skipped as duplicate: %r", text)
                    continue
                if _is_loopish(text, loop_texts, names):
                    log.info("Candidate skipped as loop-like: %r", text)
                    continue
                cands_kept.append(text)

            store.save_summary(day, {
                "yesterday_paragraph": result.yesterday_paragraph,
                "loops_to_open": result.loops_to_open,
                "loops_to_close": result.loops_to_close,
                "durable_candidates": result.durable_memory_candidates,
                "loops_applied": loops_kept,
                "candidates_applied": cands_kept,
                "input_entry_ids": [e["id"] for e in entries],
                "backend": summ.description,
            })
            known_ids = {lp["id"] for lp in active}
            for text in loops_kept:
                try:
                    store.open_loop(text, source="summarizer", review=True)
                except ValueError:
                    pass
            for loop_id in result.loops_to_close:
                if loop_id in known_ids:
                    store.close_loop(loop_id, status="done", reason="per day summary", source="summarizer")
            added = store.add_candidates(cands_kept, day)
            log.info("Day %s summarized; %d loop(s) opened, %d candidate(s) queued (%d proposed)",
                     day, len(loops_kept), len(added), len(result.durable_memory_candidates))
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
