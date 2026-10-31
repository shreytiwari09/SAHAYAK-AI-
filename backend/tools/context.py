"""fetch_context: pull everything about a merchant in parallel (PRD §5.2, node 2)."""

import asyncio

from pydantic import BaseModel

from backend.tools.client import PaytmTools
from mock_paytm import schemas as s


class MerchantContext(BaseModel):
    profile: s.MerchantProfile
    transactions: s.TransactionSummary
    settlements: list[s.SettlementOut]
    kyc: s.KycStatusOut
    devices: list[s.DeviceOut]
    billing: s.BillingOut
    tickets: list[s.TicketOut]


async def fetch_context(tools: PaytmTools, merchant_id: str, days: int = 7) -> MerchantContext:
    profile, txns, settlements, kyc, devices, billing, tickets = await asyncio.gather(
        tools.get_merchant_profile(merchant_id),
        tools.get_transactions(merchant_id, days),
        tools.get_settlement_status(merchant_id, days),
        tools.get_kyc_status(merchant_id),
        tools.get_device_status(merchant_id),
        tools.get_billing(merchant_id),
        tools.get_ticket_history(merchant_id),
    )
    return MerchantContext(
        profile=profile,
        transactions=txns,
        settlements=settlements,
        kyc=kyc,
        devices=devices,
        billing=billing,
        tickets=tickets,
    )
