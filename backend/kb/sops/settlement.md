# Settlements

## Normal settlement timing
A merchant's collections for a business day are settled to their bank account on T+1, by about
09:00 IST the next day. The settlement record shows the business date, the net amount after any
rental deduction, the expected time, and a UTR (bank reference) once credited. A settlement that
has not reached CREDITED after its expected time is "overdue".

## Diagnosing a missing settlement
Always read the settlement records before saying anything about money. Check, in order:
1. Hold reason on the settlement: KYC_REVERIFICATION, BANK_ACCOUNT_FAILED or RISK_REVIEW.
2. Merchant KYC status and bank account status on the profile.
3. Whether the settlement is PENDING or FAILED and overdue with no hold reason (a stuck batch).
4. Whether a rental deduction reduced the amount (explain, do not treat as missing money).
Never state an amount, date or UTR that did not come from a tool result.

## KYC re-verification hold
Cause: KYC status PENDING_REVERIFICATION or EXPIRED, settlements ON_HOLD with reason
KYC_REVERIFICATION. Fix: send the re-KYC link (send_kyc_link). When the merchant completes it,
held settlements move to PENDING; then re-trigger each one (retrigger_settlement). Verify each
reaches CREDITED with a UTR before telling the merchant the money is on its way to the bank and
before closing the ticket. If the KYC status is NAME_MISMATCH, do not send a link: escalate.

## Bank account failure
Cause: bank status FAILED (for example the IFSC changed after a bank merger); the settlement is
FAILED with reason BANK_ACCOUNT_FAILED. Fix: send a bank-details update link
(raise_bank_update). After the merchant submits it, re-trigger the failed settlement and verify
CREDITED.

## Stuck settlement past T+1
Cause: settlement PENDING or FAILED, overdue, no hold reason, KYC verified, bank active. Fix:
re-trigger. Settlements up to the auto limit in policy.yaml may be re-triggered without a human.
Verify CREDITED with UTR.

## Risk review hold
Cause: settlement ON_HOLD with reason RISK_REVIEW, or the merchant risk flag is set. Sahayak
never re-triggers or explains risk details. Escalate to a human with the case summary and tell
the merchant, politely and without speculation, that a specialist is reviewing it.

## Regulatory clock
RBI's turnaround-time framework for failed transactions (circular RBI/2019-20/67, 20 Sep 2019)
sets auto-reversal deadlines (T+1 for UPI credits not made to the beneficiary, T+5 for merchant
POS/e-commerce cases) and compensation of ₹100 per day of delay beyond them. Sahayak uses these
as SLA timers; check the circular's annex before quoting exact rows.
