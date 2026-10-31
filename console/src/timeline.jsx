import { Chip, Icon } from "./ui";
import { ACTION, CAUSE, LANGUAGE, OUTCOME, WAITING, humanize, label } from "./labels";
import { clock, inr, two } from "./format";

const PARTS = [
  ["evidence_rule_match", "Account data agrees"],
  ["self_consistency", "Attempts agree"],
  ["verifier_pass", "Second check"],
  ["schema_valid", "Answer is valid"],
];

// The four checks behind a confidence score, each with its weight, so the number is never opaque.
export function Confidence({ c }) {
  if (!c) return null;
  return (
    <dl className="parts">
      {PARTS.map(([key, name]) => (
        <div key={key} className="parts__item">
          <dt>{name}</dt>
          <dd>
            <span className="measured">{two(c[key])}</span>
            <span className="parts__weight">× {c.weights?.[key]}</span>
          </dd>
          <span className="meter" style={{ "--v": c[key] }} aria-hidden="true" />
        </div>
      ))}
      <div className="parts__item parts__item--total">
        <dt>Confidence</dt>
        <dd><span className="measured">{two(c.total)}</span></dd>
      </div>
    </dl>
  );
}

const TIER = {
  AUTO: { tone: "instrument", head: "Gate let it through", text: "Acts on its own" },
  APPROVE: { tone: "person", head: "Gate asked for a person", text: "Needs one click" },
  ESCALATE: { tone: "person", head: "Gate stopped it", text: "Goes to a person" },
};

function resultLine(action, r = {}) {
  switch (action) {
    case "retrigger_settlement":
      return `${inr(r.amount_inr)} settlement is now ${humanize(r.status).toLowerCase()}`;
    case "remote_device_reset":
      return r.result === "RECOVERED" ? "The device recovered" : "The device did not respond";
    case "reverse_rental_charge":
      return `${inr(r.amount_inr)} reversed, reference ${r.reversal_ref}`;
    case "create_refund_request":
      return `${inr(r.amount_inr)} ${humanize(r.status).toLowerCase()}`;
    case "book_device_replacement":
      return `Replacement booked, tracking ${r.tracking_id}`;
    case "send_kyc_link":
    case "raise_bank_update":
      return "Link sent to the merchant";
    case "stop_rental":
      return "Rent stopped";
    default:
      return "";
  }
}

function Proof({ proof = {}, open = [] }) {
  const rows = [];
  (proof.settlements || []).forEach((s) =>
    rows.push([`${inr(s.amount_inr)} credited`, `UTR ${s.utr}`]));
  (proof.rent_reversed || []).forEach((s) =>
    rows.push([`${inr(s.amount_inr)} rent reversed`, `Reference ${s.reversal_ref}`]));
  (proof.credit_adjustments || []).forEach((s) =>
    rows.push([`${inr(s.amount_inr)} credited back`, humanize(s.status)]));
  (proof.devices_online || []).forEach((d) => rows.push([`${d} is online`, "Heartbeat received"]));
  (proof.replacements || []).forEach((d) => rows.push(["Replacement booked", `Tracking ${d.tracking_id}`]));
  if (!rows.length && !open.length) return null;
  return (
    <ul className="proof">
      {rows.map(([what, ref]) => (
        <li key={what + ref}><span>{what}</span><span className="measured">{ref}</span></li>
      ))}
      {open.length > 0 && (
        <li><span>Still open</span><span className="measured">{open.map((c) => label(CAUSE, c)).join(", ")}</span></li>
      )}
    </ul>
  );
}

// One audit event becomes one entry: who did it, what it means, and the detail worth reading.
function describe(e, ctx) {
  const r = e.result || {};
  const person = e.actor && !e.actor.startsWith("sahayak");
  const who = person ? "person" : "instrument";

  switch (e.event) {
    case "UNDERSTOOD": {
      const c = r.classification || {};
      return {
        who,
        title: ctx.proactive ? "Noticed a problem before the merchant did" : "Read the message and the account",
        body: (
          <>
            {!ctx.proactive && (
              <p>
                Written in {LANGUAGE[c.language] || c.language} ({c.script}), sounds {c.sentiment}, {humanize(c.intent).toLowerCase()}.
              </p>
            )}
            <p>
              Account data shows:{" "}
              <span className="measured">
                {r.findings?.length ? r.findings.map((x) => label(CAUSE, x)).join(", ") : "no problem"}
              </span>
            </p>
            {r.signals?.length > 0 && (
              <p className="chips">
                {r.signals.map((s) => <Chip key={s} tone="person">{humanize(s)}</Chip>)}
              </p>
            )}
          </>
        ),
      };
    }
    case "DIAGNOSED":
      return {
        who,
        title: `Diagnosed: ${label(CAUSE, r.cause)}`,
        body: (
          <>
            {r.action && <p>First step would be: {label(ACTION, r.action)}.</p>}
            <Confidence c={e.confidence} />
          </>
        ),
      };
    case "PLANNED": {
      const n = r.next || {};
      if (n.type === "wait") return { who, title: `Waiting for the merchant to ${WAITING[n.kind] || "act"}` };
      if (n.type === "stalled") return { who, title: "The merchant has not done it yet", muted: true };
      return null; // a step plan is shown by the gate decision that follows
    }
    case "GATE_DECIDED": {
      const t = TIER[e.gate?.tier] || TIER.AUTO;
      return {
        who: t.tone === "person" ? "person" : "instrument",
        title: `${t.head}: ${label(ACTION, e.action)}`,
        body: (
          <>
            <p><Chip tone={t.tone}>{e.gate?.tier}</Chip> {e.gate?.reason}</p>
          </>
        ),
        key: "gate",
      };
    }
    case "EXECUTED":
      return { who, title: `Did it: ${label(ACTION, e.action)}`, body: resultLine(e.action, r) && <p>{resultLine(e.action, r)}.</p> };
    case "EXECUTION_REFUSED":
      return { who, tone: "bad", title: `Paytm systems refused: ${label(ACTION, e.action)}`, body: <p className="measured">{r.code}</p> };
    case "APPROVED":
    case "REJECTED":
      return {
        who: "person",
        title: `${e.actor} ${e.event === "APPROVED" ? "approved" : "rejected"}: ${label(ACTION, e.action)}`,
      };
    case "MERCHANT_RESPONDED":
      return {
        who: "person",
        title: `Merchant ${r.done ? "did" : "did not do"} it: ${WAITING[r.waiting_for] || r.waiting_for}`,
      };
    case "VERIFIED":
      return {
        who,
        tone: e.verified ? "ok" : "bad",
        title: e.verified ? "Checked the systems again: fixed" : "Checked the systems again: not fixed yet",
        icon: e.verified ? "check" : "cross",
        body: <Proof proof={r.proof} open={e.verified ? [] : r.open_causes} />,
      };
    case "RESOLVED":
      return { who, title: "Closed the ticket as verified" };
    case "ESCALATED":
      return {
        who: "person",
        title: "Handed to a person",
        body: r.reason ? <p>{r.reason}.</p> : null,
      };
    case "MESSAGE_SENT":
      return {
        who,
        title: "Messaged the merchant",
        body: <blockquote className="said">{(r.text || "").replace(/https?:\/\/\S+/g, "(link)")}</blockquote>,
      };
    case "OFFER_REPLY":
      return { who: "person", title: `Merchant answered the offer: ${r.reply || "no answer"}` };
    case "CONSENT_RECORDED":
      return { who, title: "Recorded the merchant's consent" };
    case "LEAD_CREATED":
      return { who, title: "Created the lending lead, after consent" };
    case "VOICE_TRANSCRIBED":
      return { who, title: `Transcribed a voice note (${r.provider}, ${LANGUAGE[r.language] || r.language || "language unknown"})` };
    case "MESSAGE_FALLBACK":
      return { who, muted: true, title: `Used a plain template instead: ${r.why}` };
    case "CLASSIFY_FALLBACK":
      return { who, muted: true, title: "The classifier was unavailable, so safe defaults were used" };
    case "VOICE_FAILED":
    case "VOICE_REPLY_FAILED":
    case "DELIVERY_FAILED":
    case "DELIVERY_SKIPPED":
      return { who, tone: "bad", title: humanize(e.event), body: <p>{r.why || r.error}</p> };
    case "CASE_ERROR":
      return { who, tone: "bad", title: "The case stopped with an error", body: <p className="measured">{r.error}</p> };
    case "WAITING_MERCHANT":
    case "NOTHING_TO_DO":
    case "REFUSED":
    case "INFO_ONLY":
      return { who, muted: true, title: label(OUTCOME, e.event) === e.event ? humanize(e.event) : label(OUTCOME, e.event) };
    default:
      return null;
  }
}

export function Timeline({ events, live, proactive }) {
  const entries = events.map((e) => ({ e, d: describe(e, { proactive }) })).filter((x) => x.d);
  return (
    <ol className="trail" aria-live={live ? "polite" : "off"}>
      {entries.map(({ e, d }) => (
        <li key={e.seq} className={`trail__item trail__item--${d.who}${d.muted ? " trail__item--muted" : ""}${d.tone ? ` trail__item--${d.tone}` : ""}`}>
          <time className="measured trail__time" dateTime={e.ts}>{clock(e.ts)}</time>
          <div className="trail__body">
            <p className="trail__title">
              {d.icon && <span className="trail__icon"><Icon name={d.icon} /></span>}
              {d.title}
            </p>
            {d.body}
          </div>
        </li>
      ))}
      {live && (
        <li className="trail__item trail__item--instrument trail__item--waiting">
          <span className="trail__time" />
          <div className="trail__body"><p className="trail__title">Working</p></div>
        </li>
      )}
    </ol>
  );
}

// The lead reading for a case: what was actually fixed, as the systems confirmed it.
export function leadReading(events) {
  const v = [...events].reverse().find((e) => e.event === "VERIFIED" && e.verified);
  if (!v) return null;
  const proof = v.result?.proof || {};
  const sum = (arr = []) => arr.reduce((t, x) => t + (x.amount_inr || 0), 0);
  if (proof.settlements?.length)
    return { value: inr(sum(proof.settlements)), caption: `credited to the bank, ${proof.settlements.length === 1 ? "1 settlement" : `${proof.settlements.length} settlements`}`, at: v.ts };
  if (proof.credit_adjustments?.length)
    return { value: inr(sum(proof.credit_adjustments)), caption: "credited back to the merchant", at: v.ts };
  if (proof.rent_reversed?.length)
    return { value: inr(sum(proof.rent_reversed)), caption: "rent reversed", at: v.ts };
  if (proof.devices_online?.length) return { value: "Online", caption: `${proof.devices_online[0]} is sending heartbeats again`, at: v.ts };
  if (proof.replacements?.length) return { value: "Booked", caption: `replacement on its way, tracking ${proof.replacements[0].tracking_id}`, at: v.ts };
  return { value: "Fixed", caption: "confirmed in the systems", at: v.ts };
}
