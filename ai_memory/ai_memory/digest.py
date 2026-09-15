"""Build the fixed-ceiling digest that gets pasted into Des's prompt.

Sections and character caps (roughly 4 chars per token):
  facts ~300 tokens, loops ~300, yesterday ~300, today ~1000.
"""
from typing import Dict, List

from .store import Store

CAPS = {"facts": 1200, "loops": 1200, "yesterday": 1200, "today": 4000}
RECENT_N = 8

def instruction(user_name: str) -> str:
    return (
        "These notes are a compressed digest. If you need more detail than they give, call "
        f"search_notes or read_day before answering. When {user_name} asks you to remember, forget, or "
        "track something, use the notebook tools rather than relying on conversation memory."
    )


def _cap(text: str, n: int) -> str:
    text = text.rstrip()
    if len(text) <= n:
        return text
    return text[: max(0, n - 3)].rstrip() + "..."


def fmt_entry(e: dict, with_day: bool = False) -> str:
    ts = e.get("ts", "")
    stamp = ts[:16].replace("T", " ") if with_day else ts[11:16]
    return f"{stamp} | {e.get('type', '?')} | {e.get('text', '')}"


def fmt_loop(lp: dict) -> str:
    w = lp.get("window") or {}
    bits = []
    if w.get("start") or w.get("end"):
        bits.append(f"{(w.get('start') or '')[:16].replace('T', ' ')}..{(w.get('end') or '')[:16].replace('T', ' ')}".strip("."))
    if lp.get("status") == "expired":
        bits.append("window passed, confirm")
    if lp.get("review") and lp.get("status") != "expired":
        bits.append("needs review")
    extra = f" [{'; '.join(bits)}]" if bits else ""
    return f"- ({lp['id']}) {lp['text']}{extra}"


def _facts_section(store: Store) -> str:
    facts = store.facts()
    if not facts:
        return "(none yet)"
    lines = [f"- {f['text']}" for f in facts]
    text = "\n".join(lines)
    if len(text) > CAPS["facts"]:
        keep: List[str] = []
        used = 0
        for ln in lines:
            if used + len(ln) + 1 > CAPS["facts"] - 20:
                break
            keep.append(ln)
            used += len(ln) + 1
        text = "\n".join(keep) + f"\n(+{len(lines) - len(keep)} more facts, use list_facts)"
    return text


def _loops_section(store: Store) -> str:
    active, resolved = store.digest_loops()
    lines = [fmt_loop(lp) for lp in active]
    if resolved:
        lines += [f"- (resolved) {lp['text']}" for lp in resolved]
    if not lines:
        return "(none)"
    text = "\n".join(lines)
    if len(text) > CAPS["loops"]:
        keep: List[str] = []
        used = 0
        for ln in lines:
            if used + len(ln) + 1 > CAPS["loops"] - 20:
                break
            keep.append(ln)
            used += len(ln) + 1
        text = "\n".join(keep) + f"\n(+{len(lines) - len(keep)} more, use list_loops)"
    return text


def _yesterday_section(store: Store) -> Dict[str, str]:
    ctx = store.yesterday_context()
    day = ctx["day"]
    if ctx["summary"]:
        return {"day": day, "text": _cap(ctx["summary"].get("yesterday_paragraph", ""), CAPS["yesterday"])}
    if not ctx["entries"]:
        return {"day": day, "text": "(no notes from that day)"}
    lines = [fmt_entry(e) for e in ctx["entries"][-6:]]
    text = "(summary not written yet; last raw notes)\n" + "\n".join(lines)
    return {"day": day, "text": _cap(text, CAPS["yesterday"])}


def _today_section(store: Store) -> Dict[str, object]:
    day = store.today()
    entries = store.today_entries()
    roll = store.rolling(day)
    summary = ""
    recent = entries
    if roll and roll.get("summary"):
        summary = roll["summary"].strip()
        covered = set(roll.get("covered_ids") or [])
        recent = [e for e in entries if e["id"] not in covered]
    recent = recent[-RECENT_N:]
    lines = [fmt_entry(e) for e in recent]
    budget = CAPS["today"]
    if summary:
        summary = _cap(summary, budget // 2 if lines else budget)
        budget -= len(summary) + 12
    while lines and sum(len(ln) + 1 for ln in lines) > budget:
        lines.pop(0)
    lines = [_cap(ln, max(200, budget)) for ln in lines]
    parts = []
    if summary:
        parts.append("So far: " + summary)
    if lines:
        parts.append("Recent:\n" + "\n".join(lines))
    if not parts:
        parts.append("(nothing noted yet today)")
    return {"day": day, "text": "\n".join(parts), "count": len(entries)}


def build(store: Store, user_name: str = "the user", assistant_name: str = "Assistant") -> dict:
    now = store.now()
    facts = _facts_section(store)
    loops = _loops_section(store)
    yesterday = _yesterday_section(store)
    today = _today_section(store)
    pending = len(store.candidates("pending"))
    # No generation timestamp here: the block must be byte-identical whenever the underlying
    # memory is unchanged, or every heartbeat would invalidate the LLM's prompt cache. Entry
    # timestamps inside the sections carry the chronology; the current clock comes from the
    # assistant's GetDateTime tool.
    block = (
        f"[{assistant_name} notebook]\n"
        f"Long-term facts about {user_name}:\n{facts}\n\n"
        f"Open loops / near future:\n{loops}\n\n"
        f"Yesterday ({yesterday['day']}):\n{yesterday['text']}\n\n"
        f"Today so far ({today['day']}):\n{today['text']}\n\n"
        f"{instruction(user_name)}"
    )
    if pending:
        block += f"\n({pending} durable-memory candidate(s) awaiting approval by {user_name}; use list_candidates.)"
    return {
        "updated": now.isoformat(timespec="seconds"),
        "today_day": today["day"],
        "yesterday_day": yesterday["day"],
        "entries_today": today["count"],
        "pending_candidates": pending,
        "sections": {"facts": facts, "loops": loops, "yesterday": yesterday["text"], "today": today["text"]},
        "prompt_block": block,
    }
