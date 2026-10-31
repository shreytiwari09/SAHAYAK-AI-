// Plain words for the names the backend uses internally.

export const CAUSE = {
  KYC_HOLD: "KYC hold",
  KYC_EXPIRING: "KYC expiring",
  KYC_NAME_MISMATCH: "KYC name mismatch",
  BANK_ACCOUNT_FAILED: "Bank account rejected",
  SETTLEMENT_STUCK: "Settlement stuck",
  RISK_HOLD: "Risk review hold",
  DEVICE_OFFLINE: "Device offline",
  RENTAL_DESYNC: "Rent charged after return",
  DEBITED_NOT_CREDITED: "Debited, not credited",
};

export const ACTION = {
  send_kyc_link: "Send KYC link",
  raise_bank_update: "Send bank details link",
  retrigger_settlement: "Re-trigger settlement",
  remote_device_reset: "Restart device remotely",
  book_device_replacement: "Book a replacement",
  stop_rental: "Stop the rent",
  reverse_rental_charge: "Reverse a rent charge",
  create_refund_request: "Credit the merchant",
  create_ticket: "Open a ticket",
  update_ticket: "Update the ticket",
  close_ticket: "Close the ticket",
  send_message: "Message the merchant",
  record_consent: "Record the consent",
  create_lead: "Create a lending lead",
};

export const OUTCOME = {
  RESOLVED_VERIFIED: "Fixed and verified",
  ESCALATED: "Handed to a person",
  WAITING_MERCHANT: "Waiting for the merchant",
  INFO_ONLY: "Answered",
  REFUSED: "Declined to share",
  NOTHING_TO_DO: "Nothing to do",
  OFFER_ACCEPTED: "Offer accepted",
  OFFER_DECLINED: "Offer declined",
};

export const WAITING = {
  kyc_link: "tap the KYC link",
  bank_update: "update the bank details",
  power_cycle: "switch the device off and on",
};

export const LANGUAGE = {
  hi: "Hindi", en: "English", ta: "Tamil", te: "Telugu", kn: "Kannada", ml: "Malayalam",
  mr: "Marathi", gu: "Gujarati", bn: "Bengali", pa: "Punjabi", od: "Odia",
};

export const FAULTS = [
  { id: "kyc_hold", label: "Settlement held for KYC", hint: "The merchant's latest settlement stops until KYC is redone." },
  { id: "bank_fail", label: "Bank account rejected", hint: "The settlement fails because the account details are refused." },
  { id: "settlement_stuck", label: "Settlement stuck past T+1", hint: "A settlement is overdue with no reason shown." },
  { id: "heartbeat_loss", label: "Soundbox goes silent", hint: "The device stops sending heartbeats.", device: true },
  { id: "rental_desync", label: "Rent charged after a return", hint: "A returned device is still billed." },
  { id: "high_refund", label: "Customer debited, merchant not credited", hint: "A payment left the customer but never reached the merchant.", amount: true },
  { id: "risk_flag", label: "Risk review hold", hint: "The settlement is held for a person to review." },
];

export const label = (map, key) => map[key] || key || "Unknown";

export const humanize = (key) =>
  String(key || "").toLowerCase().replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

// What state a case is in, as one phrase and a tone (person = needs someone, instrument = working).
export function caseState(c) {
  if (c.status === "error") return { tone: "bad", text: "Failed" };
  if (c.status === "running") return { tone: "instrument", text: "Working" };
  if (c.status === "paused") {
    const p = c.pause || {};
    if (p.type === "approval") return { tone: "person", text: "Needs approval" };
    if (p.type === "offer_reply") return { tone: "person", text: "Waiting for the merchant's answer" };
    return { tone: "person", text: "Waiting for the merchant" };
  }
  if (c.outcome === "ESCALATED") return { tone: "person", text: OUTCOME.ESCALATED };
  if (c.outcome === "RESOLVED_VERIFIED") return { tone: "ok", text: OUTCOME.RESOLVED_VERIFIED };
  return { tone: "quiet", text: label(OUTCOME, c.outcome) };
}

// One sentence on what the case is waiting for or has done.
export function caseSentence(c) {
  if (c.status === "error") return c.error || "The case stopped with an error.";
  if (c.status === "running") return "Sahayak is working on it.";
  if (c.status === "paused") {
    const p = c.pause || {};
    if (p.type === "approval") return `Waiting for an approver: ${p.summary}.`;
    if (p.type === "offer_reply") return "The loan offer has been sent. Waiting for the merchant to answer.";
    return `Waiting for the merchant to ${WAITING[p.waiting_for] || "act"}.`;
  }
  if (c.outcome === "ESCALATED") return c.escalation_reason ? `Handed to a person: ${c.escalation_reason}.` : "Handed to a person.";
  if (c.outcome === "RESOLVED_VERIFIED") return "Fixed, and the systems were checked again to confirm it.";
  if (c.outcome === "WAITING_MERCHANT") return "The merchant did not complete the step, so the ticket stays open.";
  return label(OUTCOME, c.outcome);
}
