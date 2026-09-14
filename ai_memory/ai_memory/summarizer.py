"""Summarization: nightly day summary and rolling today summary.

Two backends:
  ai_task    - calls Home Assistant's ai_task.generate_data service, so whatever AI Task entity the
               user picks (Anthropic, OpenAI, Google, Ollama, ...) does the work. Provider agnostic.
  anthropic  - calls the Anthropic API directly with an API key from the add-on options.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

import httpx

from .config import SUPERVISOR, Settings

log = logging.getLogger(__name__)


@dataclass
class DaySummaryResult:
    yesterday_paragraph: str
    loops_to_open: List[str] = field(default_factory=list)
    loops_to_close: List[str] = field(default_factory=list)
    durable_memory_candidates: List[str] = field(default_factory=list)


def day_instructions(assistant: str, user: str) -> str:
    return f"""You maintain the memory notebook for {assistant}, a home voice assistant used by {user}.
You are given the raw notebook log for one day (chronological, with timestamps and entry types)
and the list of currently open loops (each with an id). Produce:

1. yesterday_paragraph: one paragraph, under 120 words, third person, chronological, describing
   the shape of the day. Leave breadcrumbs: use concrete nouns and names (products, people, places,
   projects) so that a later keyword search of the raw log will find them. Never write vague phrases
   like "discussed various topics". If the day was quiet, say so briefly.
2. loops_to_open: concrete pending things with a future component (appointments, deliveries, tasks
   {user} said they would do) that are not already open. Short imperative phrases. Usually empty.
3. loops_to_close: ids of open loops the log clearly shows were completed or abandoned. Only ids.
4. durable_memory_candidates: at most 5 standalone facts that would still be true in a month:
   stable preferences, people and relationships, ongoing projects, recurring schedules, skills or
   history {user} shared. Never one-off events, moods, meals, or anything said in passing once.
   Phrase each as a complete sentence about {user}. Prefer an empty list over a weak candidate."""


def rolling_instructions(assistant: str, user: str) -> str:
    return f"""You maintain a running summary of today for {assistant}, a home voice assistant used by {user}.
You get the existing running summary (may be empty) and new raw notebook entries since it was written.
Fold the new entries into an updated running summary: chronological, third person, under 150 words,
concrete nouns and names kept as breadcrumbs, no filler. Return only the summary text."""


DAY_STRUCTURE = {
    "yesterday_paragraph": {"description": "One paragraph, under 120 words, describing the day",
                            "required": True, "selector": {"text": {}}},
    "loops_to_open": {"description": "New open loops to create (short phrases); usually empty",
                      "required": False, "selector": {"text": {"multiple": True}}},
    "loops_to_close": {"description": "Ids of existing open loops that were completed or abandoned",
                       "required": False, "selector": {"text": {"multiple": True}}},
    "durable_memory_candidates": {"description": "At most 5 durable facts about the user, full sentences",
                                  "required": False, "selector": {"text": {"multiple": True}}},
}


def _entries_text(entries: List[dict]) -> str:
    lines = []
    for e in entries:
        stamp = e.get("ts", "")[:16].replace("T", " ")
        lines.append(f"{stamp} | {e.get('type', '?')} | {e.get('text', '')}")
    return "\n".join(lines) if lines else "(no entries)"


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split("\n") if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


class Summarizer:
    def __init__(self, settings: Settings):
        self.s = settings
        self.backend = settings.summarizer_backend
        self._anthropic = None
        if self.backend == "anthropic" and settings.anthropic_api_key:
            import anthropic  # imported lazily so the ai_task backend needs no SDK
            self._anthropic = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    @property
    def enabled(self) -> bool:
        if self.backend == "ai_task":
            return bool(self.s.ai_task_entity and self.s.supervisor_token)
        if self.backend == "anthropic":
            return self._anthropic is not None
        return False

    @property
    def description(self) -> str:
        if self.backend == "ai_task":
            return f"AI Task via {self.s.ai_task_entity}" if self.s.ai_task_entity else "AI Task (no entity set)"
        if self.backend == "anthropic":
            return f"Anthropic {self.s.anthropic_model}" + ("" if self._anthropic else " (no API key)")
        return "off"

    # ------------------------------------------------------------- public
    async def summarize_day(self, day: str, entries: List[dict], open_loops: List[dict]) -> DaySummaryResult:
        loops_text = "\n".join(f"- ({lp['id']}) {lp['text']} [{lp['status']}]" for lp in open_loops) or "(none)"
        user = f"Day: {day}\n\nOpen loops:\n{loops_text}\n\nRaw log:\n{_entries_text(entries)}"
        instr = day_instructions(self.s.assistant_name, self.s.user_name)
        if self.backend == "ai_task":
            data = await self._ai_task("ai_memory_day_summary", instr + "\n\n" + user, DAY_STRUCTURE)
            if not isinstance(data, dict):
                raise RuntimeError(f"AI Task returned non-structured data: {str(data)[:200]}")
            return DaySummaryResult(
                yesterday_paragraph=str(data.get("yesterday_paragraph") or "").strip(),
                loops_to_open=_as_list(data.get("loops_to_open")),
                loops_to_close=_as_list(data.get("loops_to_close")),
                durable_memory_candidates=_as_list(data.get("durable_memory_candidates")),
            )
        return await asyncio.to_thread(self._anthropic_day, instr, user)

    async def summarize_rolling(self, day: str, prior: str, new_entries: List[dict]) -> str:
        user = (f"Day: {day}\n\nExisting running summary:\n{prior.strip() or '(empty)'}\n\n"
                f"New entries:\n{_entries_text(new_entries)}")
        instr = rolling_instructions(self.s.assistant_name, self.s.user_name)
        if self.backend == "ai_task":
            data = await self._ai_task("ai_memory_rolling_summary", instr + "\n\n" + user)
            return str(data).strip()
        return await asyncio.to_thread(self._anthropic_rolling, instr, user)

    # ------------------------------------------------------------ ai_task
    async def _ai_task(self, task_name: str, instructions: str, structure: Optional[dict] = None) -> Any:
        body: dict = {"task_name": task_name, "instructions": instructions, "entity_id": self.s.ai_task_entity}
        if structure:
            body["structure"] = structure
        async with httpx.AsyncClient(timeout=240) as client:
            r = await client.post(SUPERVISOR + "/core/api/services/ai_task/generate_data?return_response",
                                  json=body, headers={"Authorization": f"Bearer {self.s.supervisor_token}"})
        if r.status_code != 200:
            raise RuntimeError(f"ai_task.generate_data HTTP {r.status_code}: {r.text[:300]}")
        return (r.json().get("service_response") or {}).get("data")

    # ---------------------------------------------------------- anthropic
    def _anthropic_day(self, instr: str, user: str) -> DaySummaryResult:
        from pydantic import BaseModel, Field

        class DaySummary(BaseModel):
            yesterday_paragraph: str = Field(description="One paragraph, under 120 words")
            loops_to_open: List[str] = Field(default_factory=list)
            loops_to_close: List[str] = Field(default_factory=list, description="loop ids only")
            durable_memory_candidates: List[str] = Field(default_factory=list)

        response = self._anthropic.messages.parse(
            model=self.s.anthropic_model, max_tokens=4000, system=instr,
            messages=[{"role": "user", "content": user}], output_format=DaySummary)
        p = response.parsed_output
        if p is None:
            raise RuntimeError(f"No parsed output (stop_reason={response.stop_reason})")
        return DaySummaryResult(p.yesterday_paragraph, list(p.loops_to_open), list(p.loops_to_close),
                                list(p.durable_memory_candidates))

    def _anthropic_rolling(self, instr: str, user: str) -> str:
        response = self._anthropic.messages.create(
            model=self.s.anthropic_model, max_tokens=1500, system=instr,
            output_config={"effort": "low"}, messages=[{"role": "user", "content": user}])
        for block in response.content:
            if block.type == "text":
                return block.text.strip()
        raise RuntimeError(f"No text in response (stop_reason={response.stop_reason})")
