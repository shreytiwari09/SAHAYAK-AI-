# WhatsApp and voice: setup and demo

The code is finished and tested. These steps connect it to a real WhatsApp number. About 20 minutes,
all free. Without them everything still runs against a local outbox (see "Try it without Meta").

## 1. Meta developer app (free)
1. Go to <https://developers.facebook.com>, log in, **My Apps → Create App**, choose **Other → Business**.
2. Add the **WhatsApp** product (use case: *Connect with customers through WhatsApp*).
3. **WhatsApp → API Setup** shows a free **test number** and two values to copy:
   - **Phone number ID** → `META_PHONE_ID`
   - **Temporary access token** (lasts 24 h) → `META_WA_TOKEN`
4. Under **To**, add your own phone (and your teammate's, and any judge's) as recipients. Meta verifies
   each with a code. The test number can message **only these recipients, up to 5**.
5. **App settings → Basic → App secret** → `META_APP_SECRET` (makes the server reject forged webhooks).
6. For demo day, replace the 24-hour token: **Business settings → Users → System users → Add**, assign
   the app, **Generate token** with `whatsapp_business_messaging` and `whatsapp_business_management`.

## 2. Configure and run
Add to `sahayak/.env`:
```
META_WA_TOKEN=...
META_PHONE_ID=...
META_APP_SECRET=...
META_VERIFY_TOKEN=any-string-you-choose
WHATSAPP_LINKS=919876543210:M1001      # your phone (country code, no +) : merchant to play
```
Start a public tunnel (free; `brew install cloudflared`), then the server with that address:
```bash
cloudflared tunnel --url http://localhost:8000          # prints https://something.trycloudflare.com
cd sahayak
INPROCESS_MOCK=1 PUBLIC_BASE_URL=https://something.trycloudflare.com \
  uv run uvicorn backend.main:app --port 8000
```
`PUBLIC_BASE_URL` matters: links the agent sends (the KYC link) are built from it, and the mock Paytm is
served under `/mock`, so one tunnel covers both. The free tunnel address changes every run, so repeat
step 3 after restarting it (a named Cloudflare tunnel keeps a fixed address if you have a domain).

## 3. Register the webhook
**WhatsApp → Configuration → Webhook → Edit**
- Callback URL: `https://something.trycloudflare.com/channels/whatsapp/webhook`
- Verify token: the same `META_VERIFY_TOKEN`
- **Verify and save**, then **Manage** and subscribe to **messages**.

## 4. Demo
1. **Message the test number first** from your phone, for example "Mera settlement 2 din se nahi aaya", or
   send a **voice note**. (A merchant must write first: WhatsApp only allows free-form replies within
   24 hours of their last message.)
2. Sahayak replies with the KYC link (and a voice note if you sent one). **Tap the link.** No reply needed:
   the server notices the KYC change and carries on.
3. It re-triggers the settlement, confirms the credit with the UTR, and offers a loan with **Haan / Nahi**
   buttons. Tap one.

Other ready-made scenarios, run first in another terminal (replace the merchant id in `WHATSAPP_LINKS`):
```bash
curl -X POST localhost:8000/faults/heartbeat_loss -H 'content-type: application/json' \
     -d '{"merchant_id":"M1002","device_fault":"POWER"}'        # Soundbox goes silent
curl -X POST localhost:8000/monitor/scan -H 'content-type: application/json' \
     -d '{"merchant_ids":["M1002"]}'                             # Sahayak messages first
```
Pending approvals: `curl localhost:8000/approvals`, approve with `POST /approvals/{id}`.

## Try it without Meta
With no `META_*` values set, replies are recorded instead of sent. Drive the real WhatsApp code path:
```bash
curl -X POST localhost:8000/channels/whatsapp/link -H 'content-type: application/json' \
     -d '{"phone":"919800000001","merchant_id":"M1001"}'
curl -X POST localhost:8000/channels/whatsapp/simulate -H 'content-type: application/json' \
     -d '{"sender":"919800000001","text":"Mera settlement 2 din se nahi aaya"}'
curl localhost:8000/channels/whatsapp/outbox            # what the merchant's phone would receive
```
A voice note works the same way with `"audio_base64": "<base64 of an .ogg/.wav>"`.

## Voice
- **In:** WhatsApp voice notes (Ogg/Opus) are converted by ffmpeg and transcribed by Sarvam Saaras v4 in
  codemix mode, so Hinglish stays readable. If Sarvam fails, Gemini transcribes instead.
- **Out:** if the merchant's last message was a voice note, replies also come as a Hindi (or the
  merchant's language) voice note from Sarvam Bulbul. The text is always sent first, so a failed voice
  reply never leaves them without the message.
- Spoken clips are cached (`data/tts_cache/`), so repeated messages cost no extra credits.
  `GET /channels/speech/stats` shows what has been spent.

## If something goes wrong
| Symptom | Cause and fix |
|---|---|
| Webhook "verify" fails | `META_VERIFY_TOKEN` differs from what you typed in Meta, or the tunnel URL changed |
| No reply, server log shows 403 on the webhook | `META_APP_SECRET` is set but wrong; fix it or clear it while testing |
| Messages stop after about a day | the temporary token expired; use a system-user token |
| Error code 131047 in the audit log (`DELIVERY_FAILED`) | more than 24 h since the merchant's last message; they must write first |
| "Number not linked" reply | add the phone to `WHATSAPP_LINKS` or `POST /channels/whatsapp/link` |
| The KYC link does not open on the phone | `PUBLIC_BASE_URL` is missing or the tunnel is down |
| Voice note not understood | check `GET /channels/speech/stats` errors; ffmpeg must be installed (`brew install ffmpeg`) |
