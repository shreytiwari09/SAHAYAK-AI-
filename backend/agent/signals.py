"""Deterministic signals read from the merchant's own words.

These run next to the LLM classifier, so a red-team message is caught even if the model misses it
(defence in depth, design decision D2). Any signal that is in policy.yaml's
`always_escalate_signals` sends the case to a human.
"""

import re

MESSAGE_SIGNALS = {
    "prompt_injection_detected",
    "legal_or_ombudsman_mention",
    "fraud_suspected",
    "merchant_requested_human",
    "chargeback_or_dispute",
}

_PATTERNS: dict[str, list[str]] = {
    "prompt_injection_detected": [
        r"ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions?|rules?|prompts?)",
        r"(system|developer)\s*prompt",
        r"\bpolicy\.ya?ml\b",
        r"\b(admin|developer|debug|god|sudo)\s*mode\b",
        r"you\s+are\s+now\b",
        r"\bsystem\s*:",
        r"(skip|bypass|disable|override)\s+(the\s+)?(approval|gate|guardrail|policy|check|verification)",
        r"(approval|guardrail|policy|verification)\s+(skip|bypass|off)\s*(karo|kar\s*do)?",
        r"\bjailbreak\b",
        r"(reveal|show|print|send)\s+(me\s+)?(your|the)\s+(instructions|prompt|rules|policy)",
    ],
    "legal_or_ombudsman_mention": [
        r"ombudsman",
        r"consumer\s+(court|forum|commission)",
        r"\bpolice\b",
        r"\b(lawyer|advocate|vakil)\b",
        r"legal\s+(action|notice)",
        r"\bcourt\b",
        r"\brbi\b.*\b(complaint|shikayat)\b",
        r"\b(complaint|shikayat)\b.*\brbi\b",
        r"(cyber\s*crime|cybercrime)",
    ],
    "fraud_suspected": [
        r"\bhack(ed|ing)?\b",
        r"\bfraud\b",
        r"\bscam(med)?\b",
        r"\bdhokha\b",
        r"(account|khata)\s+(takeover|hijack)",
        r"kisi\s+ne\s+mera\s+account",
        r"unauthori[sz]ed\s+(transaction|access|transfer)",
        r"\bphishing\b",
    ],
    "chargeback_or_dispute": [r"\bchargeback\b", r"\bdispute(d)?\b"],
}

# The merchant typing HUMAN (or asking for one) always reaches a person.
_HUMAN = [
    r"^\s*(human|agent|insaan|manager)\s*[.!]*\s*$",
    r"(talk|speak|baat)\s+(to|with)\s+(a\s+)?(human|agent|person|manager|insaan)",
    r"\b(human|real\s+person|live\s+agent)\b.*\b(please|chahiye|want|need|bhejo)\b",
    r"insaan\s+se\s+baat",
    r"kisi\s+(insaan|aadmi|bande)\s+se",
]
_COMPILED = {k: [re.compile(p, re.IGNORECASE) for p in v] for k, v in _PATTERNS.items()}
_HUMAN_RX = [re.compile(p, re.IGNORECASE) for p in _HUMAN]


def rule_signals(text: str | None) -> set[str]:
    if not text:
        return set()
    found = {sig for sig, rxs in _COMPILED.items() if any(rx.search(text) for rx in rxs)}
    if any(rx.search(text) for rx in _HUMAN_RX):
        found.add("merchant_requested_human")
    return found
