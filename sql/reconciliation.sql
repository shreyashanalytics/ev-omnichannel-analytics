-- =====================================================================
-- EV Omnichannel: CRM + DMS + CX reconciliation  (PostgreSQL syntax)
-- BigQuery: swap NOW()->CURRENT_DATE(), ::date -> CAST(.. AS DATE), FILTER(WHERE) works as-is
-- MySQL has no FULL OUTER JOIN -> emulate with LEFT JOIN UNION ALL RIGHT JOIN (anti-join)
-- As-of date used for ageing: 2026-10-25
-- =====================================================================

-- 0. STAGING: normalise keys so "ld-10003 " matches "LD-10003" -----------------
CREATE OR REPLACE VIEW stg_dms AS
SELECT d.*,
       NULLIF(UPPER(TRIM(d.crm_lead_id)), '')   AS crm_lead_key,
       UPPER(REGEXP_REPLACE(TRIM(d.customer_name), '\s+', ' ', 'g')) AS name_key
FROM dms_sales_inventory d;

-- 1. HEADLINE: raw vs normalised discrepancy (target band 8-12%) ---------------
SELECT COUNT(*)                                                         AS dms_bookings,
       COUNT(*) FILTER (WHERE c_raw.lead_id IS NULL)                    AS unlinked_raw,
       ROUND(100.0 * COUNT(*) FILTER (WHERE c_raw.lead_id  IS NULL) / COUNT(*), 1) AS pct_unlinked_raw,
       ROUND(100.0 * COUNT(*) FILTER (WHERE c_norm.lead_id IS NULL) / COUNT(*), 1) AS pct_unlinked_after_normalise
FROM dms_sales_inventory d
LEFT JOIN crm_leads c_raw  ON c_raw.lead_id  = d.crm_lead_id
LEFT JOIN crm_leads c_norm ON c_norm.lead_id = UPPER(TRIM(d.crm_lead_id));

-- 2. UNLINKED BOOKINGS (DMS rows with no CRM parent) with a failure reason ------
SELECT d.booking_id, d.crm_lead_id, d.customer_name, d.booking_date, d.dms_status,
       CASE WHEN d.crm_lead_id IS NULL                              THEN 'NULL_KEY'
            WHEN UPPER(TRIM(d.crm_lead_id)) IN (SELECT lead_id FROM crm_leads) THEN 'FORMAT_ERROR (auto-fixable)'
            ELSE 'WRONG_KEY' END AS failure_reason
FROM dms_sales_inventory d
LEFT JOIN crm_leads c ON c.lead_id = d.crm_lead_id
WHERE c.lead_id IS NULL
ORDER BY d.booking_date;

-- 3. ORPHANED CRM LEADS: finished the test ride but no booking attached --------
--    Some are genuine "undecided"; others are bookings whose DMS key was corrupted.
SELECT c.lead_id, c.customer_name, c.experience_center, c.test_ride_date,
       (DATE '2026-10-25' - c.test_ride_date) AS days_since_ride
FROM crm_leads c
LEFT JOIN stg_dms d ON d.crm_lead_key = c.lead_id
WHERE c.lead_stage = 'Test Ride Completed'
  AND d.booking_id IS NULL
ORDER BY days_since_ride DESC;

-- 4. FULL OUTER JOIN reconciliation ledger (single source of truth) -------------
SELECT COALESCE(c.lead_id, d.crm_lead_key)  AS join_key,
       c.lead_id, d.booking_id,
       CASE WHEN c.lead_id IS NOT NULL AND d.booking_id IS NOT NULL THEN 'MATCHED'
            WHEN c.lead_id IS NOT NULL AND d.booking_id IS NULL
                 AND c.lead_stage = 'Test Ride Completed'            THEN 'ORPHAN_LEAD_POST_RIDE'
            WHEN c.lead_id IS NOT NULL AND d.booking_id IS NULL      THEN 'LEAD_NOT_YET_BOOKED'
            ELSE 'UNLINKED_BOOKING' END     AS recon_status
FROM crm_leads c
FULL OUTER JOIN stg_dms d ON d.crm_lead_key = c.lead_id;

-- 5. HEAL THE LINK USING CX (tickets keep the correct crm_lead_id + booking_id) -
CREATE OR REPLACE VIEW dim_booking_link AS
WITH cx_map AS (
    SELECT booking_id, MAX(crm_lead_id) AS cx_lead_id
    FROM cx_tickets_feedback
    WHERE booking_id IS NOT NULL AND crm_lead_id IS NOT NULL
    GROUP BY booking_id
)
SELECT d.booking_id,
       COALESCE(c1.lead_id, cm.cx_lead_id)                       AS resolved_lead_id,
       CASE WHEN c1.lead_id IS NOT NULL    THEN 'DMS_KEY'
            WHEN cm.cx_lead_id IS NOT NULL THEN 'CX_HEALED'
            ELSE 'UNATTRIBUTED' END                               AS link_method
FROM stg_dms d
LEFT JOIN crm_leads c1 ON c1.lead_id = d.crm_lead_key
LEFT JOIN cx_map   cm ON cm.booking_id = d.booking_id;

-- 5b. Last-resort fuzzy candidates: same normalised name, booking 0-21d after test ride
SELECT d.booking_id, c.lead_id AS candidate_lead_id
FROM stg_dms d
JOIN dim_booking_link l ON l.booking_id = d.booking_id AND l.link_method = 'UNATTRIBUTED'
JOIN crm_leads c ON UPPER(TRIM(c.customer_name)) = d.name_key
                AND d.booking_date BETWEEN c.test_ride_date AND c.test_ride_date + 21;

-- 6. MASTER FACT: one row per CRM lead, with booking and CX rolled up -----------
CREATE OR REPLACE VIEW fact_funnel AS
SELECT c.lead_id, c.created_date, c.city, c.experience_center, c.lead_source, c.lead_stage,
       c.test_ride_date, c.drop_off_reason,
       d.booking_id, d.booking_date, d.dms_status, d.retail_invoice_date, d.delivery_date,
       d.model_variant, d.financing_partner, d.final_onroad_price_inr, l.link_method,
       (c.test_ride_date IS NOT NULL AND c.test_ride_date <= DATE '2026-10-25'
        AND c.lead_stage IN ('Test Ride Completed','Drop-off'))     AS f_test_ride_done,
       (d.booking_id IS NOT NULL)                                    AS f_booked,
       (d.booking_id IS NOT NULL AND d.dms_status <> 'Cancelled')    AS f_net_booked,
       (d.retail_invoice_date IS NOT NULL)                           AS f_retailed,
       (d.dms_status = 'Delivered')                                  AS f_delivered
FROM crm_leads c
LEFT JOIN dim_booking_link l ON l.resolved_lead_id = c.lead_id
LEFT JOIN dms_sales_inventory d ON d.booking_id = l.booking_id;

-- 7. FUNNEL BY STORE with stage-to-stage conversion -----------------------------
SELECT experience_center,
       COUNT(*)                                   AS enquiries,
       SUM(f_test_ride_done::int)                 AS test_rides,
       SUM(f_booked::int)                         AS bookings,
       SUM(f_retailed::int)                       AS retail,
       SUM(f_delivered::int)                      AS delivered,
       ROUND(100.0*SUM(f_test_ride_done::int)/COUNT(*), 1)                   AS enq_to_ride_pct,
       ROUND(100.0*SUM((f_booked AND f_test_ride_done)::int)
                   /NULLIF(SUM(f_test_ride_done::int),0), 1)                 AS ride_to_booking_pct,
       ROUND(100.0*SUM(f_retailed::int)/NULLIF(SUM(f_net_booked::int),0), 1) AS booking_to_retail_pct,
       ROUND(100.0*SUM(f_delivered::int)/NULLIF(SUM(f_retailed::int),0), 1)  AS retail_to_delivery_pct
FROM fact_funnel
GROUP BY experience_center
ORDER BY ride_to_booking_pct;

-- 8. CX + STORE: SLA compliance, TAT, CSAT, NPS-proxy ---------------------------
SELECT c.experience_center,
       COUNT(*)                                                         AS tickets,
       ROUND(100.0*(1 - AVG((t.sla_breached='TRUE')::int)), 1)          AS sla_compliance_pct,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resolution_tat_hours)::numeric,1) AS median_tat_h,
       ROUND(AVG(t.csat_score),2)                                       AS avg_csat,
       ROUND(100.0*COUNT(*) FILTER (WHERE t.csat_score>=4)/NULLIF(COUNT(t.csat_score),0),1) AS csat_pct_4_5,
       ROUND(100.0*(COUNT(*) FILTER (WHERE t.csat_score=5) - COUNT(*) FILTER (WHERE t.csat_score<=3))
                   /NULLIF(COUNT(t.csat_score),0),1)                    AS nps_proxy
FROM cx_tickets_feedback t
LEFT JOIN crm_leads c ON c.lead_id = t.crm_lead_id
GROUP BY c.experience_center
ORDER BY sla_compliance_pct;

-- 9. CX tickets with no CRM parent (data-quality watchlist) ---------------------
SELECT t.* FROM cx_tickets_feedback t
LEFT JOIN crm_leads c ON c.lead_id = t.crm_lead_id
WHERE c.lead_id IS NULL;
