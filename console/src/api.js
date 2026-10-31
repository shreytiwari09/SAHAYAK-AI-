// The Sahayak HTTP API. In development requests go through the Vite proxy at /api;
// in production the console is served by the API itself, so paths are relative to the origin.
export const BASE = import.meta.env.VITE_API_BASE ?? (import.meta.env.DEV ? "/api" : "");

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function request(method, path, body) {
  let res;
  try {
    res = await fetch(BASE + path, {
      method,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "The Sahayak server is not reachable.");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
    } catch {
      /* keep the status text */
    }
    throw new ApiError(res.status, detail);
  }
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res.text();
}

const qs = (params) => {
  const clean = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return clean.length ? "?" + new URLSearchParams(clean).toString() : "";
};

export const api = {
  health: () => request("GET", "/health"),
  merchants: () => request("GET", "/merchants"),
  agents: () => request("GET", "/agents"),
  cases: (params = {}) => request("GET", "/cases" + qs(params)),
  case: (id) => request("GET", `/cases/${id}`),
  approvals: () => request("GET", "/approvals"),
  decide: (id, approved, approver = "console") =>
    request("POST", `/approvals/${id}`, { approved, approver }),
  escalations: () => request("GET", "/escalations"),
  chat: (merchant_id, text) => request("POST", "/chat", { merchant_id, text, channel: "console" }),
  simulate: (id, body) => request("POST", `/cases/${id}/simulate`, body),
  audit: (params = {}) => request("GET", "/audit" + qs(params)),
  auditVerify: () => request("GET", "/audit/verify"),
  metrics: () => request("GET", "/metrics"),
  policy: () => request("GET", "/policy"),
  killSwitch: (on) => request("POST", "/policy/kill-switch", { on, actor: "console" }),
  scan: (body = {}) => request("POST", "/monitor/scan", body),
  fault: (name, body) => request("POST", `/faults/${name}`, body),
  // A merchant tapping a link from a message (the mock Paytm is served under /mock in-process).
  tapLink: (kind, token) => request("GET", `/mock/sim/${kind}/${token}/complete`),
  // The mock Paytm is served under /mock by the API when it runs in-process.
  resetDemo: () => request("POST", "/mock/admin/reset"),
  wa: {
    link: (phone, merchant_id) => request("POST", "/channels/whatsapp/link", { phone, merchant_id }),
    send: (body) => request("POST", "/channels/whatsapp/simulate", body),
    outbox: (after = 0) => request("GET", "/channels/whatsapp/outbox" + qs({ after })),
    audioUrl: (id) => `${BASE}/channels/whatsapp/outbox/${id}/audio`,
  },
  // Live audit events for one case, as server-sent events.
  events: (id) => new EventSource(`${BASE}/events/${id}`),
};
