"""PII masking before anything reaches the LLM (design decision D11).

The model sees placeholders like <PAN_1>; real values live in a per-thread PiiVault and are put
back only in the tool layer and in outbound messages.

Recognizers cover Indian identifiers: Aadhaar (Verhoeff checksum), PAN, UPI VPA, IFSC, Indian
mobile numbers and bank account numbers (only with nearby context words), plus email and card
numbers. Uses Microsoft Presidio when available, otherwise the same patterns with plain `re`.
Person names are not masked by default: spaCy's English NER is unreliable on Hinglish text.
"""

import re
from dataclasses import dataclass, field

AADHAAR = "AADHAAR"
PAN = "PAN"
UPI_VPA = "UPI_VPA"
IFSC = "IFSC"
IN_MOBILE = "IN_MOBILE"
BANK_ACCOUNT = "BANK_ACCOUNT"
EMAIL = "EMAIL"
CARD = "CARD"


@dataclass(frozen=True)
class Rule:
    entity: str
    pattern: str
    score: float
    # Words that raise the score when they appear near the match.
    context: tuple[str, ...] = ()


RULES = [
    Rule(AADHAAR, r"\b[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}\b", 0.9, ("aadhaar", "aadhar", "uid")),
    Rule(PAN, r"\b[A-Z]{3}[ABCFGHLJPT][A-Z]\d{4}[A-Z]\b", 0.85, ("pan",)),
    Rule(EMAIL, r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", 0.9),
    # Not followed by ".com" etc., so email addresses are left to the EMAIL rule.
    Rule(UPI_VPA, r"\b[\w.-]{2,256}@[a-zA-Z]{2,64}\b(?!\.[a-zA-Z])", 0.8, ("upi", "vpa")),
    Rule(IFSC, r"\b[A-Z]{4}0[A-Z0-9]{6}\b", 0.85, ("ifsc",)),
    Rule(
        IN_MOBILE, r"(?<!\d)(?:\+91[\s-]?|0)?[6-9]\d{9}(?!\d)", 0.7, ("phone", "mobile", "number")
    ),
    Rule(CARD, r"\b(?:\d{4}[\s-]?){3}\d{4}\b", 0.6, ("card",)),
    # Scores below the threshold unless a context word is nearby.
    Rule(BANK_ACCOUNT, r"\b\d{9,18}\b", 0.2, ("account", "a/c", "acct", "khata", "bank", "acc")),
]
DEFAULT_ENTITIES = tuple(r.entity for r in RULES)
THRESHOLD = 0.4
CONTEXT_WINDOW = 40
CONTEXT_BOOST = 0.35

# Verhoeff tables for the Aadhaar checksum.
_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def verhoeff_valid(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    c = 0
    for i, d in enumerate(reversed(digits)):
        c = _D[c][_P[i % 8][d]]
    return c == 0


def luhn_valid(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


CHECKSUMMED = {AADHAAR, CARD}


def _validate(entity: str, value: str) -> bool:
    if entity == AADHAAR:
        return verhoeff_valid(value)
    if entity == CARD:
        return luhn_valid(value)
    return True


@dataclass
class Span:
    entity: str
    start: int
    end: int
    score: float


@dataclass
class PiiVault:
    """Per-thread placeholder <-> value map. Serialisable so it can live in graph state."""

    by_value: dict[str, str] = field(default_factory=dict)
    by_token: dict[str, str] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)

    def token_for(self, entity: str, value: str) -> str:
        key = f"{entity}:{_normalise(value)}"
        if key not in self.by_value:
            n = self.counters.get(entity, 0) + 1
            self.counters[entity] = n
            token = f"<{entity}_{n}>"
            self.by_value[key] = token
            self.by_token[token] = value
        return self.by_value[key]

    def unmask(self, text: str) -> str:
        return re.sub(
            r"<[A-Z_]+_\d+>", lambda mt: self.by_token.get(mt.group(0), mt.group(0)), text
        )

    def to_dict(self) -> dict:
        return {"by_value": self.by_value, "by_token": self.by_token, "counters": self.counters}

    @classmethod
    def from_dict(cls, d: dict) -> "PiiVault":
        return cls(dict(d["by_value"]), dict(d["by_token"]), dict(d["counters"]))


def _normalise(value: str) -> str:
    return re.sub(r"[\s-]", "", value).upper()


class _RegexEngine:
    def __init__(self, entities: tuple[str, ...]):
        self.rules = [r for r in RULES if r.entity in entities]
        self.compiled = [(r, re.compile(r.pattern, re.IGNORECASE)) for r in self.rules]

    def find(self, text: str) -> list[Span]:
        lower = text.lower()
        spans = []
        for rule, rx in self.compiled:
            for mt in rx.finditer(text):
                if not _validate(rule.entity, mt.group(0)):
                    continue
                score = rule.score
                window = lower[max(0, mt.start() - CONTEXT_WINDOW) : mt.end() + CONTEXT_WINDOW]
                if rule.context and any(w in window for w in rule.context):
                    score = min(1.0, score + CONTEXT_BOOST)
                if score >= THRESHOLD:
                    spans.append(Span(rule.entity, mt.start(), mt.end(), score))
        return spans


class _PresidioEngine:
    def __init__(self, entities: tuple[str, ...]):
        from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerRegistry
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        class ValidatedRecognizer(PatternRecognizer):
            def validate_result(self, pattern_text: str) -> bool | None:
                # Presidio: True -> score 1.0, False -> discard, None -> keep the pattern score.
                entity = self.supported_entities[0]
                if not _validate(entity, pattern_text):
                    return False
                return True if entity in CHECKSUMMED else None

        registry = RecognizerRegistry(supported_languages=["en"])
        for r in RULES:
            if r.entity in entities:
                registry.add_recognizer(
                    ValidatedRecognizer(
                        supported_entity=r.entity,
                        name=f"sahayak_{r.entity.lower()}",
                        patterns=[Pattern(r.entity, r.pattern, r.score)],
                        context=list(r.context),
                    )
                )
        nlp = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
            }
        ).create_engine()
        self.entities = list(entities)
        self.analyzer = AnalyzerEngine(
            registry=registry, nlp_engine=nlp, supported_languages=["en"]
        )

    def find(self, text: str) -> list[Span]:
        results = self.analyzer.analyze(
            text=text, language="en", entities=self.entities, score_threshold=THRESHOLD
        )
        return [Span(r.entity_type, r.start, r.end, r.score) for r in results]


def _resolve_overlaps(spans: list[Span]) -> list[Span]:
    """Keep non-overlapping spans, preferring higher score, then longer span."""
    chosen: list[Span] = []
    for sp in sorted(spans, key=lambda x: (-x.score, -(x.end - x.start), x.start)):
        if all(sp.end <= c.start or sp.start >= c.end for c in chosen):
            chosen.append(sp)
    return sorted(chosen, key=lambda x: x.start)


class PiiMasker:
    def __init__(self, entities: tuple[str, ...] = DEFAULT_ENTITIES, use_presidio: bool = True):
        self.engine: _RegexEngine | _PresidioEngine
        self.backend = "regex"
        if use_presidio:
            try:
                self.engine = _PresidioEngine(entities)
                self.backend = "presidio"
            except Exception:  # spaCy model missing etc.: fall back, same patterns
                self.engine = _RegexEngine(entities)
        else:
            self.engine = _RegexEngine(entities)

    def find(self, text: str) -> list[Span]:
        return _resolve_overlaps(self.engine.find(text))

    def mask(self, text: str, vault: PiiVault) -> str:
        out, last = [], 0
        for sp in self.find(text):
            out.append(text[last : sp.start])
            out.append(vault.token_for(sp.entity, text[sp.start : sp.end]))
            last = sp.end
        out.append(text[last:])
        return "".join(out)
