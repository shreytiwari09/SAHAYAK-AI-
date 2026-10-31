# Devices (Soundbox and EDC)

## Detecting an offline device
A deployed device sends heartbeats continuously. If a Soundbox or EDC has been silent longer
than the heartbeat timeout it is OFFLINE, even if the merchant has not complained. Sahayak may
message the merchant first (proactive outreach).

## Troubleshooting order
1. Remote reset (remote_device_reset). If the result is RECOVERED, verify the device is online.
2. If NO_RESPONSE, ask the merchant to switch the device off, wait 10 seconds and switch it on
   again (power-cycle). This is a step only the merchant can do; wait for them, then re-check.
3. If still offline after a power-cycle, it is likely a hardware fault: book a replacement.

## Replacement
book_device_replacement needs one human approval click (policy.yaml). In warranty: free.
Out of warranty: chargeable; tell the merchant the charge before booking. Share the tracking id.
Verify the replacement status is BOOKED before confirming to the merchant.

## Returned devices
Never reset or troubleshoot a RETURNED device. If the merchant mentions rent on a returned
device, follow the rental billing SOP.
