"""Outbound messages: grounding check, PII scrub, AI disclosure and fallback templates.

Design decisions D1/D11: the LLM writes the words, but every number in a message must come from a
tool result (the `facts`), and no PII placeholder or raw identifier may reach the merchant. If a
composed message fails either check it is replaced by a plain template built from the facts.
"""

import json
import re
from typing import Any

PLACEHOLDER = re.compile(r"<[A-Z_]+_\d+>")
# Numbers of 3+ digits, with Indian/Western grouping and optional decimals: 18,400  1,85,000  ₹149
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

DISCLOSURE = {
    "hi": "Namaste! Main {who}Paytm ka AI assistant hoon. Kabhi bhi HUMAN likhein, aapko insaan se jod diya jayega.",
    "en": "Hello! I'm {who}Paytm's AI assistant. Type HUMAN at any time to reach a person.",
}
INTRO = {"hi": "{name}, ", "en": "{name}, "}


def digits_of(value: str) -> str:
    return re.sub(r"[,\s]", "", value).rstrip(".")


def numbers_in(text: str) -> set[str]:
    """Numbers of 3 or more digits in `text`, normalised (commas removed, decimal .0 dropped)."""
    out = set()
    for raw in NUMBER.findall(text):
        n = digits_of(raw)
        if n.endswith(".0") or n.endswith(".00"):
            n = n.split(".")[0]
        if len(n.replace(".", "")) >= 3:
            out.add(n)
    return out


def allowed_numbers(facts: dict[str, Any]) -> set[str]:
    allowed = set()
    for token in NUMBER.findall(json.dumps(facts, ensure_ascii=False, default=str)):
        n = digits_of(token)
        allowed.add(n)
        if "." in n:
            allowed.add(n.rstrip("0").rstrip("."))
            allowed.add(n.split(".")[0])
        # Digits run together inside ids (UTR123456789012) must not whitelist partial numbers.
    return allowed


def ungrounded_numbers(text: str, facts: dict[str, Any]) -> set[str]:
    return numbers_in(text) - allowed_numbers(facts)


def scrub(text: str) -> str:
    """Remove PII placeholders the model may have echoed. They are never un-masked outbound."""
    cleaned = re.sub(r"\s{2,}", " ", PLACEHOLDER.sub("", text))
    return re.sub(r"\s+([.,!?])", r"\1", cleaned).strip()


def disclosure(lang: str, agent: str | None = None) -> str:
    """The one-time AI introduction; names the merchant's rep when there is one."""
    key = lang if lang in DISCLOSURE else "en"
    return DISCLOSURE[key].format(who=INTRO[key].format(name=agent) if agent else "")


def inr(x: float) -> str:
    """₹18,400 / ₹1,85,000 (Indian grouping); paise shown only when present."""
    whole, _, frac = f"{abs(x):.2f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    if head:
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])
    return f"₹{whole}" + (f".{frac}" if frac != "00" else "")


# Plain templates used when the LLM is unavailable or its message fails the checks.
# {hi} is Hinglish (Latin script); other languages get the English text.
TEMPLATES: dict[str, dict[str, str]] = {
    "kyc_link": {
        "hi": "Aapka settlement KYC re-verification ki wajah se ruka hai. Yeh link kholkar KYC poora karein: {url}",
        "en": "Your settlement is on hold for KYC re-verification. Please complete it here: {url}",
    },
    "bank_update": {
        "hi": "Aapke bank account details mein dikkat hai, isliye settlement fail hua. Sahi details yahan bharein: {url}",
        "en": "Your bank account details were rejected, so the settlement failed. Please update them here: {url}",
    },
    "power_cycle": {
        "hi": "Aapka {device} offline hai. Kripya use band karke 10 second baad dobara chalu karein, phir hum check karenge.",
        "en": "Your {device} is offline. Please switch it off, wait 10 seconds and switch it on again; we will then re-check.",
    },
    "resolved": {
        "hi": "Aapki samasya hal ho gayi hai aur humne check bhi kar liya hai. {proof}",
        "en": "Your issue is fixed and we have checked it. {proof}",
    },
    "stalled": {
        "hi": "Hum aapke agle step ka intezaar kar rahe hain. Poora hote hi hum aage badhenge. Madad ke liye HUMAN likhein.",
        "en": "We are waiting for your next step and will continue as soon as it is done. Type HUMAN for help.",
    },
    "escalated": {
        "hi": "Aapka case ek specialist ko de diya gaya hai. Aapko dobara kuch samjhane ki zaroorat nahi, woh jaldi sampark karenge.",
        "en": "Your case has been passed to a specialist. You do not need to explain again; they will contact you soon.",
    },
    "approval_pending": {
        "hi": "Aapka request hamari team ke paas approval ke liye hai. Hum jaldi update denge.",
        "en": "Your request is with our team for approval. We will update you shortly.",
    },
    "explain": {
        "hi": "Humne aapke account ko check kiya. {note}",
        "en": "We checked your account. {note}",
    },
    "answer": {
        "hi": "Maaf kijiye, abhi main iska sahi jawab nahi de pa raha. Kripya HUMAN likhein, aapko ek insaan se jod diya jayega.",
        "en": "Sorry, I cannot answer that properly right now. Please type HUMAN and a person will take over.",
    },
    "refuse": {
        "hi": "Maaf kijiye, main sirf aapke apne account ki jaankari de sakta hoon.",
        "en": "Sorry, I can only share information about your own account.",
    },
    "offer": {
        "hi": "Aapke business ke liye {amount} ka loan offer hai ({tenure} mahine, {apr}% saalana). Kya aap details dekhna chahenge? Haan / Nahi",
        "en": "There is a {amount} loan offer for your business ({tenure} months, {apr}% a year). Would you like to see the details? Yes (Haan) / No (Nahi)",
    },
    "offer_thanks": {
        "hi": "Shukriya! Humne aapki request note kar li hai. Aapke paas {days} din ka cooling-off period hai.",
        "en": "Thank you! We have noted your request. You have a {days}-day cooling-off period.",
    },
    "offer_declined": {
        "hi": "Theek hai, koi baat nahi. Hum aapko dobara nahi poochhenge.",
        "en": "No problem. We will not ask again for now.",
    },
    "no_offer": {
        "hi": "Abhi aapke liye koi loan offer available nahi hai. Jaise hi hoga hum batayenge.",
        "en": "There is no loan offer available for you right now. We will let you know when there is.",
    },
}


def template(kind: str, facts: dict[str, Any], lang: str) -> str:
    t = TEMPLATES.get(kind) or TEMPLATES["explain"]
    body = t["hi"] if lang == "hi" else t["en"]
    values = {k: v for k, v in facts.items() if isinstance(v, str | int | float)}
    try:
        return body.format_map(_Default(values))
    except (KeyError, ValueError):
        return body


class _Default(dict):
    def __missing__(self, key: str) -> str:
        return ""
