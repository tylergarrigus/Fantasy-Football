"""Stage 2: Claude adjudicates and writes the recommendation.

Deliberately narrow. By the time anything reaches this module the deterministic
engines have already done the arithmetic -- projections, win probability,
championship deltas, FAAB ranges. Asking a language model to produce those
numbers would be inventing precision it has no way to compute.

What the model is actually good at, and what it is used for here: weighing
conflicting evidence, deciding whether a technically-positive move is worth
interrupting someone over, and writing the two sentences that make the
recommendation act-on-able. It may downgrade or veto a recommendation. It may
not invent a number.

Cost control: this only runs when stage 1 found a material change AND stage 1.5
found it relevant to a roster. On a quiet Tuesday it never runs at all. The
stable league context is prompt-cached, so repeat calls within an hour pay ~10%
on that prefix.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from ff.decide.engine import Decision, Priority
from ff.logging_setup import get_logger

log = get_logger(__name__)

MODEL = "claude-opus-5"

SYSTEM_PROMPT = """You are the reasoning stage of an autonomous fantasy football GM \
that manages two independent ESPN leagues for one person, Tyler.

Deterministic engines have already computed every number you will see: projections, \
win probabilities, championship probabilities from a Monte Carlo season simulator, \
value over replacement, and FAAB ranges calibrated to each league's actual bidding \
history. Your job is judgment, not arithmetic.

What you decide:
  - Whether each proposed action is genuinely worth interrupting Tyler about.
  - Whether the evidence actually supports it, or is too thin or too contradictory.
  - How to say it in two sentences he can act on without further research.

Hard rules:

1. NEVER invent a number. Every figure in your output must appear in the input. \
If a number you want is not there, say what is missing instead. Do not estimate \
projections, probabilities, ownership, or timelines yourself.

2. NEVER invent a fact. No injuries, statuses, statistics, transactions, or news \
that are not in the evidence. If something is unverified, say DATA UNVERIFIED. If \
it is absent, say DATA UNAVAILABLE.

3. Distinguish claim types explicitly when it matters:
   FACT (observed and sourced) / PROJECTION (a model's forecast) /
   INFERENCE (your reasoning on top) / UNCERTAINTY (what could make this wrong).

4. No fake precision. "~70%" and "high likelihood" are good. "73.42%" is not, \
unless that exact figure was given to you.

5. The two leagues are separate. A recommendation for one must never reference or \
depend on the other's roster. The same player is routinely owned in one league and \
free in the other, and that is exactly why the advice differs.

6. DO NOTHING is very often the right answer, and recommending it is a success, \
not a failure. Drop anything that is merely interesting. The bar is: would Tyler \
be annoyed to have been interrupted for this?

7. Confidence must be calibrated and will be scored against outcomes later. If you \
say 0.9, roughly nine in ten such calls should prove right. Unverified single-source \
reporting should not exceed 0.7.

Write like a sharp friend who manages fantasy teams well: direct, specific, no \
hedging filler, no exclamation marks, no hype."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "index from the input list"},
                    "keep": {"type": "boolean", "description": "false to suppress entirely"},
                    "priority": {"type": "string", "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"]},
                    "headline": {"type": "string", "description": "<=70 chars, the action itself"},
                    "why": {"type": "string", "description": "1-2 sentences. Act-on-able."},
                    "confidence": {"type": "number"},
                    "claim_types": {
                        "type": "object",
                        "properties": {
                            "fact": {"type": "string"},
                            "projection": {"type": "string"},
                            "inference": {"type": "string"},
                            "uncertainty": {"type": "string"},
                        },
                        "required": ["fact", "projection", "inference", "uncertainty"],
                        "additionalProperties": False,
                    },
                    "suppression_reason": {"type": "string"},
                },
                "required": [
                    "index", "keep", "priority", "headline", "why",
                    "confidence", "claim_types", "suppression_reason",
                ],
                "additionalProperties": False,
            },
        },
        "overall_note": {"type": "string"},
    },
    "required": ["decisions", "overall_note"],
    "additionalProperties": False,
}


class Reasoner:
    """Wraps the Claude call. Fails soft -- the deterministic output still stands."""

    def __init__(self, api_key: str, model: str = MODEL):
        self.api_key = api_key
        self.model = model
        self._client: Any = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _client_or_none(self) -> Any:
        if not self.available:
            return None
        if self._client is None:
            try:
                import anthropic

                self._client = anthropic.Anthropic(api_key=self.api_key)
            except ImportError:
                log.warning("anthropic SDK not installed; stage-2 reasoning disabled")
                return None
        return self._client

    # -- the call ----------------------------------------------------------

    def adjudicate(
        self, decisions: list[Decision], league_context: dict[str, Any]
    ) -> list[Decision]:
        """Review, rewrite, and possibly veto proposed decisions.

        On any failure the input is returned unchanged: a working recommendation
        without polished prose beats no recommendation.
        """
        if not decisions:
            return []
        client = self._client_or_none()
        if client is None:
            return decisions

        payload = [self._serialize(d, i) for i, d in enumerate(decisions)]

        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=8000,
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                },
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        # Stable across every call -- cache it. Volatile content
                        # goes in the user turn, after this breakpoint.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "League context (this league only):\n"
                            f"{json.dumps(league_context, indent=2, default=str)}\n\n"
                            "Proposed decisions, already computed by the engines:\n"
                            f"{json.dumps(payload, indent=2, default=str)}\n\n"
                            "For each: decide whether it is worth interrupting Tyler about, "
                            "set its priority, and write the headline and the why. Suppress "
                            "anything that does not clear the bar."
                        ),
                    }
                ],
            )
        except Exception as exc:  # noqa: BLE001 - never let the API kill a cycle
            log.warning("stage-2 reasoning failed (%s); using deterministic output", exc)
            return decisions

        if getattr(response, "stop_reason", None) == "refusal":
            log.warning("stage-2 declined this request; using deterministic output")
            return decisions

        try:
            text = next(b.text for b in response.content if b.type == "text")
            verdict = json.loads(text)
        except (StopIteration, json.JSONDecodeError, AttributeError) as exc:
            log.warning("could not parse stage-2 output (%s); using deterministic", exc)
            return decisions

        usage = getattr(response, "usage", None)
        if usage is not None:
            log.info(
                "stage-2: %s in / %s out / %s cached",
                getattr(usage, "input_tokens", "?"),
                getattr(usage, "output_tokens", "?"),
                getattr(usage, "cache_read_input_tokens", 0),
            )

        return self._apply(decisions, verdict)

    def _apply(self, decisions: list[Decision], verdict: dict[str, Any]) -> list[Decision]:
        out: list[Decision] = []
        for item in verdict.get("decisions", []):
            idx = item.get("index")
            if not isinstance(idx, int) or not 0 <= idx < len(decisions):
                continue
            decision = decisions[idx]

            if not item.get("keep", True):
                log.info(
                    "stage-2 suppressed %s/%s: %s",
                    decision.league_key, decision.summary,
                    item.get("suppression_reason", "no reason given"),
                )
                continue

            decision.summary = item.get("headline") or decision.summary
            decision.rationale = item.get("why") or decision.rationale
            if item.get("priority") in (
                Priority.CRITICAL, Priority.HIGH, Priority.MEDIUM, Priority.LOW
            ):
                decision.priority = item["priority"]

            confidence = item.get("confidence")
            if isinstance(confidence, (int, float)) and 0 <= confidence <= 1:
                decision.confidence = round(float(confidence), 2)

            claims = item.get("claim_types") or {}
            if claims:
                decision.assumptions = [
                    f"FACT: {claims.get('fact', 'n/a')}",
                    f"PROJECTION: {claims.get('projection', 'n/a')}",
                    f"INFERENCE: {claims.get('inference', 'n/a')}",
                    f"UNCERTAINTY: {claims.get('uncertainty', 'n/a')}",
                ]
            decision.evidence["model"] = self.model
            out.append(decision)
        return out

    def _serialize(self, decision: Decision, index: int) -> dict[str, Any]:
        data = asdict(decision)
        data["index"] = index
        data["championship_delta"] = decision.championship_delta
        return data
