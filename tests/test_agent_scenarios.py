"""Every eval scenario run through the real graph with a scripted stand-in for Gemini.

This exercises the graph, playbook, gate, tools, audit and scorer together. It proves the
*machinery* is right; how well Gemini diagnoses and writes is measured by `eval.run_eval`.
"""

import pytest

from eval.fake_llm import FakeLLM
from eval.runner import build_template_db, run_once
from eval.scenario import load_scenarios

SCENARIOS = load_scenarios()


@pytest.fixture(scope="module")
def template_db(tmp_path_factory):
    return build_template_db(tmp_path_factory.mktemp("eval") / "template.db")


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
async def test_scenario_passes_end_to_end(sc, template_db, tmp_path):
    r = await run_once(sc, template_db, tmp_path, FakeLLM.for_scenario)
    assert r.error is None, r.error
    assert not r.score.failures, [(c.check, c.expected, c.actual) for c in r.score.failures] + [
        ("outcome", r.state.get("outcome"), r.state.get("escalation_reason"))
    ]
    assert r.observed_tier == sc.expected.tier.value, (r.observed_tier, r.state.get("outcome"))
    assert not r.unsafe and not r.consent_violation and not r.closed_with_mismatch
