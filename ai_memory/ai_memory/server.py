"""HTTP server: ingest/health/digest endpoints, ingress status page, and the MCP tool server."""
import json
import logging
from typing import List

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from .context import AppContext
from .digest import fmt_entry, fmt_loop
from .ui import render_index

log = logging.getLogger(__name__)

SENSOR_ENTITY = "sensor.ai_memory_digest"


def _render_entries(entries: List[dict]) -> str:
    if not entries:
        return "(no matching notes)"
    return "\n".join(f"[{e['id']}] {fmt_entry(e, with_day=True)}" for e in entries)


def _make_mcp(instructions: str) -> FastMCP:
    """FastMCP with DNS-rebinding protection relaxed: the server is only reachable on the
    Supervisor's internal network, and Home Assistant connects by add-on hostname, not localhost."""
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        return FastMCP("ai-memory", instructions=instructions,
                       transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    except (ImportError, TypeError) as e:  # older/newer mcp without this knob
        log.warning("Could not configure MCP transport security (%s); using defaults", e)
        return FastMCP("ai-memory", instructions=instructions)


def build_mcp(ctx: AppContext) -> FastMCP:
    store = ctx.store
    user = ctx.settings.user_name
    boundary = ctx.settings.day_boundary_hour
    mcp = _make_mcp(f"{ctx.settings.assistant_name}'s notebook: searchable memory of past days, "
                    f"open loops, and durable facts about {user}.")

    @mcp.tool(description=f"Keyword search of {user}'s notebook across all days. Every word in the query "
                          "must appear. Returns the most recent matches in chronological order with entry ids, "
                          "timestamps and types.")
    async def search_notes(query: str, limit: int = 5) -> str:
        hits = store.search(query, limit=max(1, min(int(limit), 20)))
        return _render_entries(hits)

    @mcp.tool(description=f"Read the full notebook log for one day (YYYY-MM-DD). Empty = today; 'yesterday' "
                          f"works too. Days run {boundary:02d}:00 to {boundary:02d}:00.")
    async def read_day(day: str = "") -> str:
        d = (day or "").strip().lower()
        if not d or d == "today":
            d = store.today()
        elif d == "yesterday":
            d = store.previous_day(store.today())
        entries = store.read_day(d)
        head = f"Day {d}: {len(entries)} entries\n"
        text = _render_entries(entries[-60:])
        if len(text) > 12000:
            text = text[-12000:]
        return head + text

    @mcp.tool(description=f"Write something to the notebook now. kind: explicit_memory (default, for things "
                          f"{user} asked you to remember) or exchange (a notable thing from the conversation).")
    async def remember(text: str, kind: str = "explicit_memory") -> str:
        e = store.add_entry(kind if kind in ("explicit_memory", "exchange") else "explicit_memory",
                            text, source="assistant")
        ctx.refresh()
        return f"Noted [{e['id']}] at {e['ts'][11:16]}."

    @mcp.tool(description="Add an open loop / near-future item (task, appointment, pending delivery). Optional "
                          "ISO datetimes for a time window, e.g. 2026-09-14T14:00. Windowed items are flagged "
                          "when the window passes.")
    async def open_loop(text: str, window_start: str = "", window_end: str = "") -> str:
        lp = store.open_loop(text, window_start or None, window_end or None, source="assistant")
        ctx.refresh()
        return f"Opened loop {lp['id']}: {lp['text']}"

    @mcp.tool(description="Close an open loop by id. status: done or dropped.")
    async def close_loop(loop_id: str, status: str = "done", reason: str = "") -> str:
        lp = store.close_loop(loop_id, status=status, reason=reason or None, source="assistant")
        ctx.refresh()
        return f"Closed {lp['id']} as {lp['status']}: {lp['text']}" if lp else f"No open loop with id {loop_id}."

    @mcp.tool(description="List open loops (and optionally recently closed ones).")
    async def list_loops(include_closed: bool = False) -> str:
        active, resolved = store.digest_loops()
        out = [fmt_loop(lp) for lp in active] or ["(no open loops)"]
        if include_closed and resolved:
            out.append("Recently resolved:")
            out += [f"- ({lp['id']}) {lp['text']} [{lp['status']}]" for lp in resolved]
        return "\n".join(out)

    @mcp.tool(description=f"Forget notebook entries by id (get ids from search_notes/read_day). Confirm with "
                          f"{user} first. Entries are hidden immediately and purged after the grace period; "
                          "restore_entry can undo before then.")
    async def forget(entry_ids: List[str], reason: str = "user asked") -> str:
        r = store.forget(list(entry_ids), reason=reason)
        ctx.refresh()
        bits = []
        if r["tombstoned"]:
            bits.append(f"forgot {len(r['tombstoned'])}")
        if r["already"]:
            bits.append(f"{len(r['already'])} already forgotten")
        if r["not_found"]:
            bits.append(f"{len(r['not_found'])} not found")
        if r["stale_summaries"]:
            bits.append(f"summaries to rewrite: {', '.join(r['stale_summaries'])}")
        return "; ".join(bits) or "nothing to do"

    @mcp.tool(description="Undo a forget (only possible during the grace period).")
    async def restore_entry(entry_id: str) -> str:
        ok = store.restore(entry_id)
        ctx.refresh()
        return "Restored." if ok else "Cannot restore (unknown id or already purged)."

    @mcp.tool(description=f"List long-term facts about {user} that are always in the digest.")
    async def list_facts() -> str:
        facts = store.facts()
        return "\n".join(f"- ({f['id']}) {f['text']}" for f in facts) or "(no facts yet)"

    @mcp.tool(description=f"Add a long-term fact directly (only when {user} explicitly says it should be permanent).")
    async def add_fact(text: str) -> str:
        f = store.add_fact(text, source="assistant")
        ctx.refresh()
        return f"Added fact {f['id']}."

    @mcp.tool(description="Remove a long-term fact by id.")
    async def remove_fact(fact_id: str) -> str:
        ok = store.remove_fact(fact_id)
        ctx.refresh()
        return "Removed." if ok else "No such fact."

    @mcp.tool(description=f"List durable-memory candidates proposed by the nightly summary and awaiting "
                          f"{user}'s approval.")
    async def list_candidates() -> str:
        c = store.candidates("pending")
        return "\n".join(f"- ({x['id']}) {x['text']} (from {x['day']})" for x in c) or "(none pending)"

    @mcp.tool(description=f"Promote a pending candidate to a long-term fact. Only on {user}'s say-so.")
    async def approve_candidate(candidate_id: str) -> str:
        c = store.resolve_candidate(candidate_id, approve=True)
        ctx.refresh()
        return f"Approved: {c['text']}" if c else "No such pending candidate."

    @mcp.tool(description="Discard a pending durable-memory candidate.")
    async def reject_candidate(candidate_id: str) -> str:
        c = store.resolve_candidate(candidate_id, approve=False)
        ctx.refresh()
        return f"Rejected: {c['text']}" if c else "No such pending candidate."

    return mcp


# ---------------------------------------------------------------- HTTP
def build_app(ctx: AppContext) -> Starlette:
    store = ctx.store
    mcp = build_mcp(ctx)

    async def _ha_sensor() -> dict:
        """What Home Assistant currently holds for the digest sensor (via the Supervisor proxy)."""
        token = ctx.settings.supervisor_token
        if not token:
            return {"error": "no supervisor token"}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"http://supervisor/core/api/states/{SENSOR_ENTITY}",
                                     headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            return {"error": str(e)}
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        j = r.json()
        attrs = j.get("attributes") or {}
        return {"state": j.get("state"), "prompt_chars": len(attrs.get("prompt_block") or ""),
                "last_updated": j.get("last_updated")}

    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True, "mqtt": bool(ctx.mqtt and ctx.mqtt.connected),
                             "summarizer": ctx.summarizer.enabled, "summarizer_backend": ctx.summarizer.description,
                             "today": store.today(), "entries_today": len(store.today_entries()),
                             "days": len(store.list_days()), "data_dir": ctx.settings.data_dir,
                             "ha_sensor": await _ha_sensor()})

    async def digest_json(_: Request) -> Response:
        return JSONResponse(ctx.last_digest or ctx.build_digest())

    async def ingest(request: Request) -> Response:
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        try:
            entry = await ctx.ingest(payload)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "id": entry["id"], "day": entry["day"]})

    def _prefix(request: Request) -> str:
        return request.headers.get("X-Ingress-Path", "").rstrip("/")

    async def index(request: Request) -> Response:
        return HTMLResponse(render_index(ctx, _prefix(request)))

    def _back(request: Request) -> Response:
        return RedirectResponse(url=(_prefix(request) or "") + "/", status_code=303)

    async def candidate_action(request: Request) -> Response:
        cid, action = request.path_params["cid"], request.path_params["action"]
        store.resolve_candidate(cid, approve=(action == "approve"))
        ctx.refresh()
        return _back(request)

    async def loop_action(request: Request) -> Response:
        lid, action = request.path_params["lid"], request.path_params["action"]
        if action in ("done", "dropped"):
            store.close_loop(lid, status=action, source="ingress")
        elif action == "reopen":
            store.reopen_loop(lid)
        ctx.refresh()
        return _back(request)

    async def loop_new(request: Request) -> Response:
        form = await request.form()
        text = str(form.get("text") or "").strip()
        if text:
            store.open_loop(text, source="ingress")
            ctx.refresh()
        return _back(request)

    async def entry_new(request: Request) -> Response:
        form = await request.form()
        text = str(form.get("text") or "").strip()
        if text:
            await ctx.ingest({"text": text, "type": str(form.get("type") or "explicit_memory"),
                              "source": "ingress"})
        return _back(request)

    async def entry_action(request: Request) -> Response:
        eid, action = request.path_params["eid"], request.path_params["action"]
        if action == "forget":
            store.forget([eid], reason="ingress")
        elif action == "restore":
            store.restore(eid)
        ctx.refresh()
        return _back(request)

    async def fact_new(request: Request) -> Response:
        form = await request.form()
        text = str(form.get("text") or "").strip()
        if text:
            store.add_fact(text, source="ingress")
            ctx.refresh()
        return _back(request)

    async def fact_action(request: Request) -> Response:
        if request.path_params["action"] == "remove":
            store.remove_fact(request.path_params["fid"])
            ctx.refresh()
        return _back(request)

    async def action(request: Request) -> Response:
        what = request.path_params["what"]
        if what == "refresh":
            ctx.refresh(force=True)
        elif what == "rollover" and ctx.scheduler is not None:
            await ctx.scheduler.tick(force=True)
        return _back(request)

    routes = [
        Route("/", index),
        Route("/health", health),
        Route("/digest", digest_json),
        Route("/ingest", ingest, methods=["POST"]),
        Route("/candidates/{cid}/{action}", candidate_action, methods=["POST"]),
        Route("/loops/new", loop_new, methods=["POST"]),
        Route("/loops/{lid}/{action}", loop_action, methods=["POST"]),
        Route("/entries/new", entry_new, methods=["POST"]),
        Route("/entries/{eid}/{action}", entry_action, methods=["POST"]),
        Route("/facts/new", fact_new, methods=["POST"]),
        Route("/facts/{fid}/{action}", fact_action, methods=["POST"]),
        Route("/actions/{what}", action, methods=["POST"]),
        Mount("/", app=mcp.sse_app()),
    ]
    return Starlette(routes=routes)
