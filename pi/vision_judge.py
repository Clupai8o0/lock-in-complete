"""Gemini-powered focus judge.

Sends a JPEG plus a strict prompt to the Gemini API and returns a parsed
FocusJudgment. Any deviation from the schema (or any network error) returns
None — the orchestrator treats that as "no opinion this cycle" and falls back
to sensor-only logic, which is the deliberate graceful-degradation path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

log = logging.getLogger("lockin.vision")

PROMPT = """You are a focus judge for a study/deep-work coaching system.

You will see ONE still photo of someone at their desk. You only see a single
frozen moment — you cannot see motion, blinks, or what happened a second
before or after. Be conservative: when in doubt, the person is focused.

Output ONLY a single JSON object with this exact schema and nothing else
(no prose, no markdown fences):

{
  "focused": true | false,
  "confidence": number between 0.0 and 1.0,
  "observation": "short factual description, max 15 words"
}

DEFAULT IS focused=true. Anyone sitting at the desk facing the screen, with
hands near the keyboard or notes, is focused — even if their expression looks
tired, neutral, or their eyes are partly/fully closed in this single frame.
A still photo cannot distinguish a blink from sleep. Do not infer sleep from
eyes alone.

Examples of focused=true (do NOT mark these as distracted):
- looking at the monitor (this is the default working posture)
- reading, writing, typing, thinking
- briefly glancing down at the keyboard, notes, or off to the side
- eyes appear closed or squinting in a single frame while otherwise upright
- neutral or tired expression while at the desk
- hand near face, leaning on hand, adjusting glasses, scratching
- drinking from a cup or bottle
- screen visible but person glancing away momentarily

Mark focused=false ONLY when the image UNAMBIGUOUSLY shows one of:
- holding or actively looking at a phone or handheld game device
- head fully resting on the desk surface (face-down or cheek-down on desk)
- clearly slumped over with torso collapsed onto the desk
- not at the desk / chair empty / person fully out of frame
- talking face-to-face with another person who is also visible in frame
- eating a meal (plate of food, utensils, actively chewing visible)
- using an obvious non-work device (game controller, TV remote)

Tie-breaker: if you are unsure whether one of the above applies, choose
focused=true with confidence < 0.6 rather than focused=false. The system
already has hardware sensors for desk presence, so you do not need to be
strict about presence — focus on the unambiguous distractions above.

- "confidence" reflects how clear the signal is. Use < 0.5 when uncertain.
- "observation" must be a single short sentence, factual, no judgement words.
"""

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass
class FocusJudgment:
    focused: bool
    confidence: float
    observation: str

    @classmethod
    def parse(cls, text: str) -> "FocusJudgment | None":
        if not text:
            return None
        candidate = text.strip()
        m = _JSON_FENCE_RE.search(candidate)
        if m:
            candidate = m.group(1)
        else:
            # find first '{' and last '}'
            i, j = candidate.find("{"), candidate.rfind("}")
            if i < 0 or j < 0 or j <= i:
                return None
            candidate = candidate[i : j + 1]
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        if "focused" not in obj or not isinstance(obj["focused"], bool):
            return None
        try:
            confidence = float(obj.get("confidence", 0.0))
        except (TypeError, ValueError):
            return None
        confidence = max(0.0, min(1.0, confidence))
        observation = str(obj.get("observation", ""))[:200].strip()
        if not observation:
            observation = "no observation"
        return cls(focused=obj["focused"], confidence=confidence, observation=observation)


class VisionJudge:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is empty")
        # google-genai SDK (the current Gemini SDK)
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore

        self._client = genai.Client(api_key=api_key)
        self._types = types
        self.model = model
        self.last_error: str | None = None
        self.last_judgment: FocusJudgment | None = None

    def _judge_sync(self, jpeg: bytes) -> FocusJudgment | None:
        try:
            part = self._types.Part.from_bytes(data=jpeg, mime_type="image/jpeg")
            cfg = self._types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=256,
                # gemini-2.5-flash is a thinking model; thinking tokens count
                # against max_output_tokens, so leaving it on truncates the
                # actual JSON answer to fragments like "Here is the JSON requested:".
                thinking_config=self._types.ThinkingConfig(thinking_budget=0),
            )
            resp = self._client.models.generate_content(
                model=self.model,
                contents=[PROMPT, part],
                config=cfg,
            )
            text = getattr(resp, "text", None) or ""
            judgment = FocusJudgment.parse(text)
            if not judgment:
                self.last_error = f"unparseable: {text[:120]!r}"
                log.warning(self.last_error)
                return None
            self.last_error = None
            self.last_judgment = judgment
            return judgment
        except Exception as e:  # network errors, API errors, etc.
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("gemini call failed: %s", self.last_error)
            return None

    async def judge(self, jpeg: bytes, *, timeout_s: float = 20.0) -> FocusJudgment | None:
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._judge_sync, jpeg),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            self.last_error = "timeout"
            log.warning("gemini timeout after %.1fs", timeout_s)
            return None
