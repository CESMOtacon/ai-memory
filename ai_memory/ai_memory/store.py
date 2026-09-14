"""Canonical storage: append-only day logs, tombstones, loops, facts, candidates, summaries.

The day log is the source of truth. Everything else is derived and rebuildable.
"""
import json
import logging
import os
import secrets
import threading
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

ENTRY_TYPES = {
    "morning_announcement", "announcement", "event", "explicit_memory", "exchange",
    "loop_opened", "loop_closed", "system",
}
CONTROL_TYPES = {"tombstone", "tombstone_restore"}
PURGED_TEXT = "[purged]"


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _load_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        log.error("Corrupt JSON in %s: %s", path, e)
        return default


def _save_json(path: str, data: Any) -> None:
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1))


def _new_id(ts: datetime) -> str:
    return f"{int(ts.timestamp() * 1000):013d}-{secrets.token_hex(3)}"


class Store:
    def __init__(self, data_dir: str, tz: tzinfo, boundary_hour: int = 5, grace_days: int = 7):
        self.dir = data_dir
        self.tz = tz
        self.boundary = boundary_hour
        self.grace_days = grace_days
        self.days_dir = os.path.join(data_dir, "days")
        self.sum_dir = os.path.join(data_dir, "summaries")
        self.roll_dir = os.path.join(data_dir, "rolling")
        for d in (self.days_dir, self.sum_dir, self.roll_dir):
            os.makedirs(d, exist_ok=True)
        self.lock = threading.RLock()
        self._tombstones: Dict[str, dict] = {}
        self._rebuild_tombstone_index()

    # ------------------------------------------------------------------ time
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def day_key(self, ts: datetime) -> str:
        ts = ts.astimezone(self.tz)
        return (ts - timedelta(hours=self.boundary)).date().isoformat()

    def today(self) -> str:
        return self.day_key(self.now())

    def day_start(self, day: str) -> datetime:
        d = date.fromisoformat(day)
        return datetime(d.year, d.month, d.day, self.boundary, 0, 0, tzinfo=self.tz)

    @staticmethod
    def previous_day(day: str) -> str:
        return (date.fromisoformat(day) - timedelta(days=1)).isoformat()

    @staticmethod
    def next_day(day: str) -> str:
        return (date.fromisoformat(day) + timedelta(days=1)).isoformat()

    def is_day_complete(self, day: str) -> bool:
        return self.day_start(self.next_day(day)) <= self.now()

    # --------------------------------------------------------------- raw io
    def _day_path(self, day: str) -> str:
        return os.path.join(self.days_dir, f"{day}.jsonl")

    def list_days(self) -> List[str]:
        return sorted(f[:-6] for f in os.listdir(self.days_dir) if f.endswith(".jsonl"))

    def _read_raw(self, day: str) -> List[dict]:
        path = self._day_path(day)
        out: List[dict] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        log.error("Bad line in %s: %r", path, line[:120])
        except FileNotFoundError:
            pass
        return out

    def _append_raw(self, day: str, record: dict) -> None:
        with open(self._day_path(day), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _rewrite_raw(self, day: str, records: List[dict]) -> None:
        _atomic_write(self._day_path(day),
                      "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))

    # -------------------------------------------------------------- entries
    def add_entry(self, type_: str, text: str, source: Optional[str] = None,
                  conversation_id: Optional[str] = None, ts: Optional[datetime] = None,
                  meta: Optional[dict] = None) -> dict:
        if type_ not in ENTRY_TYPES:
            type_ = "exchange"
        text = (text or "").strip()
        if not text:
            raise ValueError("empty text")
        ts = (ts or self.now()).astimezone(self.tz)
        with self.lock:
            entry = {
                "id": _new_id(ts),
                "ts": ts.isoformat(timespec="seconds"),
                "day": self.day_key(ts),
                "type": type_,
                "source": source or "unknown",
                "conversation_id": conversation_id,
                "text": text,
            }
            if meta:
                entry["meta"] = meta
            self._append_raw(entry["day"], entry)
            return entry

    def _is_hidden(self, rec: dict) -> bool:
        return rec.get("id") in self._tombstones or bool(rec.get("purged", False))

    def read_day(self, day: str, include_hidden: bool = False) -> List[dict]:
        with self.lock:
            recs = [r for r in self._read_raw(day) if r.get("type") not in CONTROL_TYPES]
            if include_hidden:
                return recs
            return [r for r in recs if not self._is_hidden(r)]

    def today_entries(self) -> List[dict]:
        return self.read_day(self.today())

    def find_entry(self, entry_id: str) -> Optional[Tuple[str, dict]]:
        with self.lock:
            # Fast path: derive the day from the id's timestamp prefix.
            try:
                ts = datetime.fromtimestamp(int(entry_id.split("-")[0]) / 1000, tz=self.tz)
                day = self.day_key(ts)
                for r in self._read_raw(day):
                    if r.get("id") == entry_id and r.get("type") not in CONTROL_TYPES:
                        return day, r
            except (ValueError, IndexError, OverflowError, OSError):
                pass
            for day in reversed(self.list_days()):
                for r in self._read_raw(day):
                    if r.get("id") == entry_id and r.get("type") not in CONTROL_TYPES:
                        return day, r
        return None

    def search(self, query: str, limit: int = 5, include_hidden: bool = False) -> List[dict]:
        terms = [t for t in (query or "").lower().split() if t]
        if not terms:
            return []
        hits: List[dict] = []
        with self.lock:
            for day in reversed(self.list_days()):
                for r in self.read_day(day, include_hidden=include_hidden):
                    hay = (r.get("text") or "").lower()
                    if all(t in hay for t in terms):
                        hits.append(r)
                if limit > 0 and len(hits) >= limit * 4:
                    break
        hits.sort(key=lambda r: r["ts"])
        return hits[-limit:] if limit > 0 else hits

    # ----------------------------------------------------------- tombstones
    def _rebuild_tombstone_index(self) -> None:
        idx: Dict[str, dict] = {}
        for day in self.list_days():
            for r in self._read_raw(day):
                if r.get("type") == "tombstone":
                    idx[r["target"]] = r
                elif r.get("type") == "tombstone_restore":
                    idx.pop(r["target"], None)
        self._tombstones = idx

    def tombstones(self) -> Dict[str, dict]:
        return dict(self._tombstones)

    def forget(self, entry_ids: List[str], reason: str = "user_request",
               immediate: bool = False) -> dict:
        """Tombstone entries. Hidden immediately; purged after the grace period (or now if immediate)."""
        result: Dict[str, List[str]] = {"tombstoned": [], "not_found": [], "already": [],
                                        "stale_summaries": []}
        now = self.now()
        purge_after = now if immediate else now + timedelta(days=self.grace_days)
        with self.lock:
            for eid in entry_ids:
                if eid in self._tombstones:
                    result["already"].append(eid)
                    continue
                found = self.find_entry(eid)
                if not found:
                    result["not_found"].append(eid)
                    continue
                day, _entry = found
                rec = {
                    "id": _new_id(now), "ts": now.isoformat(timespec="seconds"), "day": day,
                    "type": "tombstone", "target": eid, "reason": reason,
                    "purge_after": purge_after.isoformat(timespec="seconds"), "purged": False,
                }
                self._append_raw(day, rec)
                self._tombstones[eid] = rec
                result["tombstoned"].append(eid)
                self._invalidate_derived(day, eid, result)
        return result

    def restore(self, entry_id: str) -> bool:
        with self.lock:
            ts = self._tombstones.get(entry_id)
            if not ts or ts.get("purged"):
                return False
            day = ts["day"]
            now = self.now()
            self._append_raw(day, {"id": _new_id(now), "ts": now.isoformat(timespec="seconds"),
                                   "day": day, "type": "tombstone_restore", "target": entry_id})
            del self._tombstones[entry_id]
            self.mark_stale(day)
            return True

    def _invalidate_derived(self, day: str, entry_id: str, result: dict) -> None:
        summ = self.summary(day)
        if summ and (entry_id in summ.get("input_entry_ids", []) or not summ.get("input_entry_ids")):
            self.mark_stale(day)
            result["stale_summaries"].append(day)
        roll = os.path.join(self.roll_dir, f"{day}.json")
        if os.path.exists(roll):
            os.remove(roll)
        cands = [c for c in self.candidates() if c.get("source_entry") != entry_id]
        _save_json(self._p("candidates.json"), cands)
        loops = self.loops()
        changed = False
        for lp in loops:
            if lp.get("source_entry") == entry_id:
                lp["review"] = True
                lp["review_reason"] = "source entry forgotten"
                changed = True
        if changed:
            self._save_loops(loops)

    def purge_due(self) -> List[str]:
        """Physically remove text of tombstoned entries whose grace period has ended."""
        purged: List[str] = []
        now = self.now()
        with self.lock:
            due = [t for t in self._tombstones.values()
                   if not t.get("purged") and datetime.fromisoformat(t["purge_after"]) <= now]
            by_day: Dict[str, List[dict]] = {}
            for t in due:
                by_day.setdefault(t["day"], []).append(t)
            for day, ts_list in by_day.items():
                targets = {t["target"] for t in ts_list}
                recs = self._read_raw(day)
                for r in recs:
                    if r.get("id") in targets:
                        r["text"] = PURGED_TEXT
                        r["purged"] = True
                        r.pop("meta", None)
                    elif r.get("type") == "tombstone" and r.get("target") in targets:
                        r["purged"] = True
                self._rewrite_raw(day, recs)
                for t in ts_list:
                    t["purged"] = True
                    purged.append(t["target"])
                # Any derived copy that could still contain the text goes too.
                summ = self.summary(day)
                if summ and (summ.get("stale")
                             or any(x in summ.get("input_entry_ids", []) for x in targets)):
                    os.remove(os.path.join(self.sum_dir, f"{day}.json"))
                roll = os.path.join(self.roll_dir, f"{day}.json")
                if os.path.exists(roll):
                    os.remove(roll)
        return purged

    # ---------------------------------------------------------------- loops
    def _p(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def loops(self) -> List[dict]:
        return _load_json(self._p("loops.json"), [])

    def _save_loops(self, loops: List[dict]) -> None:
        _save_json(self._p("loops.json"), loops)

    def get_loop(self, loop_id: str) -> Optional[dict]:
        for lp in self.loops():
            if lp["id"] == loop_id:
                return lp
        return None

    def open_loop(self, text: str, window_start: Optional[str] = None,
                  window_end: Optional[str] = None, source: str = "assistant",
                  source_entry: Optional[str] = None, review: bool = False) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("empty loop text")
        now = self.now()
        with self.lock:
            loops = self.loops()
            window = None
            if window_start or window_end:
                window = {"start": window_start or None, "end": window_end or None}
            lp = {
                "id": "L" + _new_id(now)[-9:], "text": text,
                "created": now.isoformat(timespec="seconds"),
                "window": window,
                "status": "open", "closed": None, "closed_reason": None,
                "source": source, "source_entry": source_entry, "review": review,
            }
            loops.append(lp)
            self._save_loops(loops)
            self.add_entry("loop_opened", text, source=source, meta={"loop_id": lp["id"]})
            return lp

    def close_loop(self, loop_id: str, status: str = "done", reason: Optional[str] = None,
                   source: str = "assistant") -> Optional[dict]:
        if status not in ("done", "dropped"):
            status = "done"
        with self.lock:
            loops = self.loops()
            for lp in loops:
                if lp["id"] == loop_id and lp["status"] in ("open", "expired"):
                    lp["status"] = status
                    lp["closed"] = self.now().isoformat(timespec="seconds")
                    lp["closed_reason"] = reason
                    lp["review"] = False
                    self._save_loops(loops)
                    suffix = f"{status}: {reason}" if reason else status
                    self.add_entry("loop_closed", f"{lp['text']} ({suffix})",
                                   source=source, meta={"loop_id": loop_id})
                    return lp
        return None

    def reopen_loop(self, loop_id: str) -> Optional[dict]:
        with self.lock:
            loops = self.loops()
            for lp in loops:
                if lp["id"] == loop_id and lp["status"] != "open":
                    lp["status"] = "open"
                    lp["closed"] = None
                    lp["closed_reason"] = None
                    lp["review"] = False
                    self._save_loops(loops)
                    return lp
        return None

    def expire_loops(self) -> List[dict]:
        now = self.now()
        expired = []
        with self.lock:
            loops = self.loops()
            for lp in loops:
                w = lp.get("window") or {}
                end = w.get("end")
                if lp["status"] == "open" and end:
                    try:
                        end_dt = datetime.fromisoformat(end)
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=self.tz)
                    except ValueError:
                        continue
                    if end_dt <= now:
                        lp["status"] = "expired"
                        lp["review"] = True
                        lp["review_reason"] = "window passed; confirm done or reopen"
                        expired.append(lp)
            if expired:
                self._save_loops(loops)
        return expired

    def digest_loops(self) -> Tuple[List[dict], List[dict]]:
        """(active, recently_resolved). Resolved = closed since the start of the previous Des-day."""
        cutoff = self.day_start(self.previous_day(self.today()))
        active, resolved = [], []
        for lp in self.loops():
            if lp["status"] in ("open", "expired"):
                active.append(lp)
            elif lp.get("closed"):
                try:
                    if datetime.fromisoformat(lp["closed"]) >= cutoff:
                        resolved.append(lp)
                except ValueError:
                    pass
        return active, resolved

    # ---------------------------------------------------------------- facts
    def facts(self) -> List[dict]:
        return _load_json(self._p("facts.json"), [])

    def add_fact(self, text: str, source: str = "manual") -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("empty fact")
        with self.lock:
            facts = self.facts()
            f = {"id": "F" + _new_id(self.now())[-9:], "text": text,
                 "added": self.now().isoformat(timespec="seconds"), "source": source}
            facts.append(f)
            _save_json(self._p("facts.json"), facts)
            return f

    def remove_fact(self, fact_id: str) -> bool:
        with self.lock:
            facts = self.facts()
            new = [f for f in facts if f["id"] != fact_id]
            if len(new) == len(facts):
                return False
            _save_json(self._p("facts.json"), new)
            return True

    # ----------------------------------------------------------- candidates
    def candidates(self, status: Optional[str] = None) -> List[dict]:
        c = _load_json(self._p("candidates.json"), [])
        return [x for x in c if status is None or x.get("status") == status]

    def add_candidates(self, texts: List[str], day: str,
                       source_entry: Optional[str] = None) -> List[dict]:
        added = []
        with self.lock:
            cands = self.candidates()
            existing = {c["text"].strip().lower() for c in cands}
            existing |= {f["text"].strip().lower() for f in self.facts()}
            for t in texts:
                t = (t or "").strip()
                if not t or t.lower() in existing:
                    continue
                c = {"id": "C" + _new_id(self.now())[-9:], "text": t, "day": day,
                     "status": "pending", "created": self.now().isoformat(timespec="seconds"),
                     "source_entry": source_entry}
                cands.append(c)
                added.append(c)
                existing.add(t.lower())
            _save_json(self._p("candidates.json"), cands)
        return added

    def resolve_candidate(self, cand_id: str, approve: bool) -> Optional[dict]:
        with self.lock:
            cands = self.candidates()
            for c in cands:
                if c["id"] == cand_id and c["status"] == "pending":
                    c["status"] = "approved" if approve else "rejected"
                    c["resolved"] = self.now().isoformat(timespec="seconds")
                    _save_json(self._p("candidates.json"), cands)
                    if approve:
                        self.add_fact(c["text"], source=f"candidate:{cand_id}")
                    return c
        return None

    # ------------------------------------------------------------ summaries
    def summary(self, day: str) -> Optional[dict]:
        return _load_json(os.path.join(self.sum_dir, f"{day}.json"), None)

    def save_summary(self, day: str, data: dict) -> None:
        data = dict(data)
        data["day"] = day
        data["stale"] = False
        data["generated"] = self.now().isoformat(timespec="seconds")
        _save_json(os.path.join(self.sum_dir, f"{day}.json"), data)

    def mark_stale(self, day: str) -> None:
        s = self.summary(day)
        if s:
            s["stale"] = True
            _save_json(os.path.join(self.sum_dir, f"{day}.json"), s)

    def days_needing_summary(self, max_days: int = 60) -> List[str]:
        out = []
        for day in self.list_days()[-max_days:]:
            if not self.is_day_complete(day):
                continue
            if not self.read_day(day):
                continue
            s = self.summary(day)
            if s is None or s.get("stale"):
                out.append(day)
        return out

    def yesterday_context(self) -> dict:
        """Most recent completed Des-day with entries, plus its summary if fresh."""
        today = self.today()
        for day in reversed(self.list_days()):
            if day >= today:
                continue
            entries = self.read_day(day)
            if not entries:
                continue
            s = self.summary(day)
            fresh = s if (s and not s.get("stale")) else None
            return {"day": day, "entries": entries, "summary": fresh}
        return {"day": self.previous_day(today), "entries": [], "summary": None}

    # -------------------------------------------------------------- rolling
    def rolling(self, day: str) -> Optional[dict]:
        return _load_json(os.path.join(self.roll_dir, f"{day}.json"), None)

    def save_rolling(self, day: str, summary: str, covered_through: str,
                     covered_ids: List[str]) -> None:
        _save_json(os.path.join(self.roll_dir, f"{day}.json"), {
            "day": day, "summary": summary, "covered_through": covered_through,
            "covered_ids": covered_ids, "updated": self.now().isoformat(timespec="seconds")})
