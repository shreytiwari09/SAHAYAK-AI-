# Payments debited but not credited

## Pattern
A customer paid (their account was debited) but the merchant was never credited. The
transaction shows status DEBITED_NOT_CREDITED.

## Fix
Raise a credit adjustment to the merchant (create_refund_request) for at most the transaction
amount, linked to the transaction id. Money-moving: amounts above the auto limit in policy.yaml
need one human approval click; amounts above the approval limit go to a human.
Verify the adjustment reaches POSTED before telling the merchant the money has been credited.
