"""Scenario schema and loader.

A scenario seeds a known broken state (faults on top of the base seed), describes the simulated
merchant, and states what must be true afterwards. Scoring compares the systems' end state, not
what the agent claims (τ-bench style).
"""

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from backend.gate.evidence import Cause
from mock_paytm.faults import Fault
from mock_paytm.models import DeviceFault

SCENARIO_DIR = Path(__file__).parent / "scenarios"


class Category(StrEnum):
    SETTLEMENT = "settlement"
    DEVICE = "device"
    BILLING = "billing"
    REFUND = "refund"
    KYC = "kyc"
    LENDING = "lending"
    SAFETY = "safety"


class Trigger(StrEnum):
    MERCHANT_MESSAGE = "merchant_message"
    PROACTIVE = "proactive"


class Mood(StrEnum):
    CALM = "calm"
    ANGRY = "angry"
    CONFUSED = "confused"
    ADVERSARIAL = "adversarial"


class ExpectedTier(StrEnum):
    AUTO = "AUTO"
    APPROVE = "APPROVE"
    ESCALATE = "ESCALATE"
    NONE = "NONE"  # nothing to execute (e.g. explain a valid charge)


class OfferReply(StrEnum):
    HAAN = "HAAN"
    NAHI = "NAHI"


class FaultSetup(BaseModel):
    fault: Fault
    merchant_id: str | None = None  # defaults to the scenario merchant
    amount_inr: float | None = None
    device_fault: DeviceFault = DeviceFault.POWER


class Behaviour(BaseModel):
    """What the simulated merchant does when the agent asks."""

    complete_kyc_link: bool = True
    complete_bank_update: bool = True
    power_cycle_when_asked: bool = True
    offer_reply: OfferReply | None = None
    # Approver's answer at the one-click approval step.
    approver_approves: bool = True


class Persona(BaseModel):
    language: str  # ISO 639-1
    script: str = "latin"
    mood: Mood = Mood.CALM
    opening: str | None = None  # None for proactive scenarios
    behaviour: Behaviour = Field(default_factory=Behaviour)


class TicketOutcome(StrEnum):
    CLOSED_VERIFIED = "CLOSED_VERIFIED"
    OPEN_ESCALATED = "OPEN_ESCALATED"
    NOT_CLOSED = "NOT_CLOSED"  # must not be closed (fix not verified)
    NONE = "NONE"  # no ticket needed


class ConsentOutcome(StrEnum):
    GRANTED = "GRANTED"
    DECLINED = "DECLINED"
    NONE = "NONE"


class EndState(BaseModel):
    settlements_credited: bool | None = None
    kyc_status: str | None = None
    device_online: bool | None = None
    replacement_booked: bool | None = None
    billing_in_sync: bool | None = None
    post_return_deductions_reversed: bool | None = None
    debited_not_credited_cleared: bool | None = None
    lead_created: bool | None = None
    consent: ConsentOutcome | None = None
    ticket: TicketOutcome | None = None
    message_sent: bool | None = None
    no_pii_in_outbound: bool | None = None
    no_cross_merchant_access: bool | None = None


class Expected(BaseModel):
    """
    causes:       what the data proves after setup (checked by the oracle, no LLM involved)
    first_action: the first write action the agent should *propose*
    tier:         the gate's decision on that proposal (or on the case, if nothing is proposed)
    must_not:     write actions that must never be *executed* in this scenario
    """

    causes: list[Cause]
    first_action: str | None
    tier: ExpectedTier
    rules: list[str] = Field(default_factory=list)
    must_not: list[str] = Field(default_factory=list)
    end_state: EndState


class Scenario(BaseModel):
    id: str
    title: str
    category: Category
    merchant_id: str
    trigger: Trigger = Trigger.MERCHANT_MESSAGE
    setup: list[FaultSetup] = Field(default_factory=list)
    persona: Persona
    expected: Expected

    @model_validator(mode="after")
    def _consistent(self) -> "Scenario":
        if self.trigger == Trigger.MERCHANT_MESSAGE and not self.persona.opening:
            raise ValueError(f"{self.id}: merchant_message scenarios need persona.opening")
        if self.trigger == Trigger.PROACTIVE and self.persona.opening:
            raise ValueError(f"{self.id}: proactive scenarios start without a merchant message")
        runs = self.expected.tier in (ExpectedTier.AUTO, ExpectedTier.APPROVE)
        if runs and self.expected.first_action in self.expected.must_not:
            raise ValueError(f"{self.id}: first_action would run but is also in must_not")
        if self.expected.tier == ExpectedTier.NONE and self.expected.first_action:
            raise ValueError(f"{self.id}: tier NONE cannot have a first_action")
        return self


def load_scenarios(directory: Path = SCENARIO_DIR) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for path in sorted(directory.glob("*.yaml")):
        for raw in yaml.safe_load(path.read_text()) or []:
            scenarios.append(Scenario.model_validate(raw))
    ids = [s.id for s in scenarios]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate scenario ids: {sorted(dupes)}")
    return scenarios
