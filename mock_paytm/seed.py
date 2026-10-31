"""Deterministic seed data: ~50 merchants, 30 days of transactions, settlements, devices, billing.

Merchants M1001-M1012 are scenario merchants in known states (hero flow, faults, edge cases);
the rest are healthy background merchants. All data is fictional.

    uv run python -m mock_paytm.seed          # drop, recreate and seed the configured database
"""

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlmodel import Session

from mock_paytm import clock
from mock_paytm import models as m
from mock_paytm.config import Settings, get_settings
from mock_paytm.db import get_engine, init_db

# Settlements reach the bank at 09:00 IST (03:30 UTC) on T+1.
SETTLEMENT_TIME_UTC = time(3, 30)

# business type -> (txns per day range, ticket size range in rupees)
PROFILES: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "kirana": ((18, 32), (20, 650)),
    "cafe": ((25, 45), (40, 450)),
    "pharmacy": ((15, 30), (50, 1200)),
    "mobile_shop": ((4, 10), (200, 9000)),
    "textiles": ((6, 14), (500, 12000)),
    "dhaba": ((35, 60), (80, 700)),
    "bakery": ((20, 40), (30, 450)),
    "general_store": ((15, 30), (20, 800)),
    "salon": ((6, 14), (100, 1500)),
    "hardware": ((5, 12), (100, 5000)),
    "electronics": ((3, 8), (500, 20000)),
}

# city, state, language
CITIES = [
    ("Indore", "Madhya Pradesh", "hi"),
    ("Bhopal", "Madhya Pradesh", "hi"),
    ("Lucknow", "Uttar Pradesh", "hi"),
    ("Kanpur", "Uttar Pradesh", "hi"),
    ("Varanasi", "Uttar Pradesh", "hi"),
    ("Patna", "Bihar", "hi"),
    ("Jaipur", "Rajasthan", "hi"),
    ("Delhi", "Delhi", "hi"),
    ("Chennai", "Tamil Nadu", "ta"),
    ("Coimbatore", "Tamil Nadu", "ta"),
    ("Pune", "Maharashtra", "mr"),
    ("Nagpur", "Maharashtra", "mr"),
    ("Surat", "Gujarat", "gu"),
    ("Ahmedabad", "Gujarat", "gu"),
    ("Kolkata", "West Bengal", "bn"),
    ("Hyderabad", "Telangana", "te"),
    ("Bengaluru", "Karnataka", "kn"),
    ("Kochi", "Kerala", "ml"),
    ("Ludhiana", "Punjab", "pa"),
]

FIRST_NAMES = {
    "hi": [
        "Ramesh",
        "Suresh",
        "Sunita",
        "Rajesh",
        "Pooja",
        "Manoj",
        "Anita",
        "Vikas",
        "Neha",
        "Ashok",
    ],
    "ta": ["Murugan", "Lakshmi", "Senthil", "Meena", "Karthik", "Revathi"],
    "mr": ["Sachin", "Kavita", "Ganesh", "Snehal", "Prakash"],
    "gu": ["Anil", "Hetal", "Jignesh", "Bhavna", "Kiran"],
    "bn": ["Subhash", "Rina", "Arup", "Moumita"],
    "te": ["Srinivas", "Padma", "Venkat", "Farida"],
    "kn": ["Manjunath", "Shobha", "Raghu", "Kavya"],
    "ml": ["Deepa", "Anil", "Biju", "Sreeja"],
    "pa": ["Harpreet", "Gurpreet", "Manjit", "Simran"],
}
SURNAMES = {
    "hi": ["Sharma", "Verma", "Gupta", "Yadav", "Singh", "Kumar", "Prasad", "Mishra"],
    "ta": ["Narayanan", "Subramanian", "Rajan", "Pandian"],
    "mr": ["Joshi", "Patil", "Deshmukh", "Kulkarni"],
    "gu": ["Patel", "Shah", "Mehta", "Desai"],
    "bn": ["Das", "Ghosh", "Banerjee", "Sen"],
    "te": ["Reddy", "Rao", "Naidu", "Khan"],
    "kn": ["Gowda", "Hegde", "Shetty"],
    "ml": ["Menon", "Nair", "Pillai"],
    "pa": ["Singh", "Gill", "Sandhu"],
}
SHOP_SUFFIX = {
    "kirana": "Kirana Store",
    "cafe": "Cafe",
    "pharmacy": "Medical Hall",
    "mobile_shop": "Mobile Point",
    "textiles": "Textiles",
    "dhaba": "Dhaba",
    "bakery": "Bakery",
    "general_store": "General Store",
    "salon": "Beauty Parlour",
    "hardware": "Hardware",
    "electronics": "Electronics",
}
IFSC_BANKS = ["SBIN", "HDFC", "ICIC", "PUNB", "BARB", "UBIN", "CNRB", "KKBK"]
VPA_HANDLES = ["okaxis", "oksbi", "okhdfcbank", "ybl", "paytm", "ibl"]
SOUNDBOX_PLANS = [("Soundbox Monthly", 12_500), ("Soundbox Display", 14_900), ("Zero Rental", 0)]


@dataclass
class Spec:
    id: str
    owner: str
    business: str
    business_type: str
    city: str
    state: str
    language: str
    devices: tuple[m.DeviceType, ...] = (m.DeviceType.SOUNDBOX,)
    soundbox_plan: tuple[str, int] | None = None


SCENARIOS = [
    # Hero flow: settlement held for KYC re-verification ("Mera settlement 2 din se nahi aaya").
    Spec(
        "M1001",
        "Ramesh Kumar",
        "Ramesh Kirana Store",
        "kirana",
        "Indore",
        "Madhya Pradesh",
        "hi",
        soundbox_plan=SOUNDBOX_PLANS[0],
    ),
    # Soundbox offline, needs a power-cycle (proactive outreach, dual control).
    Spec(
        "M1002",
        "Lakshmi Narayanan",
        "Lakshmi Filter Coffee",
        "cafe",
        "Chennai",
        "Tamil Nadu",
        "ta",
        (m.DeviceType.SOUNDBOX, m.DeviceType.EDC),
    ),
    # Rental still charged on a returned Soundbox; earlier ticket closed unresolved.
    Spec(
        "M1003",
        "Sunita Sharma",
        "Sharma General Store",
        "general_store",
        "Jaipur",
        "Rajasthan",
        "hi",
        soundbox_plan=SOUNDBOX_PLANS[0],
    ),
    # Bank account rejected (IFSC changed after a bank merger); settlement FAILED.
    Spec(
        "M1004",
        "Mohammed Imran",
        "Imran Mobile Point",
        "mobile_shop",
        "Lucknow",
        "Uttar Pradesh",
        "hi",
    ),
    # Risk review hold: must always escalate.
    Spec("M1005", "Anil Patel", "Patel Textiles", "textiles", "Surat", "Gujarat", "gu"),
    # Settlement stuck PENDING past T+1: a re-trigger fixes it.
    Spec("M1006", "Mohan Prasad", "Prasad Medical Hall", "pharmacy", "Patna", "Bihar", "hi"),
    # KYC expiring in 5 days: proactive reminder.
    Spec("M1007", "Kavita Joshi", "Joshi Bakery", "bakery", "Pune", "Maharashtra", "mr"),
    # Third contact about the same KYC-held settlement: must escalate with history.
    Spec("M1008", "Rakesh Verma", "Verma Kirana", "kirana", "Kanpur", "Uttar Pradesh", "hi"),
    # ₹48,500 customer payment debited but not credited: credit adjustment needs approval.
    Spec(
        "M1009",
        "Deepa Menon",
        "Menon Electronics",
        "electronics",
        "Kochi",
        "Kerala",
        "ml",
        (m.DeviceType.SOUNDBOX, m.DeviceType.EDC),
    ),
    # Healthy, high volume, loan-eligible: consented offer demo.
    Spec("M1010", "Harpreet Singh", "Singh Da Dhaba", "dhaba", "Ludhiana", "Punjab", "pa"),
    # KYC name mismatch: cannot be self-served, must escalate.
    Spec("M1011", "Sanjay Gupta", "Gupta Hardware", "hardware", "Bhopal", "Madhya Pradesh", "hi"),
    # Hardware fault: remote reset and power-cycle fail; replacement needed.
    Spec("M1012", "Farida Khan", "Khan Beauty Parlour", "salon", "Hyderabad", "Telangana", "hi"),
]


class Seeder:
    def __init__(self, session: Session, settings: Settings):
        self.session = session
        self.seed_value = settings.seed_random
        self.rng = random.Random(self.seed_value)
        self.days = settings.seed_days
        self.n_merchants = max(settings.seed_merchants, len(SCENARIOS))
        self.now = clock.now()
        self.today = self.now.date()

    # ----- helpers -----

    def hexid(self, prefix: str, n: int = 10) -> str:
        return f"{prefix}{self.rng.getrandbits(n * 4):0{n}X}"

    def phone(self) -> str:
        return f"{self.rng.choice('6789')}{self.rng.randrange(10**8, 10**9)}"

    def pan(self, surname: str) -> str:
        letters = "".join(self.rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(3))
        digits = self.rng.randrange(1000, 9999)
        check = self.rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ")
        return f"{letters}P{surname[0].upper()}{digits}{check}"

    def vpa(self) -> str:
        return f"{self.rng.randrange(10**9, 10**10)}@{self.rng.choice(VPA_HANDLES)}"

    def at(self, d: date, t: time) -> datetime:
        return datetime.combine(d, t, tzinfo=UTC)

    # ----- generation -----

    def use_rng(self, scope: str) -> None:
        """A generator per merchant, so each merchant's data is the same however many are seeded."""
        self.rng = random.Random(f"{self.seed_value}:{scope}")

    def generic_specs(self) -> list[Spec]:
        specs = []
        for i in range(len(SCENARIOS) + 1, self.n_merchants + 1):
            self.use_rng(f"spec:{i}")
            city, state, lang = self.rng.choice(CITIES)
            first = self.rng.choice(FIRST_NAMES[lang])
            last = self.rng.choice(SURNAMES[lang])
            btype = self.rng.choice(list(PROFILES))
            devices = (m.DeviceType.SOUNDBOX,)
            if btype in ("cafe", "textiles", "electronics", "pharmacy") and self.rng.random() < 0.5:
                devices = (m.DeviceType.SOUNDBOX, m.DeviceType.EDC)
            specs.append(
                Spec(
                    f"M{1000 + i}",
                    f"{first} {last}",
                    f"{last} {SHOP_SUFFIX[btype]}",
                    btype,
                    city,
                    state,
                    lang,
                    devices,
                )
            )
        return specs

    def merchant(self, spec: Spec) -> m.Merchant:
        surname = spec.owner.split()[-1]
        return m.Merchant(
            id=spec.id,
            owner_name=spec.owner,
            business_name=spec.business,
            business_type=spec.business_type,
            city=spec.city,
            state=spec.state,
            language=spec.language,
            phone=self.phone(),
            pan=self.pan(surname),
            kyc_expiry=self.today + timedelta(days=self.rng.randrange(120, 700)),
            bank_account=str(self.rng.randrange(10**10, 10**14)),
            ifsc=f"{self.rng.choice(IFSC_BANKS)}0{self.rng.randrange(10**5, 10**6)}",
            onboarded_at=self.now - timedelta(days=self.rng.randrange(200, 1500)),
        )

    def devices_and_billing(self, spec: Spec) -> tuple[list[m.Device], list[m.BillingPlan]]:
        devices, plans = [], []
        for dtype in spec.devices:
            deployed = self.now - timedelta(days=self.rng.randrange(90, 900))
            if dtype == m.DeviceType.SOUNDBOX:
                model = self.rng.choice(["Soundbox 4G", "Soundbox Display", "AI Soundbox"])
                plan_name, rent = spec.soundbox_plan or self.rng.choice(SOUNDBOX_PLANS)
                warranty_days = 365
            else:
                model, plan_name, rent, warranty_days = (
                    "Android EDC POS",
                    "EDC Monthly",
                    49_900,
                    730,
                )
            d = m.Device(
                id=self.hexid("DEV", 8),
                merchant_id=spec.id,
                type=dtype,
                model=model,
                serial=self.hexid("SN", 12),
                last_heartbeat_at=self.now,
                warranty_till=(deployed + timedelta(days=warranty_days)).date(),
            )
            devices.append(d)
            plans.append(
                m.BillingPlan(
                    id=self.hexid("PLN", 8),
                    merchant_id=spec.id,
                    device_id=d.id,
                    plan_name=plan_name,
                    monthly_rent_paise=rent,
                    started_at=deployed,
                )
            )
        return devices, plans

    def transactions(self, spec: Spec) -> list[m.Transaction]:
        (lo_n, hi_n), (lo_amt, hi_amt) = PROFILES[spec.business_type]
        txns = []
        for back in range(self.days, -1, -1):
            day = self.today - timedelta(days=back)
            for _ in range(self.rng.randint(lo_n, hi_n)):
                created = self.at(day, time(2, 30)) + timedelta(
                    minutes=self.rng.randrange(0, 14 * 60)
                )
                if created > self.now:
                    continue
                amount = self.rng.randint(lo_amt, hi_amt)
                txns.append(
                    m.Transaction(
                        id=self.hexid("TXN", 12),
                        merchant_id=spec.id,
                        amount_paise=amount * 100,
                        mode="CARD" if amount > 2000 and self.rng.random() < 0.3 else "UPI",
                        payer_vpa=self.vpa(),
                        status=m.TxnStatus.FAILED
                        if self.rng.random() < 0.02
                        else m.TxnStatus.SUCCESS,
                        created_at=created,
                    )
                )
        return txns

    def settlements_and_deductions(
        self, spec: Spec, txns: list[m.Transaction], plans: list[m.BillingPlan]
    ) -> tuple[list[m.Settlement], list[m.RentalDeduction]]:
        by_day: dict[date, list[m.Transaction]] = {}
        for t in txns:
            by_day.setdefault(t.created_at.date(), []).append(t)

        settlements, deductions = [], []
        for day in sorted(by_day):
            if day >= self.today:
                continue  # today's money settles tomorrow
            rent = 0
            if day.day == 1:
                for p in plans:
                    if p.monthly_rent_paise and p.rental_active:
                        rent += p.monthly_rent_paise
                        deductions.append(
                            m.RentalDeduction(
                                id=self.hexid("RNT", 8),
                                plan_id=p.id,
                                merchant_id=spec.id,
                                period=day.strftime("%Y-%m"),
                                amount_paise=p.monthly_rent_paise,
                                deducted_at=self.at(day + timedelta(days=1), SETTLEMENT_TIME_UTC),
                            )
                        )
            gross = sum(t.amount_paise for t in by_day[day] if t.status == m.TxnStatus.SUCCESS)
            expected = self.at(day + timedelta(days=1), SETTLEMENT_TIME_UTC)
            credited = expected <= self.now
            st = m.Settlement(
                id=self.hexid("STL", 10),
                merchant_id=spec.id,
                business_date=day,
                amount_paise=max(gross - rent, 0),
                rental_deducted_paise=rent,
                status=m.SettlementStatus.CREDITED if credited else m.SettlementStatus.PENDING,
                expected_at=expected,
                credited_at=expected - timedelta(minutes=self.rng.randrange(5, 90))
                if credited
                else None,
                utr=f"UTR{self.rng.randrange(10**11, 10**12)}" if credited else None,
                attempts=1,
            )
            settlements.append(st)
            for t in by_day[day]:
                t.settlement_id = st.id
        return settlements, deductions

    def tickets(self, spec: Spec) -> list[m.Ticket]:
        out = []
        for _ in range(self.rng.choice([0, 0, 1, 1, 2])):
            created = self.now - timedelta(days=self.rng.randrange(15, 85))
            category, subject = self.rng.choice(
                [
                    ("SETTLEMENT", "Settlement timing query"),
                    ("DEVICE", "Soundbox volume low"),
                    ("BILLING", "Rental charge query"),
                    ("OTHER", "Change shop name on QR"),
                ]
            )
            out.append(
                m.Ticket(
                    id=self.hexid("TKT", 8),
                    merchant_id=spec.id,
                    category=category,
                    subject=subject,
                    status=m.TicketStatus.CLOSED,
                    created_at=created,
                    updated_at=created + timedelta(days=1),
                    closed_at=created + timedelta(days=1),
                    resolution_note="Information shared with merchant",
                    verified_resolution=True,
                    history=[
                        {
                            "at": created.isoformat(),
                            "action": "CREATED",
                            "note": subject,
                            "actor": "merchant",
                        },
                        {
                            "at": (created + timedelta(days=1)).isoformat(),
                            "action": "CLOSED",
                            "note": "Information shared with merchant",
                            "actor": "agent",
                        },
                    ],
                )
            )
        return out

    # ----- scenarios -----

    def apply_scenario(
        self,
        spec: Spec,
        merchant: m.Merchant,
        devices: list[m.Device],
        plans: list[m.BillingPlan],
        settlements: list[m.Settlement],
        deductions: list[m.RentalDeduction],
        tickets: list[m.Ticket],
        txns: list[m.Transaction],
    ) -> None:
        recent = sorted(settlements, key=lambda x: x.business_date, reverse=True)

        def hold(st: m.Settlement, status: m.SettlementStatus, reason: m.HoldReason | None) -> None:
            st.status, st.hold_reason = status, reason
            st.utr = st.credited_at = None

        def closed_ticket(days_ago: int, category: str, subject: str, note: str) -> m.Ticket:
            created = self.now - timedelta(days=days_ago)
            return m.Ticket(
                id=self.hexid("TKT", 8),
                merchant_id=spec.id,
                category=category,
                subject=subject,
                status=m.TicketStatus.CLOSED,
                created_at=created,
                updated_at=created + timedelta(hours=20),
                closed_at=created + timedelta(hours=20),
                resolution_note=note,
                verified_resolution=False,
                history=[
                    {
                        "at": created.isoformat(),
                        "action": "CREATED",
                        "note": subject,
                        "actor": "merchant",
                    },
                    {
                        "at": (created + timedelta(hours=20)).isoformat(),
                        "action": "CLOSED",
                        "note": note,
                        "actor": "auto",
                    },
                ],
            )

        soundbox = next((d for d in devices if d.type == m.DeviceType.SOUNDBOX), None)
        match spec.id:
            case "M1001" | "M1008":
                merchant.kyc_status = m.KycStatus.PENDING_REVERIFICATION
                merchant.kyc_expiry = self.today - timedelta(days=3)
                for st in recent[:2]:
                    hold(st, m.SettlementStatus.ON_HOLD, m.HoldReason.KYC_REVERIFICATION)
                if spec.id == "M1008":
                    tickets.append(
                        closed_ticket(
                            6,
                            "SETTLEMENT",
                            "Settlement not received",
                            "Settlement is under process. Please wait 24-48 hours.",
                        )
                    )
                    tickets.append(
                        closed_ticket(
                            2,
                            "SETTLEMENT",
                            "Settlement not received again",
                            "Auto-closed: AI response shared",
                        )
                    )
            case "M1002":
                soundbox.fault = m.DeviceFault.POWER
                soundbox.last_heartbeat_at = self.now - timedelta(minutes=45)
            case "M1003":
                soundbox.status = m.DeviceStatus.RETURNED
                soundbox.returned_at = self.now - timedelta(days=40)
                # Rent kept being deducted after the return (on top of any 1st-of-month
                # deductions already generated inside the seed window).
                deductions.append(
                    m.RentalDeduction(
                        id=self.hexid("RNT", 8),
                        plan_id=plans[0].id,
                        merchant_id=spec.id,
                        period=(self.now - timedelta(days=35)).strftime("%Y-%m"),
                        amount_paise=plans[0].monthly_rent_paise,
                        deducted_at=self.now - timedelta(days=35),
                    )
                )
                tickets.append(
                    closed_ticket(
                        20,
                        "BILLING",
                        "Rent deducted after returning Soundbox",
                        "Closed: sound box is still active in your account",
                    )
                )
            case "M1004":
                merchant.bank_status = m.BankStatus.FAILED
                hold(recent[0], m.SettlementStatus.FAILED, m.HoldReason.BANK_ACCOUNT_FAILED)
            case "M1005":
                merchant.risk_flag = True
                hold(recent[0], m.SettlementStatus.ON_HOLD, m.HoldReason.RISK_REVIEW)
            case "M1006":
                # The day-before-yesterday batch never went out; it is past its T+1 time.
                hold(recent[1], m.SettlementStatus.PENDING, None)
            case "M1007":
                merchant.kyc_expiry = self.today + timedelta(days=5)
            case "M1009":
                txns.append(
                    m.Transaction(
                        id=self.hexid("TXN", 12),
                        merchant_id=spec.id,
                        amount_paise=4_850_000,
                        mode="UPI",
                        payer_vpa="9876501234@okhdfcbank",
                        status=m.TxnStatus.DEBITED_NOT_CREDITED,
                        created_at=self.now - timedelta(days=2, hours=3),
                    )
                )
            case "M1011":
                merchant.kyc_status = m.KycStatus.NAME_MISMATCH
                hold(recent[0], m.SettlementStatus.ON_HOLD, m.HoldReason.KYC_REVERIFICATION)
            case "M1012":
                soundbox.fault = m.DeviceFault.HARD
                soundbox.last_heartbeat_at = self.now - timedelta(hours=6)

    # ----- run -----

    def run(self) -> dict[str, int]:
        counts = {"merchants": 0, "transactions": 0, "settlements": 0, "devices": 0, "tickets": 0}
        scenario_ids = {s.id for s in SCENARIOS}
        for spec in SCENARIOS + self.generic_specs():
            self.use_rng(spec.id)
            merchant = self.merchant(spec)
            devices, plans = self.devices_and_billing(spec)
            txns = self.transactions(spec)
            settlements, deductions = self.settlements_and_deductions(spec, txns, plans)
            tickets = self.tickets(spec) if spec.id not in scenario_ids else []
            if spec.id in scenario_ids:
                self.apply_scenario(
                    spec, merchant, devices, plans, settlements, deductions, tickets, txns
                )

            # Parents first so foreign keys hold on Postgres.
            self.session.add(merchant)
            self.session.flush()
            self.session.add_all(devices)
            self.session.flush()
            self.session.add_all(plans)
            self.session.flush()
            self.session.add_all([*txns, *settlements, *deductions, *tickets])
            self.session.flush()

            counts["merchants"] += 1
            counts["transactions"] += len(txns)
            counts["settlements"] += len(settlements)
            counts["devices"] += len(devices)
            counts["tickets"] += len(tickets)
        self.session.commit()
        return counts


def seed(session: Session, settings: Settings | None = None) -> dict[str, int]:
    return Seeder(session, settings or get_settings()).run()


def main() -> None:
    engine = get_engine()
    init_db(engine, drop=True)
    with Session(engine) as session:
        counts = seed(session)
    print("Seeded:", ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
