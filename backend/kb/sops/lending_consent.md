# Offers, consent and lending

## When an offer may be made
Only after the merchant's issue is resolved and verified, sentiment is not negative, and the
merchant is pre-qualified (check_loan_eligibility). Never inside a complaint reply, never to a
merchant with an unresolved issue, a risk flag or KYC not verified.

## Asking for consent
Ask first, as a separate message with Haan / Nahi buttons, saying what the offer is about.
Record the answer (record_consent) with purpose LOAN_OFFER, the KFS notice version, the offer id
and the button payload. This follows the purpose-limitation principle of the DPDP Act 2023.

## Key Fact Statement and cooling-off
Before acceptance show the Key Fact Statement: lender, amount, tenure, APR, processing fee, EMI
and total repayable, and the cooling-off period (at least one day), as required by the RBI
Digital Lending Directions 2025. A lead is created (create_lead) only with a granted consent
for that exact offer; the lending system refuses it otherwise.

## If the merchant says Nahi
Record the refusal and do not offer again for 30 days.
