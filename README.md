# AI Memory for Home Assistant

Persistent memory, open loops and a searchable notebook for any Home Assistant LLM voice assistant
(Anthropic, OpenAI, Google, Ollama, or anything else that renders a prompt template).

Home Assistant's conversation history expires after a few minutes. This add-on keeps memory outside
that: every announcement and notable exchange is written to a notebook, a fixed-size digest is
injected into your assistant's prompt on every turn, and the assistant gets tools to search the
full archive, track open loops, and forget things on request.

## Install

1. Settings > Add-ons > Add-on store > three-dot menu > Repositories > add this repo's URL.
2. Install **AI Memory**, set the options (assistant name, user name, which AI Task entity to use
   for summaries), start it.
3. Add this line at the end of your conversation agent's instructions:
   `{{ state_attr('sensor.ai_memory_digest', 'prompt_block') }}`
4. Optional but recommended: add the **Model Context Protocol** integration with URL
   `http://local-ai-memory:8099/sse` and enable it in your agent's settings, so the assistant gets
   the notebook tools.
5. Have your announcement automations publish to MQTT topic `ai_memory/ingest` with a JSON payload
   `{"type": "announcement", "source": "automation.x", "text": "..."}`.

Requires the Mosquitto broker add-on (or any MQTT broker configured in Home Assistant).

See `DESIGN.md` for the architecture and `ai_memory/README.md` for the add-on details.
