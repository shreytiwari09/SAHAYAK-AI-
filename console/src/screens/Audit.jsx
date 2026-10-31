import { Fragment, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { usePoll, useMerchants } from "../hooks";
import { Button, Chip, Region, Skeleton } from "../ui";
import { ACTION, humanize, label } from "../labels";
import { clock, shortId } from "../format";

export default function Audit() {
  const [params, setParams] = useSearchParams();
  const thread = params.get("thread") || "";
  const merchant = params.get("merchant") || "";
  const query = usePoll(() => api.audit({ thread_id: thread, merchant_id: merchant, limit: 500 }), [thread, merchant], 3000);
  const chain = usePoll(() => api.auditVerify(), [], 10000);
  const merchants = useMerchants();
  const [open, setOpen] = useState(null);

  const set = (key, value) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next);
  };

  return (
    <div className="page">
      <div className="toolbar">
        <h1>Audit trail</h1>
        {chain.data && (
          chain.data.intact
            ? <Chip tone="ok">Chain intact: no entry edited or removed</Chip>
            : <Chip tone="bad">Chain broken at entry {chain.data.first_bad_seq}</Chip>
        )}
      </div>
      <div className="filters">
        <label className="field">
          <span>Merchant</span>
          <select value={merchant} onChange={(e) => set("merchant", e.target.value)}>
            <option value="">All merchants</option>
            {merchants.list.map((m) => <option key={m.id} value={m.id}>{m.owner_name} ({m.id})</option>)}
          </select>
        </label>
        {thread && (
          <p className="filters__note">
            Showing one case, <span className="measured">{shortId(thread)}</span>.{" "}
            <Button onClick={() => set("thread", "")}>Show all cases</Button>
          </p>
        )}
      </div>
      <Region
        query={query}
        skeleton={<Skeleton rows={8} />}
        empty={{
          title: "No entries yet",
          body: "Every step Sahayak takes is recorded here: what it saw, what the gate decided, who approved, and what the systems returned.",
          action: merchant || thread ? <Button onClick={() => setParams({})}>Clear the filters</Button> : null,
        }}
      >
        {(rows) => (
          <table className="table table--stack table--audit">
            <thead>
              <tr><th className="num">Entry</th><th>Time</th><th>Case</th><th>Step</th><th>By</th><th>Detail</th></tr>
            </thead>
            <tbody>
              {[...rows].reverse().map((e) => {
                const person = e.actor && !e.actor.startsWith("sahayak");
                const isOpen = open === e.seq;
                return (
                  <Fragment key={e.seq}>
                    <tr>
                      <td className="num measured" data-label="Entry">{e.seq}</td>
                      <td className="measured cool" data-label="Time">{clock(e.ts)}</td>
                      <td data-label="Case">
                        {e.thread_id === "policy" ? <span className="muted">Policy</span> : (
                          <Link className="measured" to={`/cases/${e.thread_id}`}>{shortId(e.thread_id)}</Link>
                        )}
                      </td>
                      <td data-label="Step">{humanize(e.event)}</td>
                      <td className={person ? "person" : "cool"} data-label="By">{person ? e.actor : "Sahayak"}</td>
                      <td data-label="Detail">
                        {e.action ? label(ACTION, e.action) : ""}
                        {e.gate?.tier ? <> <Chip tone={e.gate.tier === "AUTO" ? "instrument" : "person"}>{e.gate.tier}</Chip></> : null}
                        <div className="cell-actions cell-actions--start">
                          <Button aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : e.seq)}>
                            {isOpen ? "Hide record" : "Show record"}
                          </Button>
                        </div>
                      </td>
                    </tr>
                    {isOpen && (
                      <tr className="table__extra">
                        <td colSpan={6}>
                          <pre className="record measured">{JSON.stringify(e, null, 2)}</pre>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </Region>
    </div>
  );
}
