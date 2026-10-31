import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { usePoll, useMerchants } from "../hooks";
import { Button, Chip } from "../ui";
import { CAUSE, FAULTS, label } from "../labels";

export default function Controls() {
  return (
    <div className="page">
      <div className="toolbar">
        <h1>Demo controls</h1>
        <p className="muted">Break a merchant on purpose, let Sahayak find it, or take its autonomy away.</p>
      </div>
      <div className="panels panels--controls">
        <BreakSomething />
        <Scan />
        <KillSwitch />
        <Reset />
      </div>
    </div>
  );
}

function BreakSomething() {
  const merchants = useMerchants();
  const [merchant, setMerchant] = useState("M1013");
  const [fault, setFault] = useState(FAULTS[0].id);
  const [deviceFault, setDeviceFault] = useState("POWER");
  const [amount, setAmount] = useState("50000");
  const [state, setState] = useState({ busy: false, done: null, error: null });
  const f = FAULTS.find((x) => x.id === fault);

  const run = async (e) => {
    e.preventDefault();
    setState({ busy: true, done: null, error: null });
    try {
      const body = { merchant_id: merchant };
      if (f.device) body.device_fault = deviceFault;
      if (f.amount) body.amount_inr = Number(amount);
      await api.fault(fault, body);
      setState({ busy: false, done: `${f.label} applied to ${merchant}.`, error: null });
    } catch (err) {
      setState({ busy: false, done: null, error: err.message });
    }
  };

  return (
    <form className="panel" onSubmit={run}>
      <h2>Break a merchant</h2>
      <label className="field">
        <span>Merchant</span>
        <select value={merchant} onChange={(e) => setMerchant(e.target.value)}>
          {merchants.list.map((m) => <option key={m.id} value={m.id}>{m.owner_name} ({m.id})</option>)}
        </select>
      </label>
      <label className="field">
        <span>Problem</span>
        <select value={fault} onChange={(e) => setFault(e.target.value)}>
          {FAULTS.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
        </select>
        <small className="helper">{f.hint}</small>
      </label>
      {f.device && (
        <label className="field">
          <span>How it fails</span>
          <select value={deviceFault} onChange={(e) => setDeviceFault(e.target.value)}>
            <option value="SOFT">Firmware hang: a remote restart fixes it</option>
            <option value="POWER">Needs the merchant to switch it off and on</option>
            <option value="HARD">Hardware fault: needs a replacement</option>
          </select>
        </label>
      )}
      {f.amount && (
        <label className="field">
          <span>Amount in rupees</span>
          <input type="number" inputMode="numeric" min="1" name="amount" value={amount} onChange={(e) => setAmount(e.target.value)} />
        </label>
      )}
      <div className="actions">
        <button type="submit" className="btn btn--primary" disabled={state.busy} aria-busy={state.busy || undefined}>Apply the problem</button>
      </div>
      <p className="helper" role="status">
        {state.done && <>{state.done} Now <Link to="/chat">message as that merchant</Link> or run the scan.</>}
        {state.error && <span className="field-error">{state.error}</span>}
      </p>
    </form>
  );
}

function Scan() {
  const [state, setState] = useState({ busy: false, started: null, error: null });
  const run = async () => {
    setState({ busy: true, started: null, error: null });
    try {
      const r = await api.scan({ limit: 20 });
      setState({ busy: false, started: r.started, error: null });
    } catch (err) {
      setState({ busy: false, started: null, error: err.message });
    }
  };
  return (
    <section className="panel">
      <h2>Find problems first</h2>
      <p className="muted">Checks every merchant and starts a case wherever the data proves a problem nobody has raised yet.</p>
      <div className="actions"><Button busy={state.busy} onClick={run}>Scan all merchants</Button></div>
      {state.error && <p className="field-error" role="alert">{state.error}</p>}
      {state.started && (
        state.started.length === 0 ? (
          <p className="helper">No new problems. Merchants with an open ticket or case are skipped.</p>
        ) : (
          <ul className="proof">
            {state.started.map((s) => (
              <li key={s.thread_id}>
                <Link to={`/cases/${s.thread_id}`}>{s.merchant_id}</Link>
                <span>{s.causes.map((c) => label(CAUSE, c)).join(", ")}</span>
              </li>
            ))}
          </ul>
        )
      )}
    </section>
  );
}

function KillSwitch() {
  const policy = usePoll(() => api.policy(), [], 5000);
  const [busy, setBusy] = useState(false);
  const on = policy.data?.kill_switch;
  const flip = async () => {
    setBusy(true);
    try {
      await api.killSwitch(!on);
      await policy.reload();
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="panel">
      <h2>Kill switch</h2>
      <p className="muted">When on, every action that changes a merchant’s systems waits for a person, whatever the confidence.</p>
      <p>{policy.data ? (on ? <Chip tone="person">On: a person approves everything</Chip> : <Chip tone="instrument">Off: the policy decides</Chip>) : <Chip>Checking</Chip>}</p>
      <div className="actions">
        <Button variant={on ? "secondary" : "person"} busy={busy} disabled={!policy.data} onClick={flip}>
          {on ? "Turn the kill switch off" : "Turn the kill switch on"}
        </Button>
      </div>
    </section>
  );
}

function Reset() {
  const dialog = useRef(null);
  const [typed, setTyped] = useState("");
  const [state, setState] = useState({ busy: false, done: null, error: null });
  const run = async (e) => {
    e.preventDefault();
    setState({ busy: true, done: null, error: null });
    try {
      const r = await api.resetDemo();
      setState({ busy: false, done: `Reseeded ${r.seeded?.merchants ?? ""} merchants.`, error: null });
      dialog.current?.close();
      setTyped("");
    } catch (err) {
      setState({ busy: false, done: null, error: err.message });
    }
  };
  return (
    <section className="panel">
      <h2>Reset the demo data</h2>
      <p className="muted">Puts every mock merchant back to its starting state. Cases already shown here stay in the list.</p>
      <div className="actions"><Button onClick={() => dialog.current?.showModal()}>Reset the demo data</Button></div>
      {state.done && <p className="helper" role="status">{state.done}</p>}
      <dialog ref={dialog} className="dialog" aria-labelledby="reset-title">
        <form onSubmit={run}>
          <h2 id="reset-title">Reset every mock merchant?</h2>
          <p className="muted">This cannot be undone. Type <span className="measured">reset</span> to confirm.</p>
          <label className="field">
            <span>Confirmation</span>
            <input name="confirm" autoComplete="off" value={typed} onChange={(e) => setTyped(e.target.value)} />
          </label>
          {state.error && <p className="field-error" role="alert">{state.error}</p>}
          <div className="actions">
            <button type="button" className="btn btn--secondary" onClick={() => dialog.current?.close()}>Keep the data</button>
            <button type="submit" className="btn btn--primary" disabled={typed !== "reset" || state.busy}>Reset the data</button>
          </div>
        </form>
      </dialog>
    </section>
  );
}
