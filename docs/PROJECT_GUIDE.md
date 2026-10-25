# EV Omnichannel Sales & CX Funnel Analytics (CRM + DMS Reconciliation)

**Stack:** Python (data) · SQL (reconciliation) · Google Sheets (ops layer) · Power BI (exec layer) · Apps Script (automation)
**As-of date for all ageing maths:** `2026-10-25` (hard-code it so the portfolio doesn't "rot").

```
ev-omnichannel-analytics/
├── README.md
├── requirements.txt
├── data/            crm_leads.csv (500) · dms_sales_inventory.csv (286) · cx_tickets_feedback.csv (200)
├── src/             generate_data.py        # reproducible generator (SEED=42)
├── sql/             reconciliation.sql      # staging, FULL OUTER JOIN ledger, CX healing, KPI queries
├── apps_script/     escalation.gs           # CRITICAL_ESCALATION automation
├── powerbi/         your .pbix file goes here
├── images/          dashboard screenshots
└── docs/            PROJECT_GUIDE.md        # this file
```
Run (from repo root): `pip install -r requirements.txt && python src/generate_data.py data`

---

## 1. What the data deliberately contains (your "findings")

| Planted anomaly | How it shows up (seed 42) |
|---|---|
| CRM↔DMS key breakage | **10.1%** of DMS bookings (29/286) fail a raw join: 22 NULL keys, 7 keys not in CRM. 4 of the bad keys are formatting errors (`ld-10003 `) that `UPPER(TRIM())` fixes. 7 more are recoverable from CX (tickets carry the right `crm_lead_id` + `booking_id`). **18 remain unattributable.** |
| Orphaned CRM leads | 33 leads sit at *Test Ride Completed* with no raw-linked booking — a mix of true "undecided" and bookings whose DMS key was corrupted. |
| Test Ride → Booking bottleneck | Marathahalli converts **~45%** vs 65–89% elsewhere. Its drop-offs skew to *High Delivery TAT* and *Price/Financing*. |
| Marathahalli CX problem | Longer test-ride lag (4–14d vs 1–7d), higher cancellations (~15% vs ~4%), slower VIN assignment, SLA breach rate ~70% vs 0–50% elsewhere (small per-store samples). |
| Open pipeline | 11 Enquiry + 11 Test Ride Scheduled leads, some with overdue ride dates → lead-ageing visuals have something to show. |

> **Modelling caveat to state in your README:** `lead_stage` is a *current-state* field, so "booked" is derived from DMS, not CRM. A lead that booked still reads `Test Ride Completed`. Stage-reach logic is defined in §4.

---

## 2. Reconciliation in SQL
See `reconciliation.sql`. Order of work: **stg_dms** (normalise keys) → **#2 unlinked bookings** → **#3 orphan leads** → **#4 FULL OUTER JOIN ledger** → **#5 `dim_booking_link`** (DMS key → CX-healed → unattributed) → **#6 `fact_funnel`** (one row per lead; the table Power BI loads).

Join logic in one line each:
- `LEFT JOIN crm ON dms.key = crm.id WHERE crm.id IS NULL` → unlinked bookings
- `LEFT JOIN dms ON crm.id = dms.key WHERE dms.id IS NULL` → orphan leads
- `FULL OUTER JOIN` → both sets in one reconciliation ledger with a `recon_status` column

---

## 3. Reconciliation in Google Sheets

**Tabs:** `CRM` (A lead_id, B created_date, C name, D phone_hash, E city, F experience_center, G lead_source, H lead_stage, I test_ride_date, J drop_off_reason) · `DMS` (A booking_id, B crm_lead_id, C booking_date, D name, E variant, F vin, G invoice_date, H delivery_date, I status, J financing, K price) · `CX` (A ticket_id, B crm_lead_id, C booking_id, D created, E category, F csat, G tat_h, H status, I sla_breached).
Create a named range **`AsOf`** = `=DATE(2026,10,25)`.

**Pull data from a source workbook (IMPORTRANGE)**
```
=QUERY(IMPORTRANGE("https://docs.google.com/spreadsheets/d/<ID>","crm_leads!A:J"),"select * where Col1 is not null",1)
```
Click **Allow access** once. Better practice: IMPORTRANGE into hidden `raw_*` tabs, then build everything off those.

**3a. DMS helper columns** (put headers in L1:N1; each is a single spilled formula)
```
L1 crm_key       L2: =ARRAYFORMULA(IF(A2:A="","",UPPER(TRIM(B2:B))))
M1 resolved_lead M2: =ARRAYFORMULA(IF(A2:A="","",
                      IFERROR(VLOOKUP(L2:L,CRM!A:A,1,FALSE),
                      IFERROR(VLOOKUP(A2:A,{CX!C:C,CX!B:B},2,FALSE),"UNATTRIBUTED"))))
N1 link_method   N2: =ARRAYFORMULA(IF(A2:A="","",
                      IF(IFERROR(MATCH(L2:L,CRM!A:A,0),0)>0,"DMS_KEY",
                      IF(IFERROR(MATCH(A2:A,CX!C:C,0),0)>0,"CX_HEALED","UNATTRIBUTED"))))
```
XLOOKUP equivalent for a single cell: `=XLOOKUP(L2, CRM!A:A, CRM!A:A, XLOOKUP(A2, CX!C:C, CX!B:B, "UNATTRIBUTED"))`. Inside `ARRAYFORMULA`, use the VLOOKUP/MATCH form above — it is the reliably array-safe one.

**3b. Discrepancy KPI** (target 8–12%)
```
=SUMPRODUCT((DMS!A2:A<>"")*ISNA(MATCH(DMS!B2:B,CRM!A2:A,0)))/COUNTA(DMS!A2:A)
```

**3c. Unlinked bookings (raw)**
```
=FILTER(DMS!A2:K, DMS!A2:A<>"", ARRAYFORMULA(ISNA(MATCH(DMS!B2:B,CRM!A2:A,0))))
```
Add a failure-reason column: `=IF(B2="","NULL_KEY",IF(ISNUMBER(MATCH(UPPER(TRIM(B2)),CRM!A:A,0)),"FORMAT_ERROR","WRONG_KEY"))`.

**3d. Orphaned CRM leads**
```
=FILTER(CRM!A2:J, CRM!H2:H="Test Ride Completed", ARRAYFORMULA(ISNA(MATCH(CRM!A2:A,DMS!M2:M,0))))
```

**3e. CRM helper columns** (K1:N1)
```
K booked      =ARRAYFORMULA(IF(A2:A="","",--(COUNTIF(DMS!M:M,A2:A)>0)))
L ride_done   =ARRAYFORMULA(IF(A2:A="","",--((I2:I<>"")*(I2:I<=AsOf)*((H2:H="Test Ride Completed")+(H2:H="Drop-off"))>0)))
M open_lead   =ARRAYFORMULA(IF(A2:A="","",--(((H2:H="Enquiry")+(H2:H="Test Ride Scheduled")+((H2:H="Test Ride Completed")*(K2:K=0)))>0)))
N lead_age_d  =ARRAYFORMULA(IF(A2:A="","",IF(M2:M=1,AsOf-B2:B,"")))
```

**3f. Store funnel table (QUERY)**
```
=QUERY(CRM!A1:N,"select F, count(A), sum(L), sum(K) where A is not null group by F
        label count(A) 'Enquiries', sum(L) 'Test Rides', sum(K) 'Bookings'",1)
```
Next to it: `Ride→Booking % = Bookings / Test Rides` per row.

**3g. CX helper + scorecards**
```
CX!J1 store   J2: =ARRAYFORMULA(IF(A2:A="","",IFERROR(VLOOKUP(B2:B,{CRM!A:A,CRM!F:F},2,FALSE),"Unknown")))

SLA compliance %   =1-COUNTIF(CX!I2:I,TRUE)/COUNTA(CX!A2:A)      // Sheets turns "TRUE" into a boolean
Avg CSAT           =AVERAGE(CX!F2:F)
CSAT % (4-5)       =COUNTIF(CX!F2:F,">=4")/COUNT(CX!F2:F)
NPS proxy          =(COUNTIF(CX!F2:F,5)-COUNTIF(CX!F2:F,"<=3"))/COUNT(CX!F2:F)
Median TAT (h)     =MEDIAN(CX!G2:G)
By store           =QUERY(CX!A1:J,"select J, count(A), avg(F), avg(G) where A is not null group by J order by avg(G) desc",1)
```

---

## 4. Metric definitions and formulas

**Stage-reach rules** (because `lead_stage` is current-state):

| Stage | Definition |
|---|---|
| **Enquiry** | Every unique CRM lead (de-dupe on `phone_hash` if you extend the data) |
| **Test Ride (done)** | `test_ride_date` not null **and** ≤ AsOf **and** `lead_stage ∈ {Test Ride Completed, Drop-off}` (scheduled-but-not-done leads are excluded) |
| **Booking** | CRM lead with a resolved DMS booking (DMS key *or* CX-healed). Report **Net Booking** = excluding `Cancelled` |
| **Retail** | `retail_invoice_date` not null |
| **Delivery** | `dms_status = 'Delivered'` (`delivery_date` not null) |

**Funnel conversion (cohort or period; state which)**
```
Enquiry → Test Ride   = Test Rides            / Enquiries
Test Ride → Booking   = Bookings (that rode)  / Test Rides          ← bottleneck KPI
Booking → Retail      = Retailed              / Net Bookings
Retail → Delivery     = Delivered             / Retailed
End-to-end            = Delivered / Enquiries
Funnel leakage (n)    = Stage(n-1) − Stage(n)
Attribution rate      = Bookings with link_method ≠ UNATTRIBUTED / All DMS bookings
```
Reference values (seed 42): 500 → 385 (77%) → ~268 attributed bookings (~70% of rides; 286 total DMS) → 251 retailed → 232 delivered.

**Lead ageing & TAT**
```
Open-lead age (days)      = AsOf − created_date        (open = Enquiry | Test Ride Scheduled | Test Ride Completed w/o booking)
Age buckets               = 0-7 | 8-14 | 15-30 | 31-60 | 60+
Test-ride lag (days)      = test_ride_date − created_date
Overdue ride (flag)       = stage = 'Test Ride Scheduled' AND test_ride_date < AsOf
Booking → Delivery TAT    = delivery_date − booking_date           (delivered only)
Booking → VIN/Invoice     = retail_invoice_date − booking_date
Resolution TAT (hrs)      = resolution_tat_hours   (Closed = actual; Open/Escalated = running clock)
                            report Median and P90, never just the mean (right-skewed)
```

**SLA targets used by the generator**: Test Ride Delay 24h · App Connectivity 48h · Refund Delay 72h · Battery Range Dispute 96h · Charger Installation Lag 120h.
```
SLA Compliance %  = 1 − (tickets with sla_breached = TRUE / total tickets)
Critical ticket   = resolution_tat_hours > 24 AND csat_score ≤ 2   (CSAT must be non-blank)
```

**CSAT / NPS scorecard**
```
Avg CSAT          = AVERAGE(csat_score)                         (responses only; blanks excluded)
CSAT %            = COUNT(csat ≥ 4) / COUNT(csat)
Response rate     = COUNT(csat) / Tickets
NPS proxy         = %Promoters (5) − %Detractors (1-3)          Passives = 4
```
> There is no 0–10 "would you recommend" question, so label it **"NPS proxy"**. Say so on the dashboard footer.

---

## 5. Power BI build

### 5.1 Model (star schema)
| Table | Grain | Source |
|---|---|---|
| `Fact_Funnel` | 1 row / CRM lead | `fact_funnel` view (or rebuild in Power Query) |
| `Fact_DMS` | 1 row / booking | `dms_sales_inventory` + `link_method`, `resolved_lead_id` |
| `Fact_Tickets` | 1 row / ticket | `cx_tickets_feedback` + `store` from Funnel |
| `Dim_Store` | center → city | distinct from CRM |
| `Dim_Date` | day | `CALENDARAUTO()`; mark as date table |

Relationships (all 1→*, single direction): `Dim_Store[center]→Fact_Funnel`, `Dim_Date[Date]→Fact_Funnel[created_date]`, `Fact_Funnel[lead_id]→Fact_Tickets[crm_lead_id]`, `Fact_Funnel[lead_id]→Fact_DMS[resolved_lead_id]` (UNATTRIBUTED rows simply don't join, so surface them with a card). Keep `Fact_DMS[booking_id]↔Fact_Tickets[booking_id]` **inactive** (use `USERELATIONSHIP`).

### 5.2 Power Query: key healing (Fact_DMS)
```m
let
  src     = Csv.Document(File.Contents("...\dms_sales_inventory.csv"),[Delimiter=",",Encoding=65001,PromoteHeaders=true]),
  keyed   = Table.AddColumn(src,"crm_key", each if [crm_lead_id]=null or Text.Trim([crm_lead_id])="" then null else Text.Upper(Text.Trim([crm_lead_id])), type text),
  cxmap   = Table.Distinct(Table.SelectRows(CX, each [booking_id]<>null and [crm_lead_id]<>null), {"booking_id"}),
  j1      = Table.NestedJoin(keyed,{"crm_key"},CRM,{"lead_id"},"crm",JoinKind.LeftOuter),
  j2      = Table.NestedJoin(j1,{"booking_id"},cxmap,{"booking_id"},"cx",JoinKind.LeftOuter),
  resolved= Table.AddColumn(j2,"resolved_lead_id", each
              if Table.RowCount([crm])>0 then [crm_key]
              else if Table.RowCount([cx])>0 then [cx]{0}[crm_lead_id] else null, type text),
  method  = Table.AddColumn(resolved,"link_method", each
              if Table.RowCount([crm])>0 then "DMS_KEY"
              else if Table.RowCount([cx])>0 then "CX_HEALED" else "UNATTRIBUTED", type text)
in method
```
Set `sla_breached` to True/False, `vin_assigned` to True/False, all `*_date` to Date.

### 5.3 DAX measures
```dax
AsOf = DATE(2026,10,25)

-- Funnel (flag columns f_* are 0/1 in Fact_Funnel)
Enquiries     = COUNTROWS(Fact_Funnel)
Test Rides    = SUM(Fact_Funnel[f_test_ride_done])
Bookings      = SUM(Fact_Funnel[f_booked])
Net Bookings  = SUM(Fact_Funnel[f_net_booked])
Retail        = SUM(Fact_Funnel[f_retailed])
Delivered     = SUM(Fact_Funnel[f_delivered])
Unattributed Bookings = CALCULATE(COUNTROWS(Fact_DMS), Fact_DMS[link_method]="UNATTRIBUTED")

Enq→Ride %      = DIVIDE([Test Rides],[Enquiries])
Ride→Booking %  = DIVIDE(CALCULATE(SUM(Fact_Funnel[f_booked]), Fact_Funnel[f_test_ride_done]=1),[Test Rides])
Booking→Retail %= DIVIDE([Retail],[Net Bookings])
Retail→Deliv %  = DIVIDE([Delivered],[Retail])
End-to-End %    = DIVIDE([Delivered],[Enquiries])
Attribution %   = DIVIDE(COUNTROWS(FILTER(Fact_DMS,Fact_DMS[link_method]<>"UNATTRIBUTED")),COUNTROWS(Fact_DMS))
Ride→Booking Gap vs Network =
    [Ride→Booking %] - CALCULATE([Ride→Booking %], ALL(Dim_Store))

-- Ageing & TAT
Open Lead Age (avg d) = AVERAGEX(FILTER(Fact_Funnel,Fact_Funnel[is_open]=1), [AsOf]-Fact_Funnel[created_date])
Overdue Rides   = CALCULATE(COUNTROWS(Fact_Funnel), Fact_Funnel[lead_stage]="Test Ride Scheduled", Fact_Funnel[test_ride_date]<DATE(2026,10,25))
Booking→Delivery (avg d) = AVERAGEX(FILTER(Fact_DMS,NOT ISBLANK(Fact_DMS[delivery_date])), DATEDIFF(Fact_DMS[booking_date],Fact_DMS[delivery_date],DAY))

-- CX
Tickets       = COUNTROWS(Fact_Tickets)
SLA Breaches  = CALCULATE(COUNTROWS(Fact_Tickets), Fact_Tickets[sla_breached]=TRUE())
SLA Compliance % = 1 - DIVIDE([SLA Breaches],[Tickets])
Median TAT (h)= MEDIAN(Fact_Tickets[resolution_tat_hours])
P90 TAT (h)   = PERCENTILE.INC(Fact_Tickets[resolution_tat_hours],0.9)
Avg CSAT      = AVERAGE(Fact_Tickets[csat_score])
CSAT % (4-5)  = DIVIDE(CALCULATE(COUNTROWS(Fact_Tickets),Fact_Tickets[csat_score]>=4),COUNT(Fact_Tickets[csat_score]))
NPS Proxy     = DIVIDE(CALCULATE(COUNTROWS(Fact_Tickets),Fact_Tickets[csat_score]=5)
                     - CALCULATE(COUNTROWS(Fact_Tickets),Fact_Tickets[csat_score]<=3),
                       COUNT(Fact_Tickets[csat_score]))*100
Critical Tickets = COUNTROWS(FILTER(Fact_Tickets, Fact_Tickets[resolution_tat_hours]>24
                     && NOT ISBLANK(Fact_Tickets[csat_score]) && Fact_Tickets[csat_score]<=2))
Active Escalations = CALCULATE(COUNTROWS(Fact_Tickets), Fact_Tickets[ticket_status]="Escalated")
```
> DAX gotcha: `BLANK() <= 2` is TRUE, which is why `Critical Tickets` guards `ISBLANK`.

---

## 6. Dashboard blueprint (3 pages)

**Global:** 16:9 canvas; slicers synced across pages: *Date (created_date)*, *City*, *Lead Source*, *Model Variant*. Page navigator buttons + "Reset filters" bookmark. Palette: neutral greys, one brand colour, **red reserved for breaches/bottlenecks**. Footer: "As of 25-Oct-2026 · NPS = proxy from CSAT".

### Page 1 — Executive Omnichannel Funnel
| Zone | Visual | Fields |
|---|---|---|
| KPI strip (6 cards) | Cards with MoM delta | Enquiries · Test Rides · Net Bookings · Retail · Delivered · End-to-End % |
| Hero | **Funnel chart** (5 stages) with conversion % labels between stages | Enquiries → Test Rides → Bookings → Retail → Delivered |
| Right | **Stage-conversion bar** (4 bars, sorted, bottleneck in red) | Enq→Ride %, Ride→Booking %, Booking→Retail %, Retail→Deliv % |
| Mid-left | Line + clustered column | Month: Enquiries (col), Bookings (col), Ride→Booking % (line, secondary axis) |
| Mid-right | **Decomposition tree** | Drop-offs → Drop-off reason → City → Store |
| Bottom-left | Stacked bar | Funnel by Lead Source (find Meta Ads leakage) |
| Bottom-right | **Data-quality card cluster** | Attribution % · Unattributed Bookings · Orphan leads (drill-through to ledger table) |

### Page 2 — Store & City Scorecard
| Zone | Visual | Fields |
|---|---|---|
| Top | Matrix (rows = City → Store) with **conditional-format data bars/heatmap** | Enquiries, Test Rides, Ride→Booking %, Gap vs Network, Booking→Delivery (d), Cancel %, Open Lead Age |
| Left | **Scatter** (x = Ride→Booking %, y = SLA Compliance %, size = Enquiries) | Quadrant lines at network averages: Marathahalli lands bottom-left |
| Right | 100% stacked bar | Drop-off reason by Store |
| Bottom-left | Clustered bar | Model Variant mix × Financing Partner by Store |
| Bottom-right | Column | Lead-age buckets (0-7…60+) of **open** leads, with overdue test rides highlighted |
| Interaction | Drill-through on Store → Page 3 filtered | Tooltip page: mini funnel for the hovered store |

### Page 3 — CX Escalation & SLA Tracker
| Zone | Visual | Fields |
|---|---|---|
| KPI strip | Cards: SLA Compliance % (red < 85%), Median TAT, P90 TAT, Avg CSAT, NPS Proxy, **Critical Tickets**, Active Escalations |
| Left | Bar: SLA Compliance % by Store (target line at 90%) | |
| Centre | **Heatmap matrix** Category × Store, value = breach rate | Pinpoints e.g. Marathahalli × Test Ride Delay |
| Right | Line: weekly SLA Compliance % and Avg CSAT | |
| Mid-left | Box-style (column + error bars) or histogram of TAT vs SLA threshold by Category | |
| Bottom | **Action table** (top-N by TAT where Critical): ticket_id, store, category, TAT, CSAT, status, `escalation_flag` | Conditional formatting; export-friendly |
| Extra | Key-influencers visual: "What drives CSAT ≤ 2?" (inputs: category, store, breached, TAT bucket) | |

---

## 7. Apps Script automation
`escalation.gs` — scans `cx_tickets_feedback`, flags rows where **`resolution_tat_hours > 24` and `csat_score ≤ 2`** (blank CSAT is skipped).
- Writes `CRITICAL_ESCALATION` to a new `escalation_flag` column (set `OVERWRITE_STATUS = true` to overwrite `ticket_status` instead).
- Idempotent: already-flagged rows aren't re-logged; new hits are appended to an `Escalation_Log` tab.
- Optional email alert (`ALERT_EMAIL`), menu **CX Automation**, and a one-click **hourly trigger**.
- With the shipped data the rule fires on **37 tickets**.

## 8. Portfolio talking points
1. *"I found 10% of bookings couldn't be tied to a lead; I quantified it, fixed ~40% of it by normalising keys and healing from CX, and left 18 flagged for ops."*
2. *"Marathahalli converts test rides at ~45% vs a ~70% network rate; the drop-off reasons point to delivery TAT and financing, not product."*
3. *"The same store has the worst SLA compliance, so the funnel bottleneck and the CX problem are one operational issue."*
