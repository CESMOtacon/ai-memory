"""Server-rendered status/approval page for the ingress panel. Self-contained, no external assets."""
from html import escape as esc
from typing import List

from .context import AppContext

CSS = """
:root{--bg:#f4f5f7;--card:#fff;--text:#1c2026;--muted:#6b7280;--border:#e3e6ea;--accent:#2563eb;--accent-ink:#fff;
--ok:#16a34a;--warn:#d97706;--danger:#dc2626;--chip:#eef2f7;--pre:#f8fafc;--shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.08)}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1f26;--text:#e6e9ee;--muted:#9aa3b2;--border:#2a313b;
--accent:#3b82f6;--ok:#22c55e;--warn:#f59e0b;--danger:#ef4444;--chip:#242b35;--pre:#13171c;--shadow:0 1px 2px rgba(0,0,0,.4)}}
*{box-sizing:border-box}html{color-scheme:light dark}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:0 16px 40px}
header{max-width:1180px;margin:0 auto;padding:22px 0 14px;display:flex;flex-wrap:wrap;align-items:center;gap:12px 18px}
header h1{font-size:20px;margin:0;font-weight:650;letter-spacing:-.01em}
header h1 span{color:var(--muted);font-weight:400;margin-left:8px;font-size:14px}
.pills{display:flex;flex-wrap:wrap;gap:8px;margin-left:auto}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;background:var(--chip);font-size:12px;color:var(--muted)}
.pill b{color:var(--text);font-weight:600}.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.bad{background:var(--danger)}
main{max-width:1180px;margin:0 auto;display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow);padding:16px 18px;min-width:0}
.card.wide{grid-column:1/-1}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 12px;display:flex;align-items:center;gap:8px}
.card h2 .count{background:var(--chip);color:var(--text);border-radius:999px;padding:0 8px;font-size:11px;letter-spacing:0}
.card h2 .actions{margin-left:auto;display:flex;gap:6px}
ul{list-style:none;margin:0;padding:0}li{display:flex;align-items:flex-start;gap:10px;padding:9px 0;border-top:1px solid var(--border)}
li:first-child{border-top:0}li .txt{flex:1;min-width:0;overflow-wrap:anywhere}li .meta{color:var(--muted);font-size:12px;margin-top:2px}
.btns{display:flex;gap:6px;flex-shrink:0}form{display:inline;margin:0}
button,.btn{font:inherit;font-size:12px;padding:5px 10px;border-radius:8px;border:1px solid var(--border);background:var(--card);color:var(--text);cursor:pointer}
button:hover{border-color:var(--muted)}button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
button.danger{color:var(--danger)}button.danger:hover{border-color:var(--danger)}
.badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:6px;background:var(--chip);color:var(--muted);white-space:nowrap}
.badge.announcement,.badge.morning_announcement{background:rgba(37,99,235,.14);color:var(--accent)}
.badge.explicit_memory{background:rgba(22,163,74,.14);color:var(--ok)}.badge.event{background:rgba(217,119,6,.14);color:var(--warn)}
.badge.loop_opened,.badge.loop_closed{background:rgba(124,58,237,.14);color:#8b5cf6}
.time{font-variant-numeric:tabular-nums;color:var(--muted);font-size:12px;min-width:38px;padding-top:2px}
pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--pre);border:1px solid var(--border);border-radius:8px;padding:12px 14px;
font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;max-height:520px;overflow:auto}
.add{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}.add input,.add select{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--border);
background:var(--pre);color:var(--text);flex:1;min-width:160px}
.empty{color:var(--muted);padding:8px 0}.tag{font-size:11px;color:var(--warn)}
footer{max-width:1180px;margin:18px auto 0;color:var(--muted);font-size:12px}
"""

JS = """
// Submit forms in place: post, then swap in the refreshed page so the scroll position survives.
document.addEventListener('submit', async function (e) {
  var f = e.target;
  if (!(f instanceof HTMLFormElement)) return;
  if (f.dataset.confirm && !confirm(f.dataset.confirm)) { e.preventDefault(); return; }
  if (!window.fetch || !window.DOMParser) return;  // plain submit + redirect still works
  e.preventDefault();
  var y = window.scrollY;
  var btn = f.querySelector('button'); if (btn) btn.disabled = true;
  try {
    var r = await fetch(f.action, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams(new FormData(f)).toString()});
    var doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    var nm = doc.querySelector('main'), nh = doc.querySelector('header'), nf = doc.querySelector('footer');
    if (!nm || !nh) throw new Error('unexpected response');
    document.querySelector('main').replaceWith(nm);
    document.querySelector('header').replaceWith(nh);
    if (nf) document.querySelector('footer').replaceWith(nf);
    window.scrollTo(0, y);
  } catch (err) { location.reload(); }
});
"""


def _btn(prefix: str, action: str, label: str, cls: str = "", confirm: str = "") -> str:
    c = f' data-confirm="{esc(confirm)}"' if confirm else ""
    k = f' class="{cls}"' if cls else ""
    return f'<form method="post" action="{prefix}/{action}"{c}><button{k}>{esc(label)}</button></form>'


def _pill(label: str, value: str, state: str = "") -> str:
    dot = f'<span class="dot {state}"></span>' if state else ""
    return f'<span class="pill">{dot}{esc(label)} <b>{esc(value)}</b></span>'


def _loop_meta(lp: dict) -> str:
    bits: List[str] = []
    w = lp.get("window") or {}
    if w.get("start") or w.get("end"):
        bits.append(f"{(w.get('start') or '')[:16].replace('T', ' ')} to {(w.get('end') or '')[:16].replace('T', ' ')}".strip())
    bits.append(f"opened {lp.get('created', '')[:16].replace('T', ' ')} via {lp.get('source', '?')}")
    s = esc(" · ".join(b for b in bits if b))
    if lp.get("status") == "expired":
        s += ' <span class="tag">window passed, confirm</span>'
    elif lp.get("review"):
        s += f' <span class="tag">{esc(lp.get("review_reason") or "needs review")}</span>'
    return s


def render_index(ctx: AppContext, prefix: str) -> str:
    store = ctx.store
    d = ctx.last_digest or ctx.build_digest()
    cands = store.candidates("pending")
    active, resolved = store.digest_loops()
    facts = store.facts()
    today = list(reversed(store.today_entries()))[:40]
    tomb = [(k, v) for k, v in store.tombstones().items() if not v.get("purged")]
    mqtt_ok = bool(ctx.mqtt and ctx.mqtt.connected)
    summ_ok = ctx.summarizer.enabled
    pending_days = store.days_needing_summary()
    p = prefix
    assistant = ctx.settings.assistant_name
    user = ctx.settings.user_name

    pills = "".join([
        _pill("MQTT", "connected" if mqtt_ok else "offline", "ok" if mqtt_ok else "bad"),
        _pill("Summarizer", ctx.summarizer.description, "ok" if summ_ok else "warn"),
        _pill("Today", d["today_day"]),
        _pill("Entries", str(d["entries_today"])),
        _pill("Days on file", str(len(store.list_days()))),
    ])

    # --- candidates
    if cands:
        cand_rows = "".join(
            f'<li><div class="txt">{esc(c["text"])}<div class="meta">proposed from {esc(c["day"])}</div></div>'
            f'<div class="btns">{_btn(p, "candidates/" + c["id"] + "/approve", "Approve", "primary")}'
            f'{_btn(p, "candidates/" + c["id"] + "/reject", "Reject")}</div></li>' for c in cands)
    else:
        cand_rows = '<li class="empty">Nothing waiting. The nightly summary proposes candidates here.</li>'

    # --- loops
    if active:
        loop_rows = "".join(
            f'<li><div class="txt">{esc(lp["text"])}<div class="meta">{_loop_meta(lp)}</div></div>'
            f'<div class="btns">{_btn(p, "loops/" + lp["id"] + "/done", "Done", "primary")}'
            + (_btn(p, "loops/" + lp["id"] + "/reopen", "Reopen") if lp["status"] == "expired" else "")
            + f'{_btn(p, "loops/" + lp["id"] + "/dropped", "Drop", "danger", "Drop this loop?")}</div></li>'
            for lp in active)
    else:
        loop_rows = '<li class="empty">No open loops.</li>'
    resolved_rows = "".join(
        f'<li><div class="txt" style="color:var(--muted)"><s>{esc(lp["text"])}</s>'
        f'<div class="meta">{esc(lp["status"])} {esc((lp.get("closed") or "")[:16].replace("T", " "))}'
        f'{(" · " + esc(lp["closed_reason"])) if lp.get("closed_reason") else ""}</div></div></li>'
        for lp in resolved)

    # --- facts
    if facts:
        fact_rows = "".join(
            f'<li><div class="txt">{esc(f["text"])}<div class="meta">added {esc(f["added"][:10])} · {esc(f["source"])}</div></div>'
            f'<div class="btns">{_btn(p, "facts/" + f["id"] + "/remove", "Remove", "danger", "Remove this fact?")}</div></li>'
            for f in facts)
    else:
        fact_rows = '<li class="empty">No long-term facts yet. Approve candidates or add one below.</li>'

    # --- entries
    if today:
        entry_rows = "".join(
            f'<li><span class="time">{esc(e["ts"][11:16])}</span>'
            f'<div class="txt"><span class="badge {esc(e["type"])}">{esc(e["type"].replace("_", " "))}</span> {esc(e["text"])}'
            f'<div class="meta">{esc(e.get("source") or "")}'
            f'{(" · " + esc(e["conversation_id"])) if e.get("conversation_id") else ""}</div></div>'
            f'<div class="btns">{_btn(p, "entries/" + e["id"] + "/forget", "Forget", "danger", "Forget this entry? It can be restored for 7 days.")}</div></li>'
            for e in today)
    else:
        entry_rows = '<li class="empty">Nothing noted yet today.</li>'

    # --- forgotten
    if tomb:
        tomb_rows = "".join(
            f'<li><div class="txt">Entry <code>{esc(k)}</code> from {esc(v.get("day", ""))}'
            f'<div class="meta">purges after {esc(v.get("purge_after", "")[:16].replace("T", " "))} · {esc(v.get("reason") or "")}</div></div>'
            f'<div class="btns">{_btn(p, "entries/" + k + "/restore", "Restore")}</div></li>' for k, v in tomb)
    else:
        tomb_rows = '<li class="empty">Nothing in the grace period.</li>'

    summary_note = ""
    if pending_days:
        summary_note = f'<div class="meta" style="margin-top:8px">{len(pending_days)} day(s) awaiting summary: {esc(", ".join(pending_days[-5:]))}</div>'

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Memory</title><style>{CSS}</style></head><body>
<header><h1>AI Memory<span>{esc(assistant)}'s notebook</span></h1><div class="pills">{pills}</div></header>
<main>
<section class="card wide"><h2>Digest <span class="count">what {esc(assistant)} sees</span>
<span class="actions">{_btn(p, "actions/refresh", "Rebuild")}{_btn(p, "actions/rollover", "Summarize now")}</span></h2>
<pre>{esc(d["prompt_block"])}</pre>{summary_note}</section>

<section class="card"><h2>Open loops <span class="count">{len(active)}</span></h2><ul>{loop_rows}</ul>
{('<h2 style="margin-top:14px">Recently resolved</h2><ul>' + resolved_rows + '</ul>') if resolved_rows else ''}
<form method="post" action="{p}/loops/new" class="add"><input name="text" placeholder="New open loop" required><button class="primary">Add</button></form></section>

<section class="card"><h2>Durable memory candidates <span class="count">{len(cands)}</span></h2><ul>{cand_rows}</ul></section>

<section class="card"><h2>Long-term facts <span class="count">{len(facts)}</span></h2><ul>{fact_rows}</ul>
<form method="post" action="{p}/facts/new" class="add"><input name="text" placeholder="Add a permanent fact about {esc(user)}" required><button class="primary">Add</button></form></section>

<section class="card"><h2>Forgotten, recoverable <span class="count">{len(tomb)}</span></h2><ul>{tomb_rows}</ul></section>

<section class="card wide"><h2>Today's entries <span class="count">{d["entries_today"]}</span></h2><ul>{entry_rows}</ul>
<form method="post" action="{p}/entries/new" class="add"><input name="text" placeholder="Add a note to the notebook" required>
<select name="type"><option value="explicit_memory">explicit memory</option><option value="exchange">exchange</option><option value="event">event</option><option value="system">system</option></select>
<button class="primary">Add</button></form></section>
</main>
<footer>Day boundary {ctx.settings.day_boundary_hour:02d}:00 · forget grace period {ctx.settings.forget_grace_days} days · notebook in {esc(ctx.settings.data_dir)} · digest last changed {esc((d.get("updated") or "never")[:16].replace("T", " "))} · last check {esc((d.get("last_check") or "")[:16].replace("T", " "))}</footer>
<script>{JS}</script></body></html>"""
