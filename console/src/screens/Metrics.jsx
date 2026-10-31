import { Link } from "react-router-dom";
import { api } from "../api";
import { usePoll } from "../hooks";
import { Chip, Reading, Region, Skeleton } from "../ui";
import { OUTCOME, label } from "../labels";
import { pct, seconds } from "../format";

const CATEGORY = { kyc: "KYC", billing: "Billing and rent", refund: "Refunds and credits", safety: "Safety and red team" };

export default function Metrics() {
  const query = usePoll(() => api.metrics(), [], 3000);
  return (
    <div className="page">
      <div className="toolbar"><h1>Metrics</h1></div>
      <Region query={query} isEmpty={() => false} skeleton={<Skeleton rows={6} tall />} empty={{}}>
        {(m) => (
          <div className="stack">
            <Live live={m.live} />
            <Evaluation e={m.evaluation} />
            <Models models={m.models} />
          </div>
        )}
      </Region>
    </div>
  );
}

function Live({ live }) {
  if (!live.cases_total) {
    return (
      <section className="state">
        <h2>No cases since the server started</h2>
        <p>Live numbers count the cases handled by this running server. Start one from the chat.</p>
        <Link className="btn btn--secondary" to="/chat">Send a merchant message</Link>
      </section>
    );
  }
  return (
    <section>
      <h2 className="section">Since the server started</h2>
      <div className="panels panels--live">
        <Reading value={live.verified_resolutions} caption="fixes verified in the systems" />
        <dl className="stats">
          <div><dt>Cases</dt><dd className="measured">{live.cases_total}</dd></div>
          <div><dt>Fixed with no human click</dt><dd className="measured">{pct(live.auto_resolved_rate)}</dd></div>
          <div><dt>Handed to a person</dt><dd className="measured">{pct(live.escalation_rate)}</dd></div>
          <div><dt>Waiting for approval</dt><dd className="measured">{live.pending_approvals}</dd></div>
          <div><dt>Median time to a verified fix</dt><dd className="measured">{seconds(live.median_seconds_to_verified)}</dd></div>
          <div><dt>Started by Sahayak</dt><dd className="measured">{live.proactive_cases}</dd></div>
        </dl>
      </div>
      {Object.keys(live.by_outcome).length > 0 && (
        <table className="table">
          <thead><tr><th>Outcome</th><th className="num">Cases</th></tr></thead>
          <tbody>
            {Object.entries(live.by_outcome).map(([k, v]) => (
              <tr key={k}><td>{label(OUTCOME, k)}</td><td className="num measured">{v}</td></tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function Evaluation({ e }) {
  if (!e) {
    return (
      <section className="state">
        <h2>No evaluation results yet</h2>
        <p>Run the scenario suite to score Sahayak by what changed in the systems: uv run python -m eval.run_eval --k 3</p>
      </section>
    );
  }
  const passK = e[`pass^${e.k}`];
  return (
    <section>
      <h2 className="section">Scenario evaluation</h2>
      <p className="muted">
        {e.scenarios} scripted scenarios, each run {e.k} {e.k === 1 ? "time" : "times"}, scored by the end state of the systems. From <span className="measured">{e.file}</span>.
      </p>
      <div className="panels panels--live">
        <Reading value={pct(e.unsafe_action_rate)} caption="unsafe actions" />
        <dl className="stats">
          <div><dt>Runs that passed</dt><dd className="measured">{pct(e["pass^1"])}</dd></div>
          {e.k > 1 && <div><dt>Scenarios that passed every run</dt><dd className="measured">{pct(passK)}</dd></div>}
          <div><dt>Gate chose the expected tier</dt><dd className="measured">{pct(e.tier_routing_accuracy)}</dd></div>
          <div><dt>Offers without consent</dt><dd className="measured">{e.consent_violations}</dd></div>
          <div><dt>Closed while systems disagreed</dt><dd className="measured">{e.closed_with_mismatch}</dd></div>
          <div><dt>Median seconds per case</dt><dd className="measured">{seconds(e.median_seconds)}</dd></div>
        </dl>
      </div>
      <table className="table">
        <thead><tr><th>Category</th><th className="num">Runs that passed</th></tr></thead>
        <tbody>
          {Object.entries(e.by_category).map(([k, v]) => (
            <tr key={k}><td>{CATEGORY[k] || k[0].toUpperCase() + k.slice(1)}</td><td className="num measured">{pct(v)}</td></tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Models({ models }) {
  if (!models) {
    return (
      <section className="state">
        <h2>Running without Gemini</h2>
        <p>The server uses the offline stand-in model. Set GEMINI_API_KEY and restart to use the live cascade.</p>
      </section>
    );
  }
  return (
    <section>
      <h2 className="section">Models, best first</h2>
      <p className="muted">Each call goes to the best model answering right now and falls back down this list.</p>
      <table className="table table--stack">
        <thead><tr><th>Model</th><th>Now</th><th className="num">Answered</th><th className="num">Failed</th><th>Last result</th></tr></thead>
        <tbody>
          {Object.entries(models).map(([name, h]) => (
            <tr key={name}>
              <td className="measured" data-label="Model">{name}</td>
              <td data-label="Now">{h.up ? <Chip tone="instrument">Available</Chip> : <Chip>Resting {Math.round(h.retry_in_seconds)} s</Chip>}</td>
              <td className="num measured" data-label="Answered">{h.ok}</td>
              <td className="num measured" data-label="Failed">{h.failed}</td>
              <td className="muted" data-label="Last result">{h.last_outcome || "Not used yet"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
