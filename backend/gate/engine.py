"""Deterministic guardrail gate (PRD §6, design decision D2).

The LLM never chooses its own autonomy tier. It proposes an action; `Gate.decide` evaluates the
policy and returns AUTO, APPROVE or ESCALATE together with every rule that fired, so each
decision can be audited and explained.
"""

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from backend.config import get_settings


class Tier(IntEnum):
    # Ordered by severity: the strictest fired rule wins.
    AUTO = 0
    APPROVE = 1
    ESCALATE = 2


class ActionPolicy(BaseModel):
    reversible: bool
    max_tier: str
    moves_money: bool
    requires_consent: str | None = None


class Thresholds(BaseModel):
    auto_min_confidence: float
    approve_min_confidence: float
    auto_max_amount_inr: float
    approve_max_amount_inr: float
    repeat_contact_escalate_at: int


class Policy(BaseModel):
    version: int
    kill_switch: bool = False
    thresholds: Thresholds
    confidence_weights: dict[str, float]
    always_escalate_signals: list[str]
    approve_if_sentiment: list[str]
    actions: dict[str, ActionPolicy]

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Policy":
        path = Path(path or get_settings().policy_path or Path(__file__).parent / "policy.yaml")
        return cls.model_validate(yaml.safe_load(path.read_text()))


class ProposedAction(BaseModel):
    """What the agent wants to do, plus the signals the gate needs to judge it."""

    action: str
    args: dict = Field(default_factory=dict)
    amount_inr: float = 0.0
    confidence: float
    sentiment: str = "neutral"
    signals: set[str] = Field(default_factory=set)
    # Contacts about this same issue, including the current one.
    contact_count: int = 1
    consents: set[str] = Field(default_factory=set)


@dataclass
class RuleHit:
    rule: str
    tier: Tier
    detail: str


@dataclass
class GateDecision:
    tier: Tier
    hits: list[RuleHit] = field(default_factory=list)
    policy_version: int = 0

    @property
    def rules(self) -> list[str]:
        return [h.rule for h in self.hits]

    @property
    def reason(self) -> str:
        decisive = [h for h in self.hits if h.tier == self.tier]
        if not decisive:
            return "all AUTO conditions met"
        return "; ".join(h.detail for h in decisive)

    def to_dict(self) -> dict:
        return {
            "tier": self.tier.name,
            "reason": self.reason,
            "rules": [{"rule": h.rule, "tier": h.tier.name, "detail": h.detail} for h in self.hits],
            "policy_version": self.policy_version,
        }


class Gate:
    def __init__(self, policy: Policy | None = None):
        self.policy = policy or Policy.load()

    def decide(self, p: ProposedAction) -> GateDecision:
        pol, th = self.policy, self.policy.thresholds
        hits: list[RuleHit] = []

        def hit(rule: str, tier: Tier, detail: str) -> None:
            hits.append(RuleHit(rule, tier, detail))

        action = pol.actions.get(p.action)
        if action is None:
            hit("unknown_action", Tier.ESCALATE, f"'{p.action}' is not an allow-listed action")
            return GateDecision(Tier.ESCALATE, hits, pol.version)

        # ----- escalate -----
        for sig in sorted(p.signals & set(pol.always_escalate_signals)):
            hit(f"signal:{sig}", Tier.ESCALATE, f"signal '{sig}' always goes to a human")
        if p.contact_count >= th.repeat_contact_escalate_at:
            hit(
                "repeat_contact",
                Tier.ESCALATE,
                f"contact #{p.contact_count} on this issue (limit {th.repeat_contact_escalate_at})",
            )
        if p.confidence < th.approve_min_confidence:
            hit(
                "low_confidence",
                Tier.ESCALATE,
                f"confidence {p.confidence:.2f} < {th.approve_min_confidence}",
            )
        if action.moves_money and p.amount_inr > th.approve_max_amount_inr:
            hit(
                "amount_above_approval_limit",
                Tier.ESCALATE,
                f"₹{p.amount_inr:,.0f} > ₹{th.approve_max_amount_inr:,.0f}",
            )
        if action.requires_consent and action.requires_consent not in p.consents:
            hit(
                "consent_missing",
                Tier.ESCALATE,
                f"needs granted {action.requires_consent} consent",
            )

        # ----- approve -----
        if pol.kill_switch:
            hit("kill_switch", Tier.APPROVE, "kill switch on: every write needs approval")
        if action.max_tier == "APPROVE":
            hit("action_needs_approval", Tier.APPROVE, f"'{p.action}' always needs a human click")
        if not action.reversible:
            hit("irreversible", Tier.APPROVE, f"'{p.action}' cannot be undone")
        if th.approve_min_confidence <= p.confidence < th.auto_min_confidence:
            hit(
                "medium_confidence",
                Tier.APPROVE,
                f"confidence {p.confidence:.2f} < {th.auto_min_confidence}",
            )
        if (
            action.moves_money
            and th.auto_max_amount_inr < p.amount_inr <= th.approve_max_amount_inr
        ):
            hit(
                "amount_above_auto_limit",
                Tier.APPROVE,
                f"₹{p.amount_inr:,.0f} > ₹{th.auto_max_amount_inr:,.0f}",
            )
        if p.sentiment in pol.approve_if_sentiment:
            hit("sentiment", Tier.APPROVE, f"merchant sentiment is {p.sentiment}")

        tier = max((h.tier for h in hits), default=Tier.AUTO)
        return GateDecision(tier, hits, pol.version)
