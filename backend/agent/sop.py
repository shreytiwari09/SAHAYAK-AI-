"""Procedure text handed to the model, picked deterministically from the SOP knowledge base.

Retrieval is by cause, not by embedding: every cause has a known section, so there is nothing to
mis-retrieve. (Embedding search over the same chunks can be added later for free-form questions.)
"""

from functools import lru_cache

from backend.gate.evidence import Cause
from backend.kb.chunker import load_sops

SECTIONS: dict[Cause, list[str]] = {
    Cause.KYC_HOLD: ["settlement#kyc-re-verification-hold", "kyc#sending-a-re-kyc-link"],
    Cause.KYC_NAME_MISMATCH: ["kyc#statuses", "escalation#always-escalate"],
    Cause.KYC_EXPIRING: ["kyc#expiring-kyc-proactive"],
    Cause.BANK_ACCOUNT_FAILED: ["settlement#bank-account-failure"],
    Cause.SETTLEMENT_STUCK: ["settlement#stuck-settlement-past-t-1"],
    Cause.RISK_HOLD: ["settlement#risk-review-hold", "escalation#always-escalate"],
    Cause.DEVICE_OFFLINE: ["device#troubleshooting-order", "device#replacement"],
    Cause.RENTAL_DESYNC: [
        "rental_billing#rent-charged-after-a-device-was-returned-billing-out-of-sync"
    ],
    Cause.DEBITED_NOT_CREDITED: ["refunds#fix"],
}


@lru_cache
def _by_id() -> dict[str, str]:
    return {c.id: f"{c.title}\n{c.text}" for c in load_sops()}


def procedures(causes: list[Cause | str]) -> str:
    ids: list[str] = []
    for c in causes:
        ids += SECTIONS.get(Cause(c), [])
    chunks = _by_id()
    seen: list[str] = []
    for i in ids:
        if i in chunks and i not in seen:
            seen.append(i)
    return "\n\n".join(chunks[i] for i in seen) or "(no procedure on file)"
