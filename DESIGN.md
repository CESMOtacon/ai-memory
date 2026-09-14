# AI Memory — Design Spec

Persistent memory and context for the Home Assistant voice assistant ("Des").
HA's own conversation history expires after a few minutes of inactivity (hard-coded),
so memory lives outside HA in a dedicated add-on. The log is canonical; everything
else is derived and rebuildable.

## Locked decisions

| Decision | Value |
|---|---|
| Host | HAOS with the Mosquitto add-on |
| Voice model | Opus 5, effort low, thinking on (never disabled) |
| Summarizer | HA AI Task service (provider agnostic; Evan uses ai_task.claude_ai_task), or direct Anthropic |
| Transport HA <-> add-on | MQTT (retained) for sensor + ingest; HTTP for MCP tools |
| Des day boundary | 05:00 local. Day key = local date of (ts - 5h). Used everywhere from v1. |
| Forget grace period | 7 days tombstoned-but-recoverable, then physical purge |
| Durable memory promotion | Manual approval by default; auto-promotion behind a config flag, off |
| Storage | JSON Lines, one file per Des-day, in the add-on config folder (/addon_configs/local_ai_memory) |

## Components

1. **Add-on `ai_memory`** (local add-on, Python). Ingests entries, stores the archive,
   builds the digest, runs the rolling and nightly summaries, serves the MCP tools,
   serves a small status/approval page via ingress.
2. **Sensor `sensor.ai_memory_digest`** via MQTT discovery, retained. State = ISO
   timestamp of last rebuild. Attributes = the digest sections as text.
3. **Prompt line** in the Anthropic integration instructions template that renders the
   digest attributes. Rendered by HA on every request.
4. **MCP server** inside the add-on, connected via HA's Model Context Protocol
   integration. Exposes tools to Des.

## Data model

### Archive entry (`days/<daykey>.jsonl`, append-only)

```json
{"id": "01J...", "ts": "2026-09-14T08:15:00-05:00", "day": "2026-09-14",
 "type": "morning_announcement", "source": "script.morning_announcement",
 "conversation_id": "2026-09-14-morning", "text": "..."}
```

Types (v1): `morning_announcement`, `explicit_memory`, `exchange`, `loop_opened`,
`loop_closed`, `tombstone`, `system`.

Rendered to Des (search / read-day) as: `2026-09-14 08:15 | morning_announcement | text`,
oldest first.

### Open loops (`loops.json`)

```json
{"id": "...", "text": "Grocery pickup", "created": "...", "window": {"start": "...", "end": "..."},
 "status": "open|done|dropped|expired", "closed": null, "closed_reason": null, "source_entry": "01J..."}
```

- Enter via: explicit request, summarizer proposal, automation.
- Exit via: `close_loop` tool, or window expiry (flagged for confirmation, not silently dropped).
- Closed items remain in the digest for one Des-day as "recently resolved", then drop.

### Long-term facts (`facts.json`)

Short curated list. Each fact has id, text, added date, source (approved candidate id or manual).

### Durable candidates (`candidates.json`)

Output of the nightly process. Status `pending|approved|rejected`. Approved -> facts.

### Day summary (`summaries/<daykey>.json`)

```json
{"day": "...", "generated": "...", "stale": false, "input_entry_ids": ["..."],
 "yesterday_paragraph": "...", "loop_proposals": [...], "durable_candidates": [...]}
```

`input_entry_ids` lets a forget operation detect contaminated summaries and mark them stale.

### Tombstone (appended to the day file of the *target* entry)

```json
{"id": "...", "ts": "...", "type": "tombstone", "target": "01J...", "reason": "user_request",
 "purge_after": "2026-09-21T..."}
```

## Digest (fixed ceiling ~1900 tokens)

| Section | Source | Cap |
|---|---|---|
| Long-term facts | facts.json | ~300 |
| Open loops | loops.json (open + recently resolved) | ~300 |
| Yesterday | most recent completed day's summary paragraph, else fallback to last few raw entries | ~300 |
| Today | rolling summary + last N raw entries of current Des-day | ~1000 |

Order in the prompt: facts, loops, yesterday, today (most volatile last, for cache-prefix stability).
If a section exceeds its cap it is compressed before publishing; it never grows past the cap.

## Processes

- **Ingest**: MQTT topic `ai_memory/ingest` (JSON payload) or HTTP `POST /ingest`.
  Assigns id/ts/day, appends to day file, triggers digest rebuild.
- **Digest rebuild**: pure function of (facts, loops, latest summary, today's entries, tombstones).
  Runs on every write, on startup, and on MQTT reconnect. Publishes retained.
- **Rolling today summary**: when today's raw entries exceed a threshold, fold older entries
  into a running summary; keep the last N verbatim.
- **Rollover (idempotent, not clock-bound)**: every few minutes and on startup, for every
  completed Des-day with entries and no non-stale summary file, generate the summary
  (oldest first). Midnight/05:00 is not a transactional boundary. Failed API calls retry;
  the digest falls back to raw entries meanwhile.
- **Forget**: Des searches, reports matches, user confirms, tombstone appended.
  Immediately excluded from search, read-day, digest, summaries, and candidates.
  Any summary whose `input_entry_ids` includes the target is marked stale and regenerated.
  Loops/facts/candidates referencing the entry are surfaced for review.
- **Purge**: daily pass. For tombstones past `purge_after`, rewrite the day file with the
  target entry's text replaced by `"[purged]"` and remove any recoverable copies the add-on
  controls (stale summaries, cached renders). Tombstone record itself stays (id only).
- **Immediate permanent forget** (not v1): same path with `purge_after = now`; the code
  path must exist as a parameter from v1 so it is a flag flip later.

## MCP tools (v1)

- `search_notes(query, limit=5)` — keyword match over non-tombstoned entries, chronological.
- `read_day(daykey)` — full non-tombstoned log for one Des-day, capped.
- `remember(text)` — explicit_memory entry.
- `open_loop(text, window?)`, `close_loop(id, reason)`
- `forget(entry_ids)` — after confirmation in conversation.
- `list_candidates()`, `approve_candidate(id)`, `reject_candidate(id)`

Prompt instruction for Des: if the digest mentions something and detail is needed, search before answering.

## Build order

1. Add-on skeleton: ingest (MQTT + HTTP), day files, digest rebuild, MQTT discovery sensor.
2. Verify sensor appears in HA with test text.
3. Prompt line added; ask Des what is in its notes. (Proof point.)
4. Announcement script publishes its text to the ingest topic.
5. Nightly summary + idempotent rollover + candidates queue.
6. Rolling today summary.
7. MCP server + tools; HA MCP integration pointed at it.
8. Forget + tombstone + purge.

Steps 3 and 7 require HA UI changes (integration options) and are done by Evan.

## Status (2026-09-14)

Add-on `local_des_memory` built, installed and running on HA. Verified: MQTT connected, digest sensor
`sensor.ai_memory_digest` registered and holding `prompt_block`, HTTP ingest, ingress status page,
MCP handshake with all 14 tools, search returning the test entry. Summarizer disabled until an API key
is entered. Prompt line, morning-briefing MQTT publish and the MCP integration were applied 2026-09-14 via
supported mechanisms (see HA_SETUP.md). Only the Anthropic API key in the add-on options remains.

Renamed to AI Memory on 2026-09-14 with assistant/user names as options, AI Task summarizer backend,
and the repo laid out as a Home Assistant add-on repository (repository.yaml + ai_memory/).
