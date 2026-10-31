import pytest
from sqlmodel import Session

from backend.audit.log import AuditEvent, AuditLog, hash_text
from backend.gate.confidence import score, self_consistency
from backend.gate.engine import Policy
from backend.kb.chunker import chunk_markdown, load_sops
from backend.pii.masker import PiiMasker, PiiVault, luhn_valid, verhoeff_valid
from mock_paytm.db import make_engine

WEIGHTS = Policy.load().confidence_weights

# ---------- confidence ----------


def test_confidence_formula_matches_prd():
    b = score(
        evidence_rule_match=1.0,
        self_consistency=2 / 3,
        verifier_pass=True,
        schema_valid=True,
        weights=WEIGHTS,
    )
    assert b.total == pytest.approx(0.4 + 0.2 + 0.2 + 0.1)
    assert b.explain().startswith("0.90 = evidence_rule_match 1.00×0.4")


def test_confidence_all_or_nothing():
    full = score(
        evidence_rule_match=1, self_consistency=1, verifier_pass=1, schema_valid=1, weights=WEIGHTS
    )
    none = score(
        evidence_rule_match=0, self_consistency=0, verifier_pass=0, schema_valid=0, weights=WEIGHTS
    )
    assert (full.total, none.total) == (1.0, 0.0)


def test_confidence_rejects_bad_inputs():
    with pytest.raises(ValueError):
        score(
            evidence_rule_match=1.2,
            self_consistency=1,
            verifier_pass=1,
            schema_valid=1,
            weights=WEIGHTS,
        )
    with pytest.raises(ValueError):
        score(
            evidence_rule_match=1,
            self_consistency=1,
            verifier_pass=1,
            schema_valid=1,
            weights={**WEIGHTS, "schema_valid": 0.5},
        )


def test_self_consistency():
    a, b = ("KYC_HOLD", "send_kyc_link"), ("SETTLEMENT_STUCK", "retrigger_settlement")
    assert self_consistency([a, a, a]) == 1.0
    assert self_consistency([a, a, b]) == pytest.approx(2 / 3)
    assert self_consistency([]) == 0.0


# ---------- PII ----------

VALID_AADHAAR = "2345 6789 0124"  # passes Verhoeff


@pytest.fixture(scope="module", params=[True, False], ids=["presidio", "regex"])
def masker(request):
    mk = PiiMasker(use_presidio=request.param)
    if request.param:
        assert mk.backend == "presidio"
    return mk


@pytest.mark.parametrize(
    "text,entity",
    [
        (f"mera aadhaar {VALID_AADHAAR} hai", "AADHAAR"),
        ("PAN ABCPK1234Z", "PAN"),
        ("pan abcpk1234z", "PAN"),
        ("UPI ramesh.k@okaxis pe bhejo", "UPI_VPA"),
        ("email ramesh@gmail.com", "EMAIL"),
        ("IFSC SBIN0001234", "IFSC"),
        ("mobile 9876543210", "IN_MOBILE"),
        ("call +91 9876543210", "IN_MOBILE"),
        ("mera account number 123456789012345 hai", "BANK_ACCOUNT"),
        ("card 4111 1111 1111 1111", "CARD"),
    ],
)
def test_masks_indian_identifiers(masker, text, entity):
    vault = PiiVault()
    out = masker.mask(text, vault)
    assert f"<{entity}_1>" in out, out
    assert vault.unmask(out) == text


@pytest.mark.parametrize(
    "text",
    [
        "Mera settlement 18400 nahi aaya",
        "UTR123456789012 mila",
        "aadhaar 2345 6789 0123 galat number",  # fails Verhoeff
        "order 1234 5678 9012 3456",  # fails Luhn
        "settlement id STL33FCFD8547",
        "123456789012345",  # long number with no account context
    ],
)
def test_leaves_non_pii_alone(masker, text):
    assert masker.mask(text, PiiVault()) == text


def test_same_value_same_token_across_messages(masker):
    vault = PiiVault()
    a = masker.mask("PAN ABCPK1234Z", vault)
    b = masker.mask("dobara: abcpk1234z", vault)
    assert a.endswith("<PAN_1>") and b.endswith("<PAN_1>")
    c = masker.mask("doosra PAN XYZPQ9876R", vault)
    assert c.endswith("<PAN_2>")


def test_vault_round_trips_through_state():
    vault = PiiVault()
    PiiMasker(use_presidio=False).mask("IFSC SBIN0001234", vault)
    restored = PiiVault.from_dict(vault.to_dict())
    assert restored.unmask("send to <IFSC_1>") == "send to SBIN0001234"
    assert restored.unmask("<PAN_9> unknown stays") == "<PAN_9> unknown stays"


def test_checksums():
    assert verhoeff_valid("234567890124") and not verhoeff_valid("234567890123")
    assert luhn_valid("4111111111111111") and not luhn_valid("4111111111111112")


# ---------- audit ----------


@pytest.fixture
def audit():
    return AuditLog(make_engine("sqlite://"))


def test_audit_append_and_query(audit):
    audit.append(
        thread_id="t1",
        merchant_id="M1001",
        node="ingest",
        event="RECEIVED",
        input_hash=hash_text("<masked text>"),
    )
    audit.append(
        thread_id="t1",
        merchant_id="M1001",
        node="gate",
        event="GATE_DECIDED",
        action="send_kyc_link",
        gate={"tier": "AUTO"},
        confidence={"total": 0.9},
    )
    audit.append(thread_id="t2", merchant_id="M1002", node="ingest", event="RECEIVED")
    events = audit.events(thread_id="t1")
    assert [e.event for e in events] == ["RECEIVED", "GATE_DECIDED"]
    assert events[1].gate == {"tier": "AUTO"}
    assert len(audit.events(merchant_id="M1002")) == 1


def test_audit_chain_detects_tampering(audit):
    for i in range(3):
        audit.append(thread_id="t1", node="execute", event="EXECUTED", result={"i": i})
    assert audit.verify_chain() == (True, None)

    with Session(audit.engine) as s:
        e = s.get(AuditEvent, 2)
        e.result = {"i": 999}
        s.commit()
    assert audit.verify_chain() == (False, 2)


def test_audit_chain_detects_deletion(audit):
    for i in range(3):
        audit.append(thread_id="t1", node="execute", event="EXECUTED", result={"i": i})
    with Session(audit.engine) as s:
        s.delete(s.get(AuditEvent, 2))
        s.commit()
    assert audit.verify_chain() == (False, 3)


def test_audit_has_no_update_or_delete_api():
    assert not any(hasattr(AuditLog, n) for n in ("update", "delete", "remove", "edit"))


# ---------- knowledge base ----------


def test_chunk_markdown():
    md = "# Doc\n\nintro ignored\n\n## First part\nalpha\n\n## Second: part!\nbeta\n"
    chunks = chunk_markdown("doc.md", md)
    assert [c.id for c in chunks] == ["doc#first-part", "doc#second-part"]
    assert chunks[1].title == "Doc > Second: part!" and chunks[1].text == "beta"


def test_sops_load_and_cover_every_workflow():
    chunks = load_sops()
    ids = {c.id for c in chunks}
    assert len(ids) == len(chunks)  # unique
    for needed in [
        "settlement#kyc-re-verification-hold",
        "settlement#risk-review-hold",
        "device#troubleshooting-order",
        "rental_billing#rent-charged-after-a-device-was-returned-billing-out-of-sync",
        "lending_consent#key-fact-statement-and-cooling-off",
        "escalation#always-escalate",
        "tone_language#ai-disclosure",
    ]:
        assert needed in ids, needed
    assert all(c.source != "README.md" for c in chunks)
