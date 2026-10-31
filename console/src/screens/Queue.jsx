import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { usePoll, useMerchants } from "../hooks";
import { Button, Chip, Icon, Region, Skeleton } from "../ui";
import { ACTION, CAUSE, humanize, label } from "../labels";
import { ago, inr } from "../format";

export default function Queue() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "handoffs" ? "handoffs" : "approvals";
  const approvals = usePoll(() => api.approvals(), [], 2000);
  const handoffs = usePoll(() => api.escalations(), [], 4000);
  const merchants = useMerchants();

  const pick = (value) => setParams(value === "approvals" ? {} : { tab: value });
  const waiting = approvals.data?.length || 0;

  return (
    <div className="page">
      <div className="toolbar">
        <h1>Needs a person</h1>
        <div className="pickers" role="group" aria-label="Queue">
          <button type="button" className="chip chip--pick" aria-pressed={tab === "approvals"} onClick={() => pick("approvals")}>
            Approvals{waiting ? ` (${waiting})` : ""}
          </button>
          <button type="button" className="chip chip--pick" aria-pressed={tab === "handoffs"} onClick={() => pick("handoffs")}>
            Handoffs
          </button>
        </div>
      </div>
      {tab === "approvals" ? (
        <Approvals query={approvals} merchants={merchants} />
      ) : (
        <Handoffs query={handoffs} merchants={merchants} />
      )}
    </div>
  );
}

const total = (explain = "") => explain.match(/^([\d.]+)\s*=/)?.[1] ?? "";

// The gate's reasons name actions by their internal id; show them in plain words.
const plain = (reason = "") =>
  reason.replace(/'([a-z_]+)'/g, (_, id) => ACTION[id] || id.replace(/_/g, " "));

function Approvals({ query, merchants }) {
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);

  const decide = async (id, approved) => {
    setBusy(id);
    setError(null);
    try {
      await api.decide(id, approved);
      await query.reload();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Region
      query={query}
      skeleton={<Skeleton rows={4} />}
      empty={{
        title: "Nothing is waiting for approval",
        body: "When Sahayak wants to move money or change a device, the request appears here with the evidence.",
        action: <Link className="btn btn--secondary" to="/chat">Try a case that needs approval</Link>,
      }}
    >
      {(rows) => (
        <>
          {error && <p className="field-error" role="alert">{error}</p>}
          <table className="table table--stack">
            <thead>
              <tr>
                <th>Merchant</th><th>Wants to</th><th className="num">Amount</th>
                <th>Why a person</th><th className="num">Confidence</th><th><span className="visually-hidden">Decision</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => {
                const r = c.request;
                const m = merchants.byId[c.merchant_id];
                return (
                  <tr key={c.thread_id}>
                    <td data-label="Merchant">
                      <Link className="rowlink" to={`/cases/${c.thread_id}`}>{m?.owner_name || c.merchant_id}</Link>
                      <span className="sub muted">{ago(c.started_at)}</span>
                    </td>
                    <td data-label="Wants to">
                      {label(ACTION, r.action)}
                      <span className="sub muted">{r.summary}</span>
                    </td>
                    <td className="num measured" data-label="Amount">{r.amount_inr > 0 ? inr(r.amount_inr) : "—"}</td>
                    <td data-label="Why a person">
                      {plain(r.reason)}
                      <span className="sub muted">{(r.rules || []).map(humanize).join(", ")}</span>
                    </td>
                    <td className="num measured" data-label="Confidence">{total(r.confidence)}</td>
                    <td>
                      <div className="cell-actions">
                        <Button variant="person" busy={busy === c.thread_id} onClick={() => decide(c.thread_id, true)}>
                          <Icon name="check" /> Approve
                        </Button>
                        <Button busy={busy === c.thread_id} onClick={() => decide(c.thread_id, false)}>Reject</Button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}
    </Region>
  );
}

function Handoffs({ query, merchants }) {
  return (
    <Region
      query={query}
      skeleton={<Skeleton rows={4} />}
      empty={{
        title: "No handoffs yet",
        body: "When Sahayak should not act alone, the whole case lands here so nobody asks the merchant to repeat themselves.",
      }}
    >
      {(rows) => (
        <ul className="handoffs">
          {rows.map((c) => {
            const h = c.handoff || {};
            const m = merchants.byId[c.merchant_id];
            return (
              <li key={c.thread_id}>
                <details>
                  <summary>
                    <span className="handoffs__who">{m?.owner_name || c.merchant_id}</span>
                    <span className="handoffs__why">{h.reason}</span>
                    <span className="muted num">{ago(c.started_at)}</span>
                  </summary>
                  <div className="handoff">
                    {h.merchant_words_masked && (
                      <div>
                        <h3>The merchant wrote</h3>
                        <blockquote className="said">{h.merchant_words_masked}</blockquote>
                      </div>
                    )}
                    <div>
                      <h3>What the systems show</h3>
                      {h.findings?.length ? (
                        <ul className="proof">
                          {h.findings.map((f) => (
                            <li key={f.cause}>
                              <span>{label(CAUSE, f.cause)}. {f.detail}</span>
                              <span className="measured">{f.amount_inr ? inr(f.amount_inr) : ""}</span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className="muted">Nothing wrong was found in the account data.</p>
                      )}
                    </div>
                    <div>
                      <h3>What was tried</h3>
                      {h.actions_tried?.length ? (
                        <ul className="proof">
                          {h.actions_tried.map((a, i) => (
                            <li key={i}>
                              <span>{label(ACTION, a.action)}</span>
                              <Chip tone={a.executed ? "instrument" : "person"}>{a.executed ? "Done" : "Held back"}</Chip>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className="muted">Nothing was done before handing over.</p>
                      )}
                    </div>
                    <div>
                      <h3>Why the gate stopped it</h3>
                      <p>{h.gate?.reason || h.reason}</p>
                      {h.confidence && <p className="muted measured">{h.confidence}</p>}
                    </div>
                    <div>
                      <h3>Already told the merchant</h3>
                      <blockquote className="said">{h.suggested_reply}</blockquote>
                    </div>
                    <p><Link className="btn btn--secondary" to={`/cases/${c.thread_id}`}>Open the full case</Link></p>
                  </div>
                </details>
              </li>
            );
          })}
        </ul>
      )}
    </Region>
  );
}
