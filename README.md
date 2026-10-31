# Sahayak AI

An AI teammate for Paytm merchant support and sales. It reads a merchant's WhatsApp message (text or a Hindi voice note), checks their account, fixes the problem, proves the fix worked, and hands over to a person when it should not act alone.

Built by Team Cognition for the Paytm Build for India AI Hackathon, Track 3: Autonomous AI Teammates.

## Architecture

```
 MERCHANT                         PAYTM STAFF / DEMO
 WhatsApp (text, voice)           Console web app   (also a terminal CLI)
      │                                  │
      ▼                                  ▼
┌──────────────────────── CHANNELS & API  (FastAPI) ────────────────────────┐
│ WhatsApp webhook · voice (Sarvam hears/speaks, ffmpeg converts)           │
│ /chat /cases /approvals /events (live) /audit /metrics /monitor /faults   │
│ Case service: tracks every case, resumes paused ones, proactive monitor   │
└──────────────────────────────────┬────────────────────────────────────────┘
                                   ▼
┌─────────────────── THE AGENT  (LangGraph state machine) ──────────────────┐
│ 1 Read the message + fetch the account (in parallel)                      │
│ 2 Diagnose: 3 independent attempts + a checker  ──►  confidence score     │
│ 3 Plan the next step (from a fixed playbook driven by system data)        │
│ 4 SAFETY GATE ──► AUTO  /  ask a person (approval)  /  escalate           │
│ 5 Act ──► wait for merchant or approver if needed ──► VERIFY ──► reply    │
│ 6 If fixed and the merchant agrees: consented loan offer                  │
│        (every pause is saved, so it survives a restart)                   │
└───────┬────────────────────────┬───────────────────────┬──────────────────┘
        ▼                        ▼                       ▼
  LLM LAYER                 GUARDRAILS               TOOL LAYER
  Gemini cascade:           policy file (tiers,      typed requests with
  best available →          limits), PII masking,    unique keys (no double
  lighter → Gemma           number checks, attack    actions)
  answers validated         detection                      │
  as structured data                                       ▼
                                              PAYTM SYSTEMS (simulated)
                                              ledger · settlement · KYC ·
                                              devices · billing · tickets ·
                                              lending   (also exposed via MCP)

  ALONGSIDE: audit trail (tamper-evident) · saved case state · procedure
  library (SOPs) · 38-scenario evaluation suite
```

### Components

| Layer | What it does | Built with |
|---|---|---|
| Channels | Where merchants and staff reach it | WhatsApp Cloud API, React console, terminal CLI |
| Voice | Hears voice notes, speaks replies | Sarvam (Saaras, Bulbul), ffmpeg |
| API and case service | Starts and tracks cases, serves the console, resumes paused cases | FastAPI |
| Agent | The step-by-step loop above | LangGraph |
| LLM layer | Understands, diagnoses, verifies and writes replies | Gemini, with fallbacks |
| Guardrails | Decides what may run and protects data | Policy file, Presidio, checks in code |
| Tool layer | The only way to touch Paytm's systems | Typed REST client |
| Paytm systems | Hold the merchant data | A simulated service, swappable for real APIs |
| Audit and state | Records every step, saves progress | Hash-chained log, SQLite (Postgres supported) |

### One case, end to end

1. A merchant sends a voice note on WhatsApp.
2. Sarvam turns it into text. Sahayak reads it and fetches the merchant's account at the same time.
3. It diagnoses a KYC hold, and the account data agrees with the diagnosis, so confidence is high.
4. The safety gate lets "send KYC link" through on its own.
5. The merchant taps the link. The server notices and carries on.
6. It re-triggers the settlement, re-checks the systems, and finds it credited with a UTR.
7. It replies in Hindi, as text and a voice note, and closes the ticket as verified.
8. Every step is logged and visible in the console.

### Design choices

- **The AI proposes, rules decide.** The model never picks its own permission level. A policy file sets three tiers: act alone, one human click, or a person takes over.
- **Remedy from data.** The next step comes from a playbook over system data, so a wrong or missing AI answer cannot cause a wrong action.
- **Verify before closing.** A ticket closes only after the systems are re-read and agree: settlement credited with a UTR, device back online, billing matching the device.
- **No direct data access.** The agent can only make a short list of approved requests. Each carries a unique key, so retries never act twice.
- **Pausable.** Waiting for a person or the merchant is a saved state that survives a restart.
- **Private data stays hidden.** PAN, Aadhaar, UPI IDs and phone numbers are replaced by placeholders before the model sees anything.
- **Swappable parts.** The model, the speech engine and the Paytm systems each sit behind a plain interface.

### Status

- **Built and tested:** the agent, safety gate, tool layer, audit log, PII masking, WhatsApp channel code, Hindi voice, HTTP API, operations console, proactive monitor, and a 38-scenario evaluation suite.
- **Simulated:** all Paytm systems run as a mock service behind defined API contracts, with fictional merchants. This is not a production Paytm integration.
- **Not wired yet:** a live WhatsApp number (the channel is tested against Meta's message format and a local simulator) and an optional MCP route for the tools.
- **Storage:** development runs on SQLite. Postgres is supported but has not been run.
