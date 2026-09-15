# AI Memory for Home Assistant

Give your Home Assistant voice assistant a memory that survives between conversations, so an
automation can say something, and hours later you can casually refer back to it from a smart
speaker and be understood.

## What it feels like

You tell the assistant you're heading out for a hike. Later, your welcome-home automation fires and
the assistant greets you with "back from the hike, then?" before running through what it normally
says. You reply from the kitchen speaker, which is a brand-new voice session with no history, and
follow up on something the automated announcement mentioned. The assistant knows exactly what
you're referring to, answers in the context of that announcement, and quietly closes the "out on a
hike" item it had been tracking.

None of that works out of the box. Home Assistant drops a conversation after a few minutes of
silence, every satellite starts a fresh session, and an automation's announcement is never part of
any conversation at all. Each interaction is an island.

## How it works

The add-on keeps a notebook outside Home Assistant's conversation system. Announcements, notable
exchanges and explicit "remember this" requests are written to it. On every turn, a fixed-size
digest of the notebook is injected into the assistant's prompt: durable facts about you, open loops
(things with a future: a pickup window, a pending install, "out on a hike"), a one-paragraph
summary of yesterday, and today so far. The assistant also gets tools to search the full archive,
open and close loops, and forget things on request. Each night the day is condensed into the
"yesterday" paragraph that carries forward, and durable facts are proposed for your approval rather
than remembered automatically.

Works with any LLM conversation integration that renders a prompt template (Anthropic, OpenAI,
Google, Ollama and others). Summaries run through Home Assistant's own AI Task service, so no extra
API keys are needed.

Requires the Mosquitto broker add-on (or any MQTT broker configured in Home Assistant).

## 1. Install the add-on

1. Settings > Add-ons > Add-on store > three-dot menu > Repositories > add
   `https://github.com/CESMOtacon/ai-memory`.
2. Install **AI Memory**. In its Configuration tab set:
   - `assistant_name`: what you call your assistant (used in prompts and the status page).
   - `user_name`: your name, as the assistant should refer to you.
   - `summarizer_backend`: `ai_task` (recommended). Set `ai_task_entity` to any AI Task entity you
     already have, e.g. `ai_task.claude_ai_task`, `ai_task.openai_ai_task`, `ai_task.google_ai_task`.
     No API key is needed in the add-on; it goes through Home Assistant.
3. Start it. Within a few seconds `sensor.ai_memory_digest` appears with a `prompt_block` attribute.
   The add-on's **Open Web UI** shows the digest exactly as your assistant will see it.

## 2. Wire the digest into your assistant's instructions

Open your conversation agent's settings (for example Settings > Devices & services > Anthropic >
your conversation entry > Configure) and add the following to the **end** of the instructions.
Keep it last: the text above it stays stable, which is what makes prompt caching work.

```
Memory: the notebook digest below is what you know about {user}'s recent days and what is still open.
Treat it as your own memory. Draw on it when it is relevant to what {user} says or to the moment,
without reciting it or bringing it up unprompted. If {user} tells you they are heading out to do
something, open a loop for it with open_loop; close it when they are back.

{{ state_attr('sensor.ai_memory_digest', 'prompt_block') }}
```

Replace `{user}` with your name. The first paragraph is deliberately passive: it tells the assistant
the digest is its own memory, without telling it to bring the notes up. That keeps ordinary voice
commands ("turn off the lights") from turning into a recap of your day. The second line is the digest
itself, rendered fresh by Home Assistant on every request.

Test: ask the assistant "what's in your notes?" It should describe the digest.

## 3. Give the assistant the notebook tools

1. Settings > Devices & services > Add integration > **Model Context Protocol**.
   Server URL: `http://local-ai-memory:8099/sse`
2. Back in your conversation agent's settings, under the LLM API / "Control Home Assistant"
   selection, tick the new **ai-memory** API alongside Assist.

The assistant now has: `search_notes`, `read_day`, `remember`, `open_loop`, `close_loop`,
`list_loops`, `forget`, `restore_entry`, `list_facts`, `add_fact`, `remove_fact`,
`list_candidates`, `approve_candidate`, `reject_candidate`.

If your integration offers a "tool search" option that defers tools, consider turning it off. With
it on, the assistant has to search for the notebook tools before it can use them and, at low effort
settings, sometimes doesn't bother.

## 4. Make announcements part of the memory

Announcement automations (morning briefing, welcome home, "the dishwasher is done", weather alerts)
usually call `conversation.process` and speak the result. Two changes make them continuous with
everything else the assistant knows.

### 4a. Write what was said to the notebook

After the `conversation.process` step, publish its response to the ingest topic:

```yaml
- action: conversation.process
  data:
    agent_id: conversation.your_agent
    text: "You are giving {user} their morning briefing. ..."
  response_variable: briefing
- action: mqtt.publish
  data:
    topic: ai_memory/ingest
    payload: >-
      {{ {"type": "morning_announcement",
          "source": "automation.morning_briefing",
          "text": briefing.response.speech.plain.speech} | to_json }}
```

Put the publish step right after `conversation.process`, before the speaker step, so the note is
written even if playback fails. `type` is one of `morning_announcement`, `announcement`, `event`,
`explicit_memory`, `exchange`, `system`. `source` is free text; use the automation's name.

Non-LLM events work too. A doorbell automation can publish
`{"type": "event", "source": "automation.vehicle_approaching", "text": "Vehicle approaching the house."}`.

### 4b. Tell the announcement prompt to use the notes

The digest is already in the assistant's instructions, but an announcement prompt is usually a
checklist ("mention the forecast, read the to-do list, remind me about X") and a checklist crowds
out anything it doesn't name. Add one active sentence to each announcement's `text`. Three
variants, strongest first:

**Welcome home** (context matters most here):

> Before you speak, check your notebook digest for what {user} has been doing today and anything
> still open. If the notes show where they were or what they were doing before arriving,
> acknowledge it naturally, the way someone who was home would. Do not recite the notes.

**Morning briefing** (continuity with yesterday):

> Before you speak, check your notebook digest. Reference anything from yesterday that carries into
> today, and any open loops, so this feels like continuity rather than a fresh start. Do not recite
> the notes.

**Everything else** (appliance done, sauna ready, weather alert):

> Before you speak, glance at your notebook digest for what {user} has been doing today and
> anything open. Weave in anything genuinely relevant to this moment, otherwise ignore it. Do not
> recite the notes.

"Do not recite the notes" matters. Without it the assistant tends to read the digest back.

## 5. Keep it cheap: prompt caching

Your agent's request is mostly a large, stable prefix: tool schemas, Home Assistant's exposed-entity
overview, your instructions, and this digest. With Anthropic that prefix is 15 to 25k tokens. Turn on
your integration's prompt caching (Anthropic: "System prompt"). The prefix is then written once and
read at a tenth of the price on every following call while the cache is warm.

The add-on is built to keep that cache warm. The digest is deterministic: it carries no generation
timestamp, and the sensor is only republished when the digest's bytes actually change. A quiet
five-minute heartbeat leaves the prompt byte-identical, so the cache survives. A real memory change,
a new announcement, a loop opened or closed, a "remember this," does update the prompt and
invalidate the cache, which is the trade-off you want.

Two more lines for your instructions that pay for themselves on Home Assistant:

> Live data: when you call GetLiveContext, always pass the narrowest filter that fits: the entity
> name when you know it, otherwise a single domain or an area. Never call GetLiveContext without a
> filter.

Unfiltered, that tool returns every exposed entity with its state, which was 10k tokens on a home
with 300 exposed entities, on every "is the door locked" question.

The current clock is deliberately not in the digest, because it would change every minute and
break the cache. Entries carry their own timestamps, and the assistant gets the time from Home
Assistant's GetDateTime tool when it needs it, so "how long ago did I get home" still works.

## 6. Day to day

- **Open loops** are things with a future: "grocery pickup 2 to 3", "PSU arrived, install pending".
  The assistant opens and closes them with tools; you can also manage them on the status page. They
  stay in the digest until closed and never get summarized away.
- **Durable memory candidates**: each night the summarizer proposes facts that would still be true
  in a month. Nothing is promoted automatically. Approve or reject them on the status page or by
  voice ("what memory candidates are waiting?").
- **Forgetting**: "forget what I told you about X" makes the assistant search, confirm, and tombstone
  the entries. They vanish from every view immediately, stay recoverable for the grace period
  (default 7 days), then are physically purged.
- **Day boundary**: a "day" runs 05:00 to 05:00 by default, so a 1 AM conversation belongs to the
  evening before. Change `day_boundary_hour` if your days look different.
- **Debugging an announcement**: Settings > Voice assistants > your assistant > three-dot menu >
  Debug shows the full text the assistant received and what it answered.

## Where the data lives

`/addon_configs/local_ai_memory/` (visible in the file editor and included in backups): one
JSON-lines file per day under `days/`, plus `summaries/`, `rolling/`, `loops.json`, `facts.json`,
`candidates.json`. The day log is canonical; everything else is derived and rebuilt as needed.

See `DESIGN.md` for the architecture and `ai_memory/README.md` for the full option and endpoint
reference.
