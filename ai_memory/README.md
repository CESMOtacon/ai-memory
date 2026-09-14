# AI Memory add-on

Persistent memory for a Home Assistant LLM voice assistant. Architecture in `../DESIGN.md`.

## Options

| Option | Meaning |
|---|---|
| `assistant_name` | How the assistant is referred to in prompts and the UI (e.g. "Des"). |
| `user_name` | How you are referred to in prompts (e.g. "Evan"). |
| `summarizer_backend` | `ai_task` (default, provider agnostic via Home Assistant's AI Task service), `anthropic` (direct API), or `off`. |
| `ai_task_entity` | The AI Task entity to use, e.g. `ai_task.claude_ai_task`, `ai_task.openai_ai_task`, `ai_task.google_ai_task`. |
| `anthropic_api_key` / `anthropic_model` | Only for the `anthropic` backend. |
| `day_boundary_hour` | When a "day" rolls over (default 05:00, so a 1 AM chat belongs to the evening before). |
| `forget_grace_days` | Forgotten entries stay recoverable this long, then are physically purged. |
| `timezone` | Defaults to Home Assistant's timezone. |

## Surfaces

- **Ingest**: MQTT topic `ai_memory/ingest` (JSON: `type`, `text`, optional `source`, `conversation_id`)
  or `POST http://local-ai-memory:8099/ingest`. Types: `morning_announcement`, `announcement`, `event`,
  `explicit_memory`, `exchange`, `system`.
- **Digest sensor**: `sensor.ai_memory_digest` via MQTT discovery; attribute `prompt_block` is the text to
  put in your agent's prompt: `{{ state_attr('sensor.ai_memory_digest', 'prompt_block') }}`.
- **MCP tools**: SSE endpoint `http://local-ai-memory:8099/sse` for the Model Context Protocol integration.
  Tools: search_notes, read_day, remember, open_loop, close_loop, list_loops, forget, restore_entry,
  list_facts, add_fact, remove_fact, list_candidates, approve_candidate, reject_candidate.
- **Status page**: the add-on's ingress panel (digest, open loops, candidates, facts, entries, forgotten).
- **Commands**: MQTT topic `ai_memory/command` with `{"command": "refresh" | "rollover" | "tick"}`.

## Data

The notebook lives in the add-on config folder (`/addon_configs/local_ai_memory` on the host): one
JSON-lines file per day under `days/`, plus `summaries/`, `rolling/`, `loops.json`, `facts.json`,
`candidates.json`. The day log is canonical; everything else is derived and rebuilt as needed.
Versions before 0.2 stored data in `/data`; it is migrated automatically on first start.
