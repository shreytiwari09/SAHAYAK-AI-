"""A scripted stand-in for Gemini, so the graph, playbook, gate and scorer can be exercised
without a network or a key. It reads the scenario's persona (mood, language), never the expected
results, and always diagnoses the first proven finding like an LLM that agrees with the data.
"""

import re
from collections import Counter
from typing import Any

from backend.agent.schemas import Classification, Diagnosis, PlanCheck
from eval.scenario import Mood, Scenario


class FakeLLM:
    def __init__(
        self,
        classification: Classification | None = None,
        agree: bool = True,
        *,
        fail: set[str] | None = None,
        invent_amount: bool = False,
    ):
        self.classification = classification or Classification()
        self.agree = agree  # False: diagnoses a cause the data does not prove
        self.fail = fail or set()  # methods that raise, e.g. {"compose", "classify"}
        self.invent_amount = invent_amount  # compose() states an amount that is not in the facts
        self.calls: Counter[str] = Counter()
        self.seen: list[str] = []  # every text the model was shown (to prove PII was masked)

    @classmethod
    def for_scenario(cls, sc: Scenario) -> "FakeLLM":
        opening = (sc.persona.opening or "").lower()
        intent = "report_problem"
        if re.search(r"loan chahida|mainu .*loan", opening):
            intent = "loan_request"
        if re.search(r"padosi|neighbou?r|mere padosi", opening):
            intent = "other_merchant_data"
        return cls(
            Classification(
                language=sc.persona.language,
                script=sc.persona.script,
                sentiment="angry" if sc.persona.mood == Mood.ANGRY else "neutral",
                intent=intent,
                topic="other",
            )
        )

    async def classify(self, masked_text: str, history: list[dict] | None = None) -> Classification:
        self.history_seen = history or []
        self.calls["classify"] += 1
        self.seen.append(masked_text)
        self._maybe_fail("classify")
        return self.classification

    async def diagnose(self, masked_text, findings, allowed, sop) -> Diagnosis:
        self.calls["diagnose"] += 1
        self.seen.append(masked_text)
        self._maybe_fail("diagnose")
        if not findings:
            return Diagnosis()
        cause = findings[0]["cause"] if self.agree else "DEVICE_OFFLINE"
        actions = allowed.get(cause) or []
        return Diagnosis(cause=cause, action=actions[0] if actions else None, reasoning="fake")

    async def verify_plan(self, findings, cause, action, sop) -> PlanCheck:
        self.calls["verify_plan"] += 1
        self._maybe_fail("verify_plan")
        return PlanCheck(passed=self.agree)

    async def compose(self, kind: str, facts: dict[str, Any], lang, script, address) -> str:
        self.calls["compose"] += 1
        self._maybe_fail("compose")
        if self.invent_amount:
            return f"{address}, aapke ₹99,999 credit ho gaye hain."
        body = (
            facts.get("url")
            or facts.get("proof")
            or facts.get("finding")
            or facts.get("question")
            or ""
        )
        return f"{address}, [{kind}] {body}".strip()

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail:
            raise RuntimeError(f"{method} unavailable")
