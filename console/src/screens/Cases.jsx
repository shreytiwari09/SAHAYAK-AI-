import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { usePoll, useCaseEvents, useMerchants } from "../hooks";
import { Button, Chip, Icon, Reading, Region, Skeleton } from "../ui";
import { Timeline, leadReading } from "../timeline";
import { ACTION, CAUSE, LANGUAGE, caseSentence, caseState, humanize, label } from "../labels";
import { ago, clock, inr } from "../format";

const FILTERS = [
  ["", "All"],
  ["running", "Working"],
  ["paused", "Waiting"],
  ["done", "Finished"],
];

// What the case is about: the diagnosed cause, else what tripped the gate, else the kind of question.
const topicOf = (c) =>
  c.diagnosis?.cause
    ? label(CAUSE, c.diagnosis.cause)
    : c.signals?.length
      ? humanize(c.signals[0])
      : c.proactive
        ? "Proactive check"
        : humanize(c.intent || "question");

export default function Cases() {
  const { id } = useParams();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") || "";
  const merchant = params.get("merchant") || "";
  const agent = params.get("agent") || "";
  const query = usePoll(
    () => api.cases({ status, merchant_id: merchant, agent_id: agent }),
    [status, merchant, agent],
    2000,
  );
  const merchants = useMerchants();

  const setParam = (key, value) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next);
  };
  const setStatus = (value) => setParam("status", value);
  const filtered = Boolean(status || merchant || agent);

  return (
    <div className={`split${id ? " split--detail" : ""}`}>
      <section className="split__list" aria-label="Cases">
        <div className="toolbar">
          <h1>Cases</h1>
          <div className="pickers" role="group" aria-label="Filter by state">
            {FILTERS.map(([value, name]) => (
              <button key={name} type="button" className="chip chip--pick" aria-pressed={status === value}
                      onClick={() => setStatus(value)}>
                {name}
              </button>
            ))}
          </div>
        </div>
        <div className="filters">
          <label className="field">
            <span>Merchant</span>
            <select value={merchant} onChange={(e) => setParam("merchant", e.target.value)}>
              <option value="">All merchants</option>
              {merchants.list.map((m) => <option key={m.id} value={m.id}>{m.owner_name} ({m.id})</option>)}
            </select>
          </label>
          {agent && (
            <p className="filters__note">
              Only <span className="measured">{agent}</span>’s merchants.{" "}
              <Button onClick={() => setParam("agent", "")}>Show every rep</Button>
            </p>
          )}
        </div>
        <Region
          query={query}
          skeleton={<Skeleton rows={7} />}
          empty={{
            title: filtered ? "No cases match" : "No cases yet",
            body: filtered
              ? "Clear the filter to see every case."
              : "A case starts when a merchant messages, or when Sahayak notices a problem first.",
            action: filtered ? (
              <Button onClick={() => setParams({})}>Show all cases</Button>
            ) : (
              <Link className="btn btn--secondary" to="/chat">Send a merchant message</Link>
            ),
          }}
        >
          {(cases) => (
            <table className="table table--stack">
              <thead>
                <tr><th>Merchant</th><th>Issue</th><th>State</th></tr>
              </thead>
              <tbody>
                {cases.map((c) => {
                  const m = merchants.byId[c.merchant_id];
                  const s = caseState(c);
                  return (
                    <tr key={c.thread_id} aria-current={c.thread_id === id || undefined}>
                      <td data-label="Merchant">
                        <Link className="rowlink" to={{ pathname: `/cases/${c.thread_id}`, search: params.toString() }}>
                          {m?.owner_name || c.merchant_id}
                        </Link>
                        <span className="sub sub--pair">
                          <span className="measured">{c.merchant_id}</span>
                          {c.agent_name && <span className="muted">{c.agent_name}</span>}
                          <span className="muted">{ago(c.started_at)}</span>
                        </span>
                      </td>
                      <td className="issue" data-label="Issue">{topicOf(c)}</td>
                      <td data-label="State"><Chip tone={s.tone}>{s.text}</Chip></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Region>
      </section>

      <section className="split__detail" aria-label="Case detail">
        {id ? (
          <CaseDetail key={id} id={id} merchants={merchants} listSearch={params.toString()} />
        ) : (
          <div className="state state--quiet">
            <h2>Pick a case</h2>
            <p>Each case shows what Sahayak understood, what it did, why the gate allowed it, and the proof it worked.</p>
          </div>
        )}
      </section>
    </div>
  );
}

export function CaseDetail({ id, merchants, listSearch, embedded }) {
  const query = usePoll(() => api.case(id), [id], 1500);
  const { events, live } = useCaseEvents(id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const act = async (fn) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await query.reload();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Region query={query} skeleton={<Skeleton rows={6} tall />} empty={{ title: "Case not found", body: "It may have been cleared when the server restarted." }}
            isEmpty={() => false}>
      {(c) => {
        const m = merchants?.byId?.[c.merchant_id];
        const s = caseState(c);
        const reading = leadReading(events);
        const pause = c.pause || {};
        return (
          <article className="case">
            {!embedded && (
              <Link className="back" to={{ pathname: "/cases", search: listSearch }}>All cases</Link>
            )}
            <header className="case__head">
              <div>
                <h2>{m?.business_name || c.merchant_id}</h2>
                <p className="muted">
                  {m ? `${m.owner_name}, ${m.city}` : c.merchant_id}
                  {c.language ? `, writes in ${LANGUAGE[c.language] || c.language}` : ""}
                </p>
              </div>
              <div className="case__tags">
                <Chip tone={s.tone}>{s.text}</Chip>
                <Chip>{c.channel}</Chip>
                {c.proactive && <Chip tone="instrument">Started by Sahayak</Chip>}
              </div>
            </header>

            <div className="statebox">
              <p>{caseSentence(c)}</p>
              {c.status === "paused" && pause.type === "approval" && (
                <div className="ask">
                  <p className="ask__what">
                    <span>{label(ACTION, pause.action)}</span>
                    {pause.amount_inr > 0 && <span className="measured">{inr(pause.amount_inr)}</span>}
                  </p>
                  <p className="muted">{pause.reason}</p>
                  <div className="actions">
                    <Button variant="person" busy={busy} onClick={() => act(() => api.decide(id, true))}>
                      <Icon name="check" /> Approve
                    </Button>
                    <Button busy={busy} onClick={() => act(() => api.decide(id, false))}>Reject</Button>
                  </div>
                </div>
              )}
              {c.status === "paused" && pause.type === "merchant_action" && (
                <div className="actions">
                  <Button busy={busy} onClick={() => act(() => api.simulate(id, { done: true }))}>
                    Simulate: the merchant did it
                  </Button>
                  <Button busy={busy} onClick={() => act(() => api.simulate(id, { done: false }))}>
                    Simulate: the merchant did not
                  </Button>
                </div>
              )}
              {c.status === "paused" && pause.type === "offer_reply" && (
                <div className="actions">
                  <Button busy={busy} onClick={() => act(() => api.simulate(id, { reply: "Haan" }))}>Simulate: Haan</Button>
                  <Button busy={busy} onClick={() => act(() => api.simulate(id, { reply: "Nahi" }))}>Simulate: Nahi</Button>
                </div>
              )}
              {error && <p className="field-error" role="alert">{error}</p>}
            </div>

            {reading && (
              <Reading
                value={reading.value}
                caption={<>{reading.caption}, verified <time className="measured cool" dateTime={reading.at}>{clock(reading.at)}</time></>}
              />
            )}

            <h3 className="section">What happened</h3>
            {events.length === 0 ? <Skeleton rows={4} /> : <Timeline events={events} live={live && c.status === "running"} proactive={c.proactive} />}
          </article>
        );
      }}
    </Region>
  );
}
