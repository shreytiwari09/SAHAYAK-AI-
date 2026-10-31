"""Explainable composite confidence (PRD §6, design decision D3).

confidence = 0.4·evidence_rule_match + 0.3·self_consistency + 0.2·verifier_pass + 0.1·schema_valid

Each component is inspectable and shown in the console, instead of asking the model to rate itself.
Weights come from policy.yaml.
"""

from collections import Counter
from collections.abc import Hashable, Sequence

from pydantic import BaseModel

COMPONENTS = ("evidence_rule_match", "self_consistency", "verifier_pass", "schema_valid")


class ConfidenceBreakdown(BaseModel):
    evidence_rule_match: float
    self_consistency: float
    verifier_pass: float
    schema_valid: float
    weights: dict[str, float]
    total: float

    def explain(self) -> str:
        parts = [
            f"{name} {getattr(self, name):.2f}×{self.weights[name]:.1f}" for name in COMPONENTS
        ]
        return f"{self.total:.2f} = " + " + ".join(parts)


def self_consistency(samples: Sequence[Hashable]) -> float:
    """Share of samples that agree with the most common answer, e.g. (cause, action) tuples."""
    if not samples:
        return 0.0
    _, top = Counter(samples).most_common(1)[0]
    return top / len(samples)


def score(
    *,
    evidence_rule_match: float,
    self_consistency: float,
    verifier_pass: float | bool,
    schema_valid: float | bool,
    weights: dict[str, float],
) -> ConfidenceBreakdown:
    values = {
        "evidence_rule_match": float(evidence_rule_match),
        "self_consistency": float(self_consistency),
        "verifier_pass": float(verifier_pass),
        "schema_valid": float(schema_valid),
    }
    for name, v in values.items():
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"{name} must be in [0, 1], got {v}")
    missing = set(COMPONENTS) - set(weights)
    if missing:
        raise ValueError(f"weights missing {sorted(missing)}")
    if abs(sum(weights[c] for c in COMPONENTS) - 1.0) > 1e-9:
        raise ValueError("confidence weights must sum to 1")
    total = round(sum(values[c] * weights[c] for c in COMPONENTS), 4)
    return ConfidenceBreakdown(**values, weights={c: weights[c] for c in COMPONENTS}, total=total)
