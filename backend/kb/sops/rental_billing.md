# Rental and billing

## Plans
Each device has a billing plan (for example Soundbox Monthly ₹125, Soundbox Display ₹149,
Zero Rental ₹0). Rent is deducted monthly, usually from the settlement on the 1st.

## Rent charged after a device was returned (billing out of sync)
If the device is RETURNED or INACTIVE but the plan still has rental active, billing disagrees
with reality. This is a known complaint pattern: earlier tickets were closed because one system
still showed the device as active. Fix:
1. Stop the rental on that plan (stop_rental). Allowed without a human.
2. Reverse every deduction made after the return date (reverse_rental_charge). Moves money, so
   each reversal needs one human approval click.
3. Verify: plan rental_active is false AND every post-return deduction shows reversed. Only then
   close the ticket. Never close while billing and device status disagree.

## Explaining a deduction
If the device is active and the deduction matches the plan, explain it with the plan name, the
monthly amount and the date. Do not reverse valid charges.
