#!/usr/bin/env python3
"""
EV Omnichannel Sales & CX Funnel Analytics - synthetic data generator
Outputs: crm_leads.csv, dms_sales_inventory.csv, cx_tickets_feedback.csv

Purposeful anomalies baked in (all deterministic via SEED):
  1. 8-12% of DMS bookings cannot be joined to CRM on raw crm_lead_id
     (nulls, formatting errors, wrong IDs, and walk-in bookings never entered in CRM)
  2. Test Ride Completed -> Booking is the weakest funnel stage, worst at Marathahalli
  3. Marathahalli has materially higher SLA breaches / worse CSAT / more cancellations
  4. CX tickets keep the *correct* crm_lead_id even when DMS lost it -> CX can "heal" DMS links

Usage:  python src/generate_data.py data      (run from the repo root)
Deps :  pip install pandas numpy faker   (faker is optional; falls back to name lists)
"""
import sys, os, hashlib, random
from datetime import date, timedelta
import numpy as np
import pandas as pd

SEED = 42
rng = np.random.default_rng(SEED)
random.seed(SEED)
try:
    from faker import Faker
    Faker.seed(SEED)
    fake = Faker("en_IN")
except ImportError:
    fake = None

OUT = sys.argv[1] if len(sys.argv) > 1 else "data"
os.makedirs(OUT, exist_ok=True)

TODAY = date(2026, 10, 25)            # "as-of" date for ageing / open tickets
START, END_LEADS = date(2026, 1, 1), date(2026, 9, 30)
N_LEADS = 500

STORES = {
    "Bangalore": (["Marathahalli", "Rajajinagar", "Yelahanka", "HSR Layout"], [.32, .22, .20, .26]),
    "Hyderabad": (["Gachibowli", "Kukatpally", "Banjara Hills"], [.4, .3, .3]),
    "Chennai":   (["OMR", "Anna Nagar", "Velachery"], [.4, .3, .3]),
}
HOT = "Marathahalli"                  # the deliberately problematic store
SOURCES = ["Meta Ads", "Google Search", "Website", "Direct Walk-in"]
SOURCE_W = [.30, .22, .23, .25]
P_RIDE = {"Direct Walk-in": .95, "Website": .80, "Google Search": .80, "Meta Ads": .68}
VARIANTS = ["Simple One 4.5kWh", "Simple One 3.7kWh", "Simple Dot One"]
BASE_PRICE = {"Simple One 4.5kWh": 172000, "Simple One 3.7kWh": 158000, "Simple Dot One": 145000}
CITY_ADJ = {"Bangalore": 0, "Hyderabad": 4000, "Chennai": 2500}
FIN = ["HDFC", "IDFC", "Greenko Finance", "Self-Funded"]
FIN_W = [.28, .22, .15, .35]
REASONS = ["Price/Financing", "Range Anxiety", "High Delivery TAT", "Competitor Purchase"]

FIRST = ["Aarav","Vihaan","Aditya","Rohan","Karthik","Suresh","Ananya","Priya","Divya","Meera",
         "Arjun","Sanjay","Lakshmi","Harish","Naveen","Pooja","Ravi","Sneha","Imran","Farhan"]
LAST = ["Reddy","Iyer","Nair","Sharma","Gowda","Naidu","Rao","Pillai","Khan","Menon","Patel","Kumar"]

def pick(opts, w=None):
    w = np.ones(len(opts)) if w is None else np.array(w, float)
    return str(opts[rng.choice(len(opts), p=w / w.sum())])

def person():
    return fake.name() if fake else f"{random.choice(FIRST)} {random.choice(LAST)}"

def phone_hash():
    n = "9" + "".join(str(rng.integers(0, 10)) for _ in range(9))
    return hashlib.sha256(("salt-ev-2026" + n).encode()).hexdigest()[:16]

# ----------------------------------------------------------------- 1. CRM LEADS
leads = []
span = (END_LEADS - START).days
for _ in range(N_LEADS):
    created = START + timedelta(days=int(span * rng.beta(2.0, 1.0)))   # volume skews recent
    city = pick(list(STORES), [.55, .25, .20])
    center = pick(*STORES[city])
    src = pick(SOURCES, SOURCE_W)
    hot = center == HOT
    age = (TODAY - created).days

    stage, trd, reason, will_book = None, None, "None", False
    rode = rng.random() < P_RIDE[src] - (.04 if hot else 0)
    if rode:
        lag = int(rng.integers(4, 15) if hot else rng.integers(1, 8))   # Marathahalli test-ride delay
        trd = created + timedelta(days=lag)
        p_book = .76 + {"Direct Walk-in": .06, "Meta Ads": -.08}.get(src, 0) + (.03 if center == "HSR Layout" else 0)
        if hot:
            p_book = .44                                                   # BOTTLENECK stage
        if rng.random() < p_book:
            stage, will_book = "Test Ride Completed", True
        elif rng.random() < .22:
            stage = "Test Ride Completed"                                   # undecided / open
        else:
            stage = "Drop-off"
            reason = pick(REASONS, [.30, .10, .45, .15] if hot else [.35, .25, .15, .25])
    else:
        if age < 60 and rng.random() < .85:
            stage = pick(["Enquiry", "Test Ride Scheduled"])
            if stage == "Test Ride Scheduled":
                trd = created + timedelta(days=int(rng.integers(1, 10)))   # may be overdue -> ageing
        else:
            stage = "Drop-off"
            reason = pick(REASONS, [.30, .30, .10, .30])
    leads.append(dict(created_date=created, customer_name=person(), phone_hash=phone_hash(), city=city,
                      experience_center=center, lead_source=src, lead_stage=stage,
                      test_ride_date=trd, drop_off_reason=reason, _will_book=will_book))

crm = pd.DataFrame(leads).sort_values("created_date").reset_index(drop=True)
crm.insert(0, "lead_id", [f"LD-{10001 + i}" for i in range(len(crm))])

# ----------------------------------------------------------------- 2. DMS BOOKINGS
def lifecycle(bdate, hot):
    """booking -> VIN -> invoice -> delivery, constrained by TODAY."""
    if rng.random() < (.15 if hot else .07):
        vin_ok = bool(rng.random() < .3 and bdate + timedelta(days=30) <= TODAY)
        return dict(vin_assigned=vin_ok, retail_invoice_date=None, delivery_date=None, dms_status="Cancelled")
    vin_lag = max(5, int(rng.normal(38, 12) if hot else rng.normal(24, 9)))
    vin_date = bdate + timedelta(days=vin_lag)
    if vin_date > TODAY:
        return dict(vin_assigned=False, retail_invoice_date=None, delivery_date=None, dms_status="Booked")
    inv = vin_date + timedelta(days=int(rng.integers(1, 8)))
    if inv > TODAY:
        return dict(vin_assigned=True, retail_invoice_date=None, delivery_date=None, dms_status="Booked")
    dlv = inv + timedelta(days=int(rng.integers(2, 13)))
    if dlv > TODAY:
        return dict(vin_assigned=True, retail_invoice_date=inv, delivery_date=None, dms_status="Invoiced")
    return dict(vin_assigned=True, retail_invoice_date=inv, delivery_date=dlv, dms_status="Delivered")

def price(variant, city):
    return int(round((BASE_PRICE[variant] + CITY_ADJ[city] + rng.normal(0, 3500)) / 100) * 100)

bk = []
for _, r in crm[crm["_will_book"]].iterrows():
    hot = r.experience_center == HOT
    bdate = min(r.test_ride_date + timedelta(days=int(rng.integers(0, 15 if hot else 10))), TODAY - timedelta(days=1))
    v = pick(VARIANTS, [.5, .3, .2])
    bk.append(dict(_true_lead_id=r.lead_id, crm_lead_id=r.lead_id, booking_date=bdate, customer_name=r.customer_name,
                   model_variant=v, **lifecycle(bdate, hot), financing_partner=pick(FIN, FIN_W),
                   final_onroad_price_inr=price(v, r.city)))
n_crm = len(bk)

# 2a. corrupt CRM linkage on ~6.5% of CRM-originated bookings
for i in rng.choice(n_crm, size=round(.065 * n_crm), replace=False):
    kind = pick(["null", "format", "wrong"], [.55, .20, .25])
    true = bk[i]["crm_lead_id"]
    bk[i]["crm_lead_id"] = None if kind == "null" else (true.lower() + " " if kind == "format"
                                                       else f"LD-{int(rng.integers(90000, 99999))}")
    if rng.random() < .3:                                           # sloppy name entry
        bk[i]["customer_name"] = bk[i]["customer_name"].upper()

# 2b. online / walk-in bookings never entered in CRM (~4% extra)
for _ in range(round(.04 * n_crm)):
    bdate = START + timedelta(days=int(rng.integers(30, 270)))
    v = pick(VARIANTS, [.5, .3, .2])
    bk.append(dict(_true_lead_id=None, crm_lead_id=None, booking_date=bdate, customer_name=person(),
                   model_variant=v, **lifecycle(bdate, False), financing_partner=pick(FIN, FIN_W),
                   final_onroad_price_inr=price(v, pick(list(CITY_ADJ)))))

dms = pd.DataFrame(bk).sort_values("booking_date").reset_index(drop=True)
dms.insert(0, "booking_id", [f"BK-{50001 + i}" for i in range(len(dms))])

# ----------------------------------------------------------------- 3. CX TICKETS
SLA_H = {"Test Ride Delay": 24, "Refund Delay": 72, "Charger Installation Lag": 120,
         "App Connectivity": 48, "Battery Range Dispute": 96}
CATS = list(SLA_H)
CAT_N = [44, 30, 44, 40, 42]                                        # = 200 tickets

store_of = crm.set_index("lead_id")["experience_center"].to_dict()
d_crm = dms[dms["_true_lead_id"].notna()].copy()
d_crm["w"] = d_crm["_true_lead_id"].map(lambda x: 2.2 if store_of[x] == HOT else 1.0)
lead_pool = crm[crm["test_ride_date"].notna()].copy()
lead_pool["w"] = np.where(lead_pool["experience_center"] == HOT, 2.5, 1.0)

def draw(pool, k):
    return pool.iloc[rng.choice(len(pool), size=k, replace=len(pool) < k, p=pool.w / pool.w.sum())]

def csat(breach):
    return int(rng.choice([1, 2, 3, 4, 5], p=[.18, .27, .30, .17, .08] if breach else [.03, .07, .20, .40, .30]))

tix = []
for cat, k in zip(CATS, CAT_N):
    if cat == "Test Ride Delay":
        pool = lead_pool
    elif cat == "Refund Delay":
        pool = d_crm[d_crm.dms_status == "Cancelled"]
    elif cat == "Charger Installation Lag":
        pool = d_crm[d_crm.dms_status.isin(["Invoiced", "Delivered"])]
    else:
        pool = d_crm[d_crm.dms_status == "Delivered"]
    for _, r in draw(pool, k).iterrows():
        if cat == "Test Ride Delay":
            lid, bid = r.lead_id, None
            base = min(r.created_date + timedelta(days=int(rng.integers(1, max(2, (r.test_ride_date - r.created_date).days + 1)))), TODAY)
        else:
            lid, bid = r._true_lead_id, r.booking_id
            anchor = {"Refund Delay": r.booking_date, "Charger Installation Lag": r.retail_invoice_date}.get(cat, r.delivery_date)
            anchor = anchor if pd.notna(anchor) else r.booking_date
            base = min(anchor + timedelta(days=int(rng.integers(1, 30))), TODAY)
        hot = store_of.get(lid) == HOT
        sla = SLA_H[cat]
        created = base
        avail = (TODAY - created).days * 24
        if rng.random() < (.70 if hot else .83) and avail >= 2:           # CLOSED
            tat = sla * rng.lognormal(np.log(.65 * (1.7 if hot else 1.0)), .5)
            tat, status = min(tat, avail), "Closed"
            breach = tat > sla
            score = csat(breach)
        else:                                                              # OPEN / ESCALATED (running TAT)
            created = max(base, TODAY - timedelta(days=int(rng.integers(1, 21))))
            tat = (TODAY - created).days * 24 + int(rng.integers(0, 24))
            breach = tat > sla
            status = "Escalated" if breach and rng.random() < .65 else "Open"
            score = csat(breach) if status == "Escalated" else None
        tix.append(dict(crm_lead_id=None if rng.random() < .04 else lid, booking_id=bid,
                        ticket_created_date=created, category=cat, csat_score=score,
                        resolution_tat_hours=round(float(tat), 1), ticket_status=status,
                        sla_breached=str(bool(breach)).upper()))

cx = pd.DataFrame(tix).sort_values("ticket_created_date").reset_index(drop=True)
cx.insert(0, "ticket_id", [f"TK-{80001 + i}" for i in range(len(cx))])
cx["csat_score"] = cx["csat_score"].astype("Int64")

# ----------------------------------------------------------------- WRITE + QA
crm.drop(columns="_will_book").to_csv(f"{OUT}/crm_leads.csv", index=False)
dms.drop(columns="_true_lead_id").to_csv(f"{OUT}/dms_sales_inventory.csv", index=False)
cx.to_csv(f"{OUT}/cx_tickets_feedback.csv", index=False)

raw_unlinked = ~dms.crm_lead_id.isin(crm.lead_id)
disc = raw_unlinked.mean() * 100
rides = crm[crm.test_ride_date.notna() & crm.lead_stage.isin(["Test Ride Completed", "Drop-off"])]
rides_by_store = rides.groupby("experience_center").size()
book_by_store = crm[crm._will_book].groupby("experience_center").size()
cx["store"] = cx.crm_lead_id.map(store_of)
print(f"CRM rows {len(crm)} | DMS rows {len(dms)} | CX rows {len(cx)}")
print(f"DMS->CRM raw-join discrepancy: {disc:.1f}%  ({raw_unlinked.sum()} of {len(dms)})")
print("\nTest Ride -> Booking conversion by store (true links):")
print((book_by_store / rides_by_store).round(2).sort_values().to_string())
print("\nSLA breach rate by store (tickets with a CRM link):")
print(cx.dropna(subset=["store"]).groupby("store").sla_breached.apply(lambda s: round((s == "TRUE").mean(), 2)).sort_values(ascending=False).to_string())
print("\nStage mix:\n", crm.lead_stage.value_counts().to_string())
print("\nDMS status mix:\n", dms.dms_status.value_counts().to_string())
print("\nTicket status mix:\n", cx.ticket_status.value_counts().to_string())
assert 8 <= disc <= 12, "discrepancy outside 8-12% target - tweak SEED/corruption rates"
