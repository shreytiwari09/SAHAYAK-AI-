"""Typed outputs the LLM must produce, validated before the agent acts on them."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Sentiment = Literal["neutral", "angry", "confused"]
Intent = Literal["report_problem", "ask_status", "loan_request", "other_merchant_data", "other"]
Topic = Literal["money", "device", "billing", "kyc", "loan", "other"]
Script = Literal["latin", "devanagari", "other"]


class Classification(BaseModel):
    language: str = "hi"  # ISO 639-1 of what the merchant wrote
    script: Script = "latin"
    sentiment: Sentiment = "neutral"
    intent: Intent = "report_problem"
    topic: Topic = "other"
    signals: list[str] = Field(default_factory=list)

    @field_validator("language", mode="before")
    @classmethod
    def _lang(cls, v: object) -> str:
        v = str(v or "hi").strip().lower()
        return v[:2] if len(v) >= 2 else "hi"


class Diagnosis(BaseModel):
    cause: str | None = None  # one of the findings' causes, or None
    action: str | None = None
    reasoning: str = ""

    @field_validator("cause", "action", mode="before")
    @classmethod
    def _none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip().upper() in ("", "NONE", "NULL"):
            return None
        return v


class PlanCheck(BaseModel):
    passed: bool
    why: str = ""
