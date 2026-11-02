# Business Learning Layer — OFF/ON A/B Measurement Report

**Date:** 2026-08-30
**Scope:** Empirical, end-to-end measurement of whether the connection-time "Business Learning Layer" (`core/orchestrator/business_learning.py::run_connect_time_business_learning`) improves Q&A outcomes, using 4 independent synthetic sqlite fixture domains, real OpenAI calls, and ground truth computed independently in Python.
**Evidence artifact:** `docs/validation/business_learning_ab_results.json` (raw per-question results, 136 question runs total: 4 domains × 2 conditions × 17 questions).
**Harness:** `docs/validation/run_business_learning_ab.py`.
**Constraints honored:** no `git commit`/`push`; source_id=1/CCPP never touched; 100% synthetic sqlite fixtures; real `OPENAI_API_KEY` used exactly as the pre-existing fixture tests already do; no production `core/`/`data/` code modified.

---

## 1. What was extended in each fixture

**Fixture A (Commerce)** — `tests/test_fixture_a_accounts_zero_metadata.py`. Added a new `OrderLines` table (`OrderLineID` PK, `OrderID` FK→Orders, `ProductID` FK→Products, `Quantity`, `LineAmount`) with 8 rows spanning Orders 1–7, and a `Category` TEXT column on `Products` (Widget/Gadget→Electronics, Lamp/Sofa→Home). This gives Accounts→Orders→OrderLines (2-hop) and Accounts→Orders→OrderLines→Products (3-hop, filterable by Category). Ground truth was hand-designed so Umbrella Inc unambiguously has both the most Electronics order lines (3) and the highest Electronics quantity (6) of any account. 7 new questions were added (10→17); no existing table/column/question/row was renamed or removed.

**Fixture B (Healthcare)** — `tests/test_fixture_b_patients_zero_metadata.py`. Added a `DepartmentID` FK column to `Providers` (Dr. Ha→Cardiology Dept, Dr. Mensah→Orthopedics Dept), giving Patients→Appointments→Providers (2-hop) and Patients→Appointments→Providers→Departments (3-hop). To give the 3-hop ranking an unambiguous winner without touching the pre-existing hardcoded per-patient assertions for patients 1/2/4, 4 new `Appointments` rows were added exclusively for patients 3 and 5 (both previously zero-appointment patients), making Carla Ibanez (patient 3) the clear Cardiology leader (3 appointments vs 2 for patients 1 and 4). This addition changed the total-appointment universe from 7→11 rows, which required updating two pre-existing hardcoded assertions (`Show patients with their appointments.` row count, `How many appointments does each patient have?` breakdown, and `Average appointment cost by patient.` row count 3→5) to stay correct — documented inline in the fixture file as a consequence of the milestone's own additions, not an unrelated change. 7 new questions were added (10→17).

**Fixture C (Logistics)** — `tests/test_fixture_c_unknown_vocabulary.py`. Added a new `Regions` table (`RegionID` PK, `RegionName`) with a `RegionID` FK on `Warehouses`, and a `WarehouseID` FK column on `Shipments`. This gives Containers→Shipments→Warehouses (2-hop) and Containers→Shipments→Warehouses→Regions (3-hop). **Design correction made during this milestone:** the original 3-hop question ("...to the West region?") collided with a pre-existing, unrelated `Carriers.Region` column (West/East) that already existed in this fixture — the real pipeline resolved "West region" against `Carriers.Region` instead of the new `Warehouses→Regions` path, an ambiguity of my own making, not a production defect. The question was renamed to "Which container has the most shipments through the Port Warehouse?" to reference the warehouse by name directly and remove the term collision; the underlying ground truth (CTR-004, 3 shipments) is unchanged. 7 new questions were added (10→17).

**Fixture E (Industrial)** — `tests/test_fixture_e_investigation_loop_industrial.py`. Added a `Cost` REAL NOT NULL column to `WorkOrders` (10 realistic values, Repair jobs costing more than Inspections) to give this fixture a genuine explicit $ measure (previously only count-based ranking existed). 2 new questions were added (`Total maintenance cost by equipment type.`, `Which depot has the highest total maintenance cost?`, exercising the Equipment→WorkOrders→Technicians→Depots 3-hop path with `SUM(Cost)`).

All four fixture files' pre-existing pytest assertions pass cleanly after these extensions (`9 passed` on the final regression run — see Section 15).

---

## 2. Full question suite per domain (17 questions each)

**A — Commerce (Accounts):**
1. How many accounts are there?
2. Show the most recent accounts.
3. Show accounts by status.
4. Show the most active accounts.
5. Show accounts with their orders.
6. How many orders does each account have?
7. Which accounts have the most orders?
8. Average invoice amount by account.
9. Show enterprise accounts.
10. How many enterprise accounts are there?
11. List all accounts.
12. Which account has the highest total order amount?
13. Show the highest performing accounts.
14. Show top accounts.
15. What is each account's profit margin?
16. How many order lines does each account have?
17. Which account bought the most products in the Electronics category?

**B — Healthcare (Patients):**
1. How many patients are there?
2. Show the most recent patients.
3. Show patients by status.
4. Show the most active patients.
5. Show patients with their appointments.
6. How many appointments does each patient have?
7. Which patients have the most appointments?
8. Average appointment cost by patient.
9. Show VIP patients.
10. How many VIP patients are there?
11. List all patients.
12. Which patient has the highest total appointment cost?
13. Show the highest performing patients.
14. Show top patients.
15. Show patient readmission rate.
16. How many appointments does each provider have?
17. Which patient has the most appointments in the Cardiology department?

**C — Logistics (Containers):**
1. How many containers are there?
2. Show the most recent containers.
3. Show containers by status.
4. Show the most active containers.
5. Show containers with their shipments.
6. How many shipments does each container have?
7. Which containers have the most shipments?
8. Average shipment weight by container.
9. Show hazmat containers.
10. How many hazmat containers are there?
11. List all containers.
12. Which container has the highest total shipment weight?
13. Show the highest performing containers.
14. Show top containers.
15. Show container customs clearance status.
16. How many shipments does each warehouse handle?
17. Which container has the most shipments through the Port Warehouse?

**E — Industrial (Equipment):**
1. How many equipment are there?
2. List all equipment.
3. Show Pump equipment.
4. Show equipment by status.
5. Show the most active equipment.
6. Show the most recently installed equipment.
7. Show the most recently serviced equipment.
8. How many Pump equipment are there?
9. Show equipment with their work orders.
10. How many work orders does each technician have?
11. How many work orders come from the North depot?
12. Show equipment with their assigned technicians.
13. Show equipment names and their type.
14. Show equipment warranty status.
15. Show equipment revenue by quarter.
16. Total maintenance cost by equipment type.
17. Which depot has the highest total maintenance cost?

---

## 3. Ground-truth methodology

Every deterministic question's expected result was computed independently in plain Python from the raw `*_ROWS` tuples each fixture module already defines (e.g. `EXPECTED_ORDERLINE_COUNT_BY_ACCOUNT`, `EXPECTED_HIGHEST_TOTAL_ORDER_AMOUNT`, `EXPECTED_MAINTENANCE_COST_BY_TYPE`) — hand-summed/hand-grouped by reading the literal row tuples, never by trusting or reverse-engineering the pipeline's own SQL or output. These same constants are imported directly into the A/B harness (`docs/validation/run_business_learning_ab.py`) so the harness's classifier and the fixture files' own pytest assertions check against the identical, single source of ground truth. For genuinely ambiguous or impossible questions (no qualifier at all; no supporting data anywhere in the schema), the "ground truth" is behavioral, not numeric: a safe refusal (`SAFELY_REFUSED`/`CLARIFICATION_REQUIRED`) is correct, and any `ANSWERED` result is flagged as a wrong-answer regression regardless of what value it returns.

---

## 4. Business Learning OFF results by domain

See `docs/validation/business_learning_ab_results.json` → `domains.<DOMAIN>.OFF.questions` for the full raw data (SQL, rows, every AgentState field). Per-question summary:

### A_commerce (OFF)
| Question | Status | Classification | Latency(s) | AI interp | AI adj | Investig rounds |
|---|---|---|---|---|---|---|
| How many accounts are there? | ANSWERED | VERIFIED_CORRECT | 0.381 | 0 | 0 | 0 |
| Show the most recent accounts. | ANSWERED | VERIFIED_CORRECT | 7.822 | 1 | 1 | 0 |
| Show accounts by status. | ANSWERED | VERIFIED_CORRECT | 4.866 | 1 | 1 | 0 |
| Show the most active accounts. | ANSWERED | VERIFIED_CORRECT | 9.036 | 1 | 2 | 0 |
| Show accounts with their orders. | ANSWERED | VERIFIED_CORRECT | 3.541 | 0 | 0 | 0 |
| How many orders does each account have? | ANSWERED | VERIFIED_CORRECT | 8.205 | 1 | 2 | 0 |
| Which accounts have the most orders? | ANSWERED | VERIFIED_CORRECT | 7.87 | 1 | 2 | 0 |
| Average invoice amount by account. | ANSWERED | VERIFIED_CORRECT | 11.496 | 1 | 2 | 0 |
| Show enterprise accounts. | ANSWERED | VERIFIED_CORRECT | 4.636 | 1 | 1 | 0 |
| How many enterprise accounts are there? | ANSWERED | VERIFIED_CORRECT | 5.025 | 1 | 1 | 0 |
| List all accounts. | ANSWERED | VERIFIED_CORRECT | 0.277 | 0 | 0 | 0 |
| Which account has the highest total order amount? | ANSWERED | **WRONG** | 7.448 | 1 | 2 | 0 |
| Show the highest performing accounts. | SAFELY_REFUSED | SAFE_REFUSAL | 14.681 | 1 | 1 | 4 |
| Show top accounts. | SAFELY_REFUSED | SAFE_REFUSAL | 2.048 | 1 | 0 | 0 |
| What is each account's profit margin? | SAFELY_REFUSED | SAFE_REFUSAL | 11.872 | 1 | 0 | 4 |
| How many order lines does each account have? | SAFELY_REFUSED | SAFE_REFUSAL | 15.277 | 1 | 1 | 4 |
| Which account bought the most products in the Electronics category? | ANSWERED | VERIFIED_CORRECT | 7.942 | 1 | 2 | 0 |

### B_healthcare (OFF)
| Question | Status | Classification | Latency(s) | AI interp | AI adj | Investig rounds |
|---|---|---|---|---|---|---|
| How many patients are there? | ANSWERED | VERIFIED_CORRECT | 0.172 | 0 | 0 | 0 |
| Show the most recent patients. | ANSWERED | VERIFIED_CORRECT | 4.479 | 1 | 1 | 0 |
| Show patients by status. | ANSWERED | VERIFIED_CORRECT | 4.505 | 1 | 1 | 0 |
| Show the most active patients. | ANSWERED | VERIFIED_CORRECT | 5.682 | 1 | 2 | 0 |
| Show patients with their appointments. | ANSWERED | VERIFIED_CORRECT | 0.351 | 0 | 0 | 0 |
| How many appointments does each patient have? | ANSWERED | VERIFIED_CORRECT | 6.359 | 1 | 2 | 0 |
| Which patients have the most appointments? | ANSWERED | VERIFIED_CORRECT | 6.471 | 1 | 2 | 0 |
| Average appointment cost by patient. | ANSWERED | VERIFIED_CORRECT | 6.132 | 1 | 2 | 0 |
| Show VIP patients. | ANSWERED | VERIFIED_CORRECT | 4.877 | 1 | 1 | 0 |
| How many VIP patients are there? | ANSWERED | VERIFIED_CORRECT | 4.405 | 1 | 1 | 0 |
| List all patients. | ANSWERED | VERIFIED_CORRECT | 0.161 | 0 | 0 | 0 |
| Which patient has the highest total appointment cost? | ANSWERED | **WRONG** | 6.83 | 1 | 2 | 0 |
| Show the highest performing patients. | SAFELY_REFUSED | SAFE_REFUSAL | 15.078 | 1 | 1 | 4 |
| Show top patients. | SAFELY_REFUSED | SAFE_REFUSAL | 1.641 | 1 | 0 | 0 |
| Show patient readmission rate. | SAFELY_REFUSED | SAFE_REFUSAL | 10.238 | 1 | 0 | 3 |
| How many appointments does each provider have? | ANSWERED | VERIFIED_CORRECT | 6.149 | 1 | 2 | 0 |
| Which patient has the most appointments in the Cardiology department? | SAFELY_REFUSED | SAFE_REFUSAL | 19.081 | 1 | 2 | 5 |

### C_logistics (OFF)
| Question | Status | Classification | Latency(s) | AI interp | AI adj | Investig rounds |
|---|---|---|---|---|---|---|
| How many containers are there? | ANSWERED | VERIFIED_CORRECT | 0.226 | 0 | 0 | 0 |
| Show the most recent containers. | ANSWERED | VERIFIED_CORRECT | 4.436 | 1 | 1 | 0 |
| Show containers by status. | ANSWERED | VERIFIED_CORRECT | 4.257 | 1 | 1 | 0 |
| Show the most active containers. | ANSWERED | VERIFIED_CORRECT | 6.168 | 1 | 2 | 0 |
| Show containers with their shipments. | ANSWERED | VERIFIED_CORRECT | 0.427 | 0 | 0 | 0 |
| How many shipments does each container have? | ANSWERED | VERIFIED_CORRECT | 7.233 | 1 | 2 | 0 |
| Which containers have the most shipments? | ANSWERED | VERIFIED_CORRECT | 7.026 | 1 | 2 | 0 |
| Average shipment weight by container. | ANSWERED | VERIFIED_CORRECT | 6.916 | 1 | 2 | 0 |
| Show hazmat containers. | SAFELY_REFUSED | SAFE_REFUSAL | 11.182 | 1 | 0 | 3 |
| How many hazmat containers are there? | SAFELY_REFUSED | SAFE_REFUSAL | 10.847 | 1 | 0 | 3 |
| List all containers. | ANSWERED | VERIFIED_CORRECT | 0.204 | 0 | 0 | 0 |
| Which container has the highest total shipment weight? | SAFELY_REFUSED | SAFE_REFUSAL | 17.716 | 1 | 1 | 4 |
| Show the highest performing containers. | SAFELY_REFUSED | SAFE_REFUSAL | 15.769 | 1 | 1 | 4 |
| Show top containers. | SAFELY_REFUSED | SAFE_REFUSAL | 1.85 | 1 | 0 | 0 |
| Show container customs clearance status. | ANSWERED | **WRONG** | 4.211 | 1 | 1 | 0 |
| How many shipments does each warehouse handle? | ANSWERED | VERIFIED_CORRECT | 7.35 | 1 | 2 | 0 |
| Which container has the most shipments through the Port Warehouse? | ANSWERED | VERIFIED_CORRECT | 7.267 | 1 | 2 | 0 |

### E_industrial (OFF)
| Question | Status | Classification | Latency(s) | AI interp | AI adj | Investig rounds |
|---|---|---|---|---|---|---|
| How many equipment are there? | ANSWERED | VERIFIED_CORRECT | 3.412 | 0 | 0 | 0 |
| List all equipment. | ANSWERED | VERIFIED_CORRECT | 0.188 | 0 | 0 | 0 |
| Show Pump equipment. | SAFELY_REFUSED | SAFE_REFUSAL | 12.612 | 1 | 0 | 3 |
| Show equipment by status. | SAFELY_REFUSED | SAFE_REFUSAL | 21.6 | 1 | 0 | 4 |
| Show the most active equipment. | ANSWERED | VERIFIED_CORRECT | 8.23 | 1 | 2 | 0 |
| Show the most recently installed equipment. | ANSWERED | VERIFIED_CORRECT | 6.086 | 1 | 1 | 0 |
| Show the most recently serviced equipment. | ANSWERED | VERIFIED_CORRECT | 6.15 | 1 | 1 | 0 |
| How many Pump equipment are there? | SAFELY_REFUSED | SAFE_REFUSAL | 16.496 | 1 | 0 | 4 |
| Show equipment with their work orders. | CLARIFICATION_REQUIRED | SAFE_REFUSAL | 0.373 | 0 | 0 | 0 |
| How many work orders does each technician have? | SAFELY_REFUSED | SAFE_REFUSAL | 15.202 | 1 | 0 | 4 |
| How many work orders come from the North depot? | SAFELY_REFUSED | SAFE_REFUSAL | 15.262 | 1 | 0 | 4 |
| Show equipment with their assigned technicians. | ANSWERED | VERIFIED_CORRECT | 9.506 | 1 | 2 | 0 |
| Show equipment names and their type. | ANSWERED | VERIFIED_CORRECT | 5.43 | 1 | 1 | 0 |
| Show equipment warranty status. | SAFELY_REFUSED | SAFE_REFUSAL | 14.238 | 1 | 1 | 4 |
| Show equipment revenue by quarter. | SAFELY_REFUSED | SAFE_REFUSAL | 16.088 | 1 | 0 | 4 |
| Total maintenance cost by equipment type. | SAFELY_REFUSED | SAFE_REFUSAL | 17.087 | 1 | 2 | 4 |
| Which depot has the highest total maintenance cost? | ANSWERED | **WRONG** | 10.181 | 1 | 3 | 0 |

---

## 5. Business Learning ON results by domain

### A_commerce (ON)
| Question | Status | Classification | Latency(s) | AI interp | AI adj | Investig rounds |
|---|---|---|---|---|---|---|
| How many accounts are there? | ANSWERED | VERIFIED_CORRECT | 0.221 | 0 | 0 | 0 |
| Show the most recent accounts. | ANSWERED | VERIFIED_CORRECT | 4.003 | 1 | 1 | 0 |
| Show accounts by status. | ANSWERED | VERIFIED_CORRECT | 5.398 | 1 | 1 | 0 |
| Show the most active accounts. | ANSWERED | VERIFIED_CORRECT | 7.456 | 1 | 2 | 0 |
| Show accounts with their orders. | ANSWERED | VERIFIED_CORRECT | 2.603 | 0 | 0 | 0 |
| How many orders does each account have? | ANSWERED | VERIFIED_CORRECT | 9.328 | 1 | 2 | 0 |
| Which accounts have the most orders? | ANSWERED | VERIFIED_CORRECT | 8.996 | 1 | 2 | 0 |
| Average invoice amount by account. | ANSWERED | VERIFIED_CORRECT | 9.942 | 1 | 2 | 0 |
| Show enterprise accounts. | ANSWERED | VERIFIED_CORRECT | 4.47 | 1 | 1 | 0 |
| How many enterprise accounts are there? | ANSWERED | **WRONG** | 4.079 | 1 | 1 | 0 |
| List all accounts. | ANSWERED | VERIFIED_CORRECT | 0.191 | 0 | 0 | 0 |
| Which account has the highest total order amount? | ANSWERED | **WRONG** | 9.426 | 1 | 3 | 0 |
| Show the highest performing accounts. | SAFELY_REFUSED | SAFE_REFUSAL | 15.497 | 1 | 1 | 4 |
| Show top accounts. | SAFELY_REFUSED | SAFE_REFUSAL | 1.541 | 1 | 0 | 0 |
| What is each account's profit margin? | SAFELY_REFUSED | SAFE_REFUSAL | 9.414 | 1 | 0 | 4 |
| How many order lines does each account have? | SAFELY_REFUSED | SAFE_REFUSAL | 14.652 | 1 | 1 | 4 |
| Which account bought the most products in the Electronics category? | ANSWERED | VERIFIED_CORRECT | 7.729 | 1 | 2 | 0 |

### B_healthcare (ON)
| Question | Status | Classification | Latency(s) | AI interp | AI adj | Investig rounds |
|---|---|---|---|---|---|---|
| How many patients are there? | ANSWERED | VERIFIED_CORRECT | 0.199 | 0 | 0 | 0 |
| Show the most recent patients. | ANSWERED | VERIFIED_CORRECT | 4.645 | 1 | 1 | 0 |
| Show patients by status. | ANSWERED | VERIFIED_CORRECT | 3.875 | 1 | 1 | 0 |
| Show the most active patients. | ANSWERED | VERIFIED_CORRECT | 6.575 | 1 | 2 | 0 |
| Show patients with their appointments. | ANSWERED | VERIFIED_CORRECT | 0.33 | 0 | 0 | 0 |
| How many appointments does each patient have? | ANSWERED | VERIFIED_CORRECT | 6.426 | 1 | 2 | 0 |
| Which patients have the most appointments? | ANSWERED | VERIFIED_CORRECT | 7.466 | 1 | 2 | 0 |
| Average appointment cost by patient. | ANSWERED | VERIFIED_CORRECT | 7.175 | 1 | 2 | 0 |
| Show VIP patients. | ANSWERED | VERIFIED_CORRECT | 4.749 | 1 | 1 | 0 |
| How many VIP patients are there? | ANSWERED | VERIFIED_CORRECT | 4.972 | 1 | 1 | 0 |
| List all patients. | ANSWERED | VERIFIED_CORRECT | 0.195 | 0 | 0 | 0 |
| Which patient has the highest total appointment cost? | SAFELY_REFUSED | SAFE_REFUSAL | 17.445 | 1 | 1 | 4 |
| Show the highest performing patients. | SAFELY_REFUSED | SAFE_REFUSAL | 15.127 | 1 | 1 | 4 |
| Show top patients. | SAFELY_REFUSED | SAFE_REFUSAL | 1.773 | 1 | 0 | 0 |
| Show patient readmission rate. | SAFELY_REFUSED | SAFE_REFUSAL | 10.947 | 1 | 0 | 3 |
| How many appointments does each provider have? | ANSWERED | VERIFIED_CORRECT | 6.635 | 1 | 2 | 0 |
| Which patient has the most appointments in the Cardiology department? | SAFELY_REFUSED | SAFE_REFUSAL | 1.981 | 1 | 0 | 0 |

### C_logistics (ON)
| Question | Status | Classification | Latency(s) | AI interp | AI adj | Investig rounds |
|---|---|---|---|---|---|---|
| How many containers are there? | ANSWERED | VERIFIED_CORRECT | 0.217 | 0 | 0 | 0 |
| Show the most recent containers. | ANSWERED | VERIFIED_CORRECT | 4.653 | 1 | 1 | 0 |
| Show containers by status. | ANSWERED | VERIFIED_CORRECT | 4.383 | 1 | 1 | 0 |
| Show the most active containers. | ANSWERED | VERIFIED_CORRECT | 5.649 | 1 | 2 | 0 |
| Show containers with their shipments. | ANSWERED | VERIFIED_CORRECT | 0.309 | 0 | 0 | 0 |
| How many shipments does each container have? | ANSWERED | **WRONG** | 6.555 | 1 | 2 | 0 |
| Which containers have the most shipments? | SAFELY_REFUSED | SAFE_REFUSAL | 16.423 | 1 | 1 | 4 |
| Average shipment weight by container. | ANSWERED | **WRONG** | 6.535 | 1 | 2 | 0 |
| Show hazmat containers. | SAFELY_REFUSED | SAFE_REFUSAL | 19.369 | 1 | 0 | 5 |
| How many hazmat containers are there? | ANSWERED | **WRONG** | 4.039 | 1 | 1 | 0 |
| List all containers. | ANSWERED | VERIFIED_CORRECT | 0.162 | 0 | 0 | 0 |
| Which container has the highest total shipment weight? | SAFELY_REFUSED | SAFE_REFUSAL | 16.558 | 1 | 1 | 4 |
| Show the highest performing containers. | SAFELY_REFUSED | SAFE_REFUSAL | 18.247 | 1 | 1 | 4 |
| Show top containers. | SAFELY_REFUSED | SAFE_REFUSAL | 1.521 | 1 | 0 | 0 |
| Show container customs clearance status. | ANSWERED | **WRONG** | 3.966 | 1 | 1 | 0 |
| How many shipments does each warehouse handle? | ANSWERED | VERIFIED_CORRECT | 6.181 | 1 | 2 | 0 |
| Which container has the most shipments through the Port Warehouse? | ANSWERED | **WRONG** | 10.148 | 1 | 3 | 0 |

### E_industrial (ON)
| Question | Status | Classification | Latency(s) | AI interp | AI adj | Investig rounds |
|---|---|---|---|---|---|---|
| How many equipment are there? | ANSWERED | VERIFIED_CORRECT | 3.652 | 0 | 0 | 0 |
| List all equipment. | ANSWERED | VERIFIED_CORRECT | 0.253 | 0 | 0 | 0 |
| Show Pump equipment. | SAFELY_REFUSED | SAFE_REFUSAL | 18.615 | 1 | 0 | 4 |
| Show equipment by status. | SAFELY_REFUSED | SAFE_REFUSAL | 20.8 | 1 | 0 | 4 |
| Show the most active equipment. | SAFELY_REFUSED | SAFE_REFUSAL | 2.371 | 1 | 0 | 0 |
| Show the most recently installed equipment. | ANSWERED | VERIFIED_CORRECT | 6.036 | 1 | 1 | 0 |
| Show the most recently serviced equipment. | ANSWERED | VERIFIED_CORRECT | 5.054 | 1 | 1 | 0 |
| How many Pump equipment are there? | ANSWERED | **WRONG** | 5.591 | 1 | 1 | 0 |
| Show equipment with their work orders. | CLARIFICATION_REQUIRED | SAFE_REFUSAL | 4.526 | 0 | 0 | 0 |
| How many work orders does each technician have? | SAFELY_REFUSED | SAFE_REFUSAL | 13.098 | 1 | 0 | 3 |
| How many work orders come from the North depot? | SAFELY_REFUSED | SAFE_REFUSAL | 15.369 | 1 | 0 | 4 |
| Show equipment with their assigned technicians. | ANSWERED | VERIFIED_CORRECT | 8.323 | 1 | 2 | 0 |
| Show equipment names and their type. | ANSWERED | VERIFIED_CORRECT | 5.272 | 1 | 1 | 0 |
| Show equipment warranty status. | SAFELY_REFUSED | SAFE_REFUSAL | 15.153 | 1 | 1 | 4 |
| Show equipment revenue by quarter. | SAFELY_REFUSED | SAFE_REFUSAL | 9.836 | 1 | 0 | 3 |
| Total maintenance cost by equipment type. | ANSWERED | VERIFIED_CORRECT | 10.743 | 1 | 3 | 0 |
| Which depot has the highest total maintenance cost? | ANSWERED | **WRONG** | 10.132 | 1 | 3 | 0 |

---

## 6. Combined A/B comparison

### Per-domain (VERIFIED_CORRECT / SAFE_REFUSAL / WRONG / UNSAFE, out of 17 each)

| Domain | OFF verified | ON verified | OFF safe-refusal | ON safe-refusal | OFF wrong | ON wrong | OFF unsafe | ON unsafe |
|---|---|---|---|---|---|---|---|---|
| A_commerce | 12 | 11 | 4 | 4 | 1 | 2 | 0 | 0 |
| B_healthcare | 12 | 12 | 4 | 5 | 1 | 0 | 0 | 0 |
| C_logistics | 11 | 7 | 5 | 5 | 1 | 5 | 0 | 0 |
| E_industrial | 7 | 7 | 9 | 8 | 1 | 2 | 0 | 0 |
| **Total** | **42** | **37** | **22** | **22** | **4** | **9** | **0** | **0** |

### Combined across all 4 domains (68 questions per condition)

| Metric | OFF | ON | DELTA (ON−OFF) |
|---|---|---|---|
| Verified correct | 42 | 37 | **−5** |
| Safe refusal | 22 | 22 | 0 |
| Wrong | 4 | 9 | **+5** |
| Unsafe | 0 | 0 | 0 |
| Investigation activations (questions with ≥1 round) | 18 | 16 | −2 |
| Total investigation rounds | 69 | 62 | −7 |
| Avg rounds/question (all 68) | 1.01 | 0.91 | −0.10 |
| Avg rounds/activated question | 3.83 | 3.88 | +0.04 |
| Normal-model calls (AI interpreter, summed) | 56 | 56 | 0 |
| Normal-model calls (candidate adjudicator, summed) | 67 | 66 | −1 |
| Strong-model calls (escalated=True rounds) | 0 | 0 | 0 |
| Investigation tool calls (summed) | 50 | 44 | −6 |
| Total latency (sum of all 68 question latencies) | 516.26s | 495.20s | −21.06s |

Strong-model calls are 0 in both conditions because `ENABLE_INVESTIGATION_STRONG_MODEL_ESCALATION` (a separate, off-by-default flag gating the strong-model escalation path inside the Investigation Agent) was not enabled for this run — it is orthogonal to Business Learning and out of this milestone's scope, so its absence is expected, not a defect.

---

## 7. Business-map quality classification by domain

For each domain's ON run, the actual persisted `preliminary_business_map` (read back via `get_preliminary_business_map`) was compared field-by-field against the real schema/data written into that fixture.

| Domain | likely_domain | major_entities | fact/dimension tables | candidate_measures | candidate_dates |
|---|---|---|---|---|---|
| A_commerce | "e-commerce/transactions" — **VERIFIED_CORRECT** | Invoice, Order, Account, Product, ActivityEvent — **VERIFIED_CORRECT** (all 5 real entities named, nothing invented) | Facts: Invoices/Orders/OrderLines/ActivityEvents; Dims: Accounts/Products — **VERIFIED_CORRECT** classification (Orders as fact is a defensible call given its FK+numeric shape) | Invoices.Amount, Orders.TotalAmount, OrderLines.LineAmount, OrderLines.Quantity, Products.Price — **VERIFIED_CORRECT**, all 5 are real, plausible measures; correctly excluded every ID/PK/FK column | Invoices.InvoiceDate, Orders.OrderDate, Accounts.CreatedDate, ActivityEvents.EventDate — **VERIFIED_CORRECT** |
| B_healthcare | "healthcare/patient records" — **VERIFIED_CORRECT** | Patient, Appointment, Provider, Department, VisitEvent — **VERIFIED_CORRECT** | Facts: Appointments/VisitEvents; Dims: Patients/Providers/Departments — **VERIFIED_CORRECT** | Appointments.Cost — **VERIFIED_CORRECT** (the only real business measure in this schema) | Appointments.AppointmentDate, VisitEvents.EventDate, Patients.RegisteredDate — **VERIFIED_CORRECT** |
| C_logistics | "logistics/shipping" — **VERIFIED_CORRECT** | Shipment, Warehouse, Container, Carrier, Region, ScanEvent — **VERIFIED_CORRECT**, all 6 real | Facts: Shipments/ScanEvents; Dims: Warehouses/Containers/Carriers/Regions — **VERIFIED_CORRECT** | Shipments.Weight — **VERIFIED_CORRECT** | Shipments.ShipmentDate, Containers.DepartedDate, ScanEvents.EventDate — **VERIFIED_CORRECT** |
| E_industrial | "industrial equipment/maintenance" — **VERIFIED_CORRECT** | WorkOrder, Technician, Equipment, Depot — **VERIFIED_CORRECT** (correctly did NOT name the 3 decoy tables as entities) | Facts: WorkOrders; Dims: EquipmentAssignments/Technicians/Equipment/Depots/EQ_Equipment/EquipmentHistory — **PLAUSIBLE_BUT_UNVALIDATED**: the 3 decoy tables (EQ_Equipment/EquipmentHistory/EquipmentAssignments) are unreachable by any real FK and are NOT genuine dimensions of Equipment, but the map has no relationship evidence to know that (structural-metadata-only, no FK graph consulted) — its own `notes` field says exactly this ("Some tables have unknown purposes, requiring further investigation"), which is the correct, honest hedge for a bounded, single LLM call | WorkOrders.Cost — **VERIFIED_CORRECT** | WorkOrders.WorkOrderDate, EquipmentAssignments.AssignedDate, Equipment.InstalledDate, Equipment.LastServicedDate, EquipmentHistory.ArchivedDate — **VERIFIED_CORRECT** (correctly identified BOTH of Equipment's two distinct date columns as separate candidates, not conflated) |

**Total hallucinated identifiers across all 4 domains: 0 / 0** (programmatically verified: every `table_fqn` and `column_name` in every `fact_tables`/`dimension_tables`/`bridge_tables`/`candidate_measures`/`candidate_dates` entry across all 4 ON runs was cross-checked against the real schema each fixture module defines; zero were absent). This matches the expected result and confirms `_infer_preliminary_business_map`'s `_valid_ref`/known_fqns filtering works empirically, not just by code inspection.

---

## 8. Preliminary-map consumption proof (Part 4)

Captured by calling `core.semantic.ai_interpreter.build_grounding_vocabulary` + `_build_user_prompt` directly against each run's real, persisted state immediately after connect, for the same sample question in both conditions.

**A_commerce — OFF** (`ENABLE_PORTABLE_KNOWLEDGE_LAYER=False`), question "How many accounts are there?":
```
Question: "How many accounts are there?"
```
No grounding vocabulary block at all — nothing injected.

**A_commerce — ON**, same question:
```
Question: "How many accounts are there?"

Known business vocabulary for this data source (prefer these terms when they match):
- Known entities: Payment, Event, User
- Known business domains: Finance, Identity & Access, Operations

UNVALIDATED preliminary business-learning hints for this data source (a bounded AI guess made before any question was asked — may be wrong; treat only as a hint, never as confirmed fact):
- Guessed domain: e-commerce/transactions
- Guessed entities: Invoice, Order, Account, Product, ActivityEvent
- Guessed candidate measures: main.Invoices.Amount, main.Orders.TotalAmount, main.OrderLines.LineAmount, main.OrderLines.Quantity, main.Products.Price
```

Analogous OFF→(nothing)/ON→(full UNVALIDATED block) pairs were captured for all 4 domains (B_healthcare, C_logistics, E_industrial) — see `sample_prompt_injection` in the raw JSON for each domain/condition. This is direct, reproducible proof that the preliminary map text physically reaches the AI question interpreter's prompt when Business Learning is ON, and is completely absent when it is OFF — the map is not merely generated-and-ignored.

(One incidental, unrelated observation: the "Known business vocabulary" block above the UNVALIDATED section — entities like "Payment"/"Event"/"User" — comes from a DIFFERENT, always-on mechanism, `data.entity_service.get_entity_summary`/`data.domain_service.get_domain_summary`, which is populated by the existing on-demand entity/domain assignment pipeline once a question has touched a table; it is not part of Business Learning and fires identically with the flag OFF once enough questions have run. It happened to be non-empty here because it was captured after several questions had already answered.)

---

## 9. Connection-time Business Learning benchmark by domain

Single-run numbers, from each ON run's real `BusinessLearningTrace`:

| Domain | fingerprint_computed | portable_match | bootstrap_activated | tables_structurally_examined | ai_calls | duration_ms |
|---|---|---|---|---|---|---|
| A_commerce | True | NONE | True | 6 | 1 | 31,746 |
| B_healthcare | True | NONE | True | 5 | 1 | 2,965 |
| C_logistics | True | NONE | True | 6 | 1 | 3,968 |
| E_industrial | True | NONE | True | 7 | 1 | 3,844 |

`portable_match=NONE` in all 4 cases is expected and correct — every fixture here is a brand-new synthetic schema with no matching portable knowledge package, so every run takes path B (bounded bootstrap LLM call), never path A (portable match/activation). `ai_calls=1` in all 4 confirms the "one bounded LLM call regardless of schema size" design. A_commerce's 31.7s duration is a clear outlier against the other three (~3–4s each); this was not re-run to determine whether it was a one-off real-OpenAI-API latency spike or a genuine size effect (A_commerce has the most tables at 6, tied with C_logistics's 6, so table count alone doesn't explain a ~9x difference). **Multi-run median/range (spec Part 11) was NOT done this pass — explicitly deferred; only single-run numbers are reported here.**

---

## 10. 3-hop ranking proof result

| Domain | Question | OFF status/classification | ON status/classification |
|---|---|---|---|
| A_commerce | Which account bought the most products in the Electronics category? | ANSWERED / VERIFIED_CORRECT (Umbrella Inc, 2 distinct Electronics products) | ANSWERED / VERIFIED_CORRECT (same) |
| B_healthcare | Which patient has the most appointments in the Cardiology department? | SAFELY_REFUSED / SAFE_REFUSAL | SAFELY_REFUSED / SAFE_REFUSAL |
| C_logistics | Which container has the most shipments through the Port Warehouse? | ANSWERED / VERIFIED_CORRECT (CTR-004 correctly ranked first) | ANSWERED / **WRONG** (grouped by `ContainerClass`, not by individual container — see Section 12) |
| E_industrial | Which depot has the highest total maintenance cost? | ANSWERED / **WRONG** (used `MAX(Cost)` instead of `SUM(Cost)` — see Section 12) | ANSWERED / **WRONG** (same defect, both conditions) |

**Pass/fail summary: 2/4 domains produced a verified-correct 3-hop answer in at least one condition (A always correct; C correct OFF only; B never answered — safe refusal only; E never correct in either condition).** The 3-hop JOIN *path itself* was correctly constructed in every single case that answered (Equipment→WorkOrders→Technicians→Depots and Containers→Shipments→Warehouses→(Class) both produced real, executable multi-hop SQL) — the failures were in aggregation-function selection (SUM vs MAX) or grouping-attribute selection (ContainerID vs ContainerClass), not in relationship/JOIN resolution. This is a genuinely mixed result, not a clean pass.

---

## 11. Date-semantics proof result

Fixture E's `InstalledDate` vs `LastServicedDate` distinction was re-verified fresh in this run, both conditions:

| Condition | "most recently installed" SQL | "most recently serviced" SQL |
|---|---|---|
| OFF | `SELECT Equipment.InstalledDate ... ORDER BY (implicit, single column selected)` | `SELECT Equipment.LastServicedDate ...` |
| ON | `SELECT Equipment.InstalledDate ...` | `SELECT Equipment.LastServicedDate ...` |

Both conditions correctly selected two **different** date columns for the two differently-worded questions, both classified VERIFIED_CORRECT (row count = full Equipment table, 8 rows, in all 4 runs). This reconfirms Fixture E's pre-existing, previously-established date-semantics proof; Business Learning made no observable difference to this behavior in either direction.

---

## 12. Ranking-regression result

Every explicit/implicit/ambiguous/activity/recency/2-hop/3-hop ranking-shaped question across all 4 domains was audited individually. **Result: NOT zero WRONG ranking outcomes — 5 distinct WRONG rankings were found, all pre-existing capability gaps unmasked by this milestone's new questions, not regressions caused by Business Learning specifically (4 of the 5 WRONG rankings occur identically or similarly in BOTH conditions):**

1. **"Highest total X" (explicit $-measure ranking) systematically fails to use SUM.** Confirmed in 3 independent domains:
   - A_commerce, "Which account has the highest total order amount?" — OFF used `COUNT(OrderID)` (the same plan as "most orders"); ON used `MAX(Amount)`. Neither run used `SUM(TotalAmount)`.
   - B_healthcare, "Which patient has the highest total appointment cost?" — OFF used `COUNT(AppointmentID)` instead of `SUM(Cost)`. (ON safely refused this one instead of answering wrong.)
   - E_industrial, "Which depot has the highest total maintenance cost?" — **both** OFF and ON used `MAX(Cost)` instead of `SUM(Cost)` — the single highest individual WorkOrder cost (700.0), not the depot-grouped total (2590.0 for North Depot). This is the same defect pattern reproduced identically twice.
   This is a real, systematic capability gap: the phrase "highest total X" is not reliably parsed as "aggregate-then-rank"; it is frequently resolved as either a bare count-ranking or a row-level MAX, both of which are silently wrong rather than refused.

2. **"Show container customs clearance status." conflated an unsupported business concept with a superficially similar real column.** Both OFF and ON answered with the literal `Containers.Status` values (InTransit/Delivered/Pending) instead of refusing — there is no customs-clearance data anywhere in this schema. This is not a ranking defect but is a real ANSWERED-when-impossible regression, included here because it's the same "should have refused, didn't" family.

3. **C_logistics ON showed 3 additional WRONG answers not present OFF**, all in the same session: "How many shipments does each container have?" and "Average shipment weight by container." both silently regrouped by `ContainerClass` (Hazmat/Standard, 2 rows) instead of by individual `ContainerID` (3 rows expected); "How many hazmat containers are there?" returned the count of ALL containers (5), ignoring the hazmat filter entirely; and the new 3-hop "Port Warehouse" question also fell into the ContainerClass-grouping bug. This cluster of 4 WRONG answers, concentrated entirely in one domain's ON run, is the largest single contributor to the ON-condition's higher wrong-answer count (see Section 13.A) and is the strongest specific evidence this pass found that Business Learning can coincide with — though this data cannot prove it *causes* — a worse outcome on a given run.

4. A_commerce ON also answered "How many enterprise accounts are there?" WRONG (count=5, ignoring the Enterprise-tier filter) — a previously-documented non-deterministic shape (see the fixture's own inline comments) that landed on the wrong side this run.

**No ambiguous question (bare "Show top X.") was ever silently answered** — all 4 domains × 2 conditions safely refused every bare "top X" question, 8/8. **No implicit-ranking question ("highest performing X") was ever answered with an unranked bare LIST/COUNT masquerading as a ranking** — every "highest performing" run either safely refused or (when it did answer, which did not happen in this run) was expected to pass the fan-out-ratio guard; all 8/8 instances safely refused.

---

## 13. Explicit numbered answers

**A. Does Business Learning improve VERIFIED correctness?** **NO.** Combined: OFF 42/68 (61.8%) vs ON 37/68 (54.4%) — a **decrease** of 5 verified-correct answers. Per domain: A_commerce 12→11, B_healthcare 12→12 (flat), C_logistics 11→7 (largest drop), E_industrial 7→7 (flat). No domain showed an improvement.

**B. Does Business Learning reduce safe refusals?** **NO change, combined.** OFF 22/68 vs ON 22/68 — identical. Per domain: A_commerce 4→4, C_logistics 5→5 (flat); B_healthcare 4→5 (+1); E_industrial 9→8 (−1). These per-domain shifts roughly cancel out combined.

**C. Does Business Learning reduce Investigation Agent usage (activation count)?** **Slightly, YES.** Combined activations (questions with ≥1 investigation round): OFF 18/68 vs ON 16/68 (−2). Largest single-domain effect: E_industrial 8→6 (−2); A_commerce and B_healthcare unchanged (3→3); C_logistics unchanged (4→4).

**D. Does it reduce investigation rounds?** **Yes, modestly, in total.** OFF 69 total rounds vs ON 62 (−7, ≈10% reduction), driven almost entirely by E_industrial (31→22). Per-activated-question average rounds barely moved (3.83→3.88, i.e. essentially flat) — the reduction is from fewer questions triggering investigation at all (see C above), not from each investigation running fewer rounds once triggered.

**E. Does it reduce total latency?** **Yes, marginally.** OFF 516.26s vs ON 495.20s (−21.06s, ≈4.1% combined). Not uniform: A_commerce (−7.5s) and E_industrial (−23.3s) improved; B_healthcare (−2.1s) roughly flat; C_logistics got **slower** (+11.8s), consistent with its extra failed/refused attempts in Section 12 point 3.

**F. How much time does Business Learning add at connection?** Single-run numbers only (median/range NOT measured this pass, explicitly deferred): A_commerce 31.7s, B_healthcare 3.0s, C_logistics 4.0s, E_industrial 3.8s. Three of four domains cluster around 3–4s; A_commerce's 31.7s is an unexplained outlier not investigated further this session.

**G. Does the preliminary business map actually influence question-time AI?** **YES**, with direct evidence: Section 8 shows the exact injected prompt text is present (domain guess, entity guesses, candidate measures) when ON and completely absent when OFF, captured via the real `build_grounding_vocabulary`/`_build_user_prompt` code path against real persisted state, not inferred.

**H. Can preliminary learning cause unvalidated physical mappings to bypass live validation?** **Expected NO — evidence supports that, with zero exceptions found.** Across all 136 question runs, `legacy_semantic_planner_competing_fallback` (the actual "legacy planner re-interpreted business meaning and bypassed the AI-first validated path" signal per `core/orchestrator/context_builder.py`'s own inline documentation) was `False` on every single run — 0/136. Separately, `legacy_semantic_fallback_used` (a broader, benign flag that is also `True` for a deliberate deterministic-grounding skip on trivially simple questions) was `True` on 24/136 runs, but 100% of those (24/24) carried the reason `no_ai_interpretation` — the documented, safe, zero-AI-needed shortcut for questions like bare COUNT/LIST that never required interpretation in the first place, not a competing/legacy re-planning path. No run of any kind returned a row count exceeding the schema's real data volume, and no run's SQL referenced a table/column that doesn't exist.

**I. Does 3-hop ranking work?** **Mixed, not a clean YES.** Per domain: A_commerce YES (both conditions); B_healthcare N/A (never answered — safe refusal only, both conditions); C_logistics YES OFF / NO ON (wrong grouping grain); E_industrial NO (both conditions — SUM/MAX confusion). See Section 10 for full detail; the JOIN path itself was always correctly constructed even when the final answer was wrong.

**J. Does date-semantic selection distinguish business dates correctly?** **YES**, confirmed fresh this session in both conditions (Section 11) — `InstalledDate` and `LastServicedDate` were never conflated.

**K/L/M.** **N/A — deferred this session.** Portability/fresh-clone/CCPP-package validation was explicitly out of scope for this pass (see Section 14).

**N. Wrong answers introduced?** **Exact count: 9 WRONG classifications in the ON condition vs 4 in the OFF condition — a net increase of 5 WRONG answers when Business Learning is ON, in this measurement.** This is the single most important finding of this report and directly contradicts the assumption that Business Learning is safe-by-default from a correctness standpoint; see Section 12 for the full breakdown and Section 15 for caveats on how much of this is attributable to real-OpenAI-call non-determinism vs a genuine ON-condition effect.

**O. Unsafe queries?** **Exact count: 0 in both conditions**, using the corrected `legacy_semantic_planner_competing_fallback`-based gate (see Answer H) plus the row-count-vs-schema-volume check built into the harness. (An earlier draft pass of the harness incorrectly flagged 24 simple COUNT/LIST answers as "UNSAFE" by keying off the broader `legacy_semantic_fallback_used` flag alone; this was identified as a harness bug — not a pipeline defect — and corrected before the final run reported here, per Answer H's explanation.)

**P. Legacy semantic fallback count?** Confirmed via `legacy_semantic_fallback_used` across every single one of the 136 question runs: **24 True (12 OFF + 12 ON), all 24 with reason `no_ai_interpretation` (the benign deterministic-grounding-skip case), 0 of any other reason.** The stricter, actually-dangerous signal `legacy_semantic_planner_competing_fallback` is **0/136**, confirming the milestone's real target invariant holds.

**Q. GO/NO-GO for the real CCPP delete/reconnect acceptance test.** Based only on the evidence gathered this session (Business-Learning-safety/correctness portion only — portability/fresh-clone/CCPP-package validation was explicitly deferred, so this does NOT speak to the full GO/NO-GO checklist): **safety criteria (Answers H, O, P) are satisfied — no legacy-planner bypass, no unsafe query, and the map-injection mechanism is proven to work as designed (Answer G).** However, **correctness evidence this session argues against a GO on the "Business Learning does no correctness harm" premise specifically** — it measured strictly more wrong answers with Business Learning ON than OFF across 4 independent domains (Answer N), including a cluster of 4 new WRONG answers concentrated in one domain's ON run that did not occur OFF. **Recommendation: NO-GO on enabling `ENABLE_PORTABLE_KNOWLEDGE_LAYER` for the real CCPP acceptance test until this session's WRONG-answer findings (Section 12) are independently triaged** — specifically whether they are real-OpenAI-call non-determinism (equally likely to appear OFF on a different run) or a genuine, reproducible effect of the injected preliminary-map prompt content steering the interpreter toward wrong measure/grouping choices. This session's single-run-per-condition design (Section 15) cannot distinguish those two explanations, and that distinction is the single most important piece of evidence still missing before a real GO/NO-GO call can be made confidently.

---

## 14. Deferred this session

The following spec parts were explicitly deferred by user scoping decision, to keep this session's evidence rigorous on a bounded scope rather than shallow across all report items:

- **Parts 9–13**: negative control (deliberately wrong preliminary map), misleading-map control, learning-promotion tracing (`promote_source_to_package`), repeat-question determinism test, multi-run median/range for connection-time benchmarking.
- **Parts 17–22**: portability regression testing, CCPP knowledge-package validation and dry-run, fresh-clone instructor simulation.
- **Parts 24–27**: system-wide portability audit, legacy-fallback formal proof beyond what Section 13.H/P already covers, full acceptance-suite regression (the locked 98-question CCPP suite was not run this session).

None of these were started this session; they are listed here as explicitly out of scope, not as incomplete work-in-progress.

---

## 15. Remaining limitations / caveats

- **Single run per (domain, condition) pair.** Every number in this report is from exactly one real-OpenAI-call run per cell, not a median across repeats. Several shapes in this codebase's own pre-existing fixture files are explicitly documented as non-deterministic across real OpenAI calls (e.g. "most active accounts" sometimes safely refuses). The 5-answer swing in verified-correct count (Answer N) is large enough to be a real signal, but this design cannot rule out that a second OFF run or a second ON run would land on different individual questions — repeat-run variance was not measured (deferred, see Section 14).
- **Two confirmed harness/fixture-authoring bugs were found and fixed during this session**, both in my own new code, not production code: (1) an `UnboundLocalError` from inserting new assertions before a helper function's definition in Fixture A's own test file (fixed by reordering); (2) an over-broad "UNSAFE" classification in the harness that conflated the benign `legacy_semantic_fallback_used` flag with the actual dangerous `legacy_semantic_planner_competing_fallback` signal, which would have wrongly reported 24 simple, correct COUNT/LIST answers as unsafe (fixed before the final run; see Answer O). Two further checker-only bugs (ID-vs-name column matching for 2-hop group-bys, and a stale 3-key expected-dict after adding new appointment rows) were found via manual inspection of the raw JSON and corrected via an offline reclassification pass over the same already-captured, unmodified raw results (no new OpenAI calls were needed for that correction — only the classification logic changed).
- **One fixture-design bug of my own was found and fixed**: Fixture C's original 3-hop question phrasing ("...to the West region?") collided with a pre-existing, unrelated `Carriers.Region` column, causing the real pipeline to (correctly, given the ambiguity) resolve against the wrong table. The question was reworded to remove the ambiguity; this is disclosed in Section 1 and is not hidden.
- **The SUM-vs-COUNT/MAX "highest total X" defect (Section 12, item 1) is the most load-bearing finding in this report** and was independently reproduced in 3 of 4 domains, in both directions with different wrong SQL each time (COUNT once, MAX twice) — this strongly suggests a real, generalizable interpretation gap rather than a one-off fluke, but it was not attempted to be fixed or root-caused at the code level this session (out of scope per the constraints: "do not fix production code, only report").
- **Judgment calls**: classifying "answered with the right numeric value but via a plan whose column-selection differs from what a human would first guess" (e.g. `count_OrderID` vs `SUM(TotalAmount)`, both producing a `row_count` but only one being semantically correct) required inspecting the actual returned column names and values row-by-row for every WRONG classification (Section 6's WRONG counts are not automated pattern-matches on status alone — every one was individually eyeballed against the captured SQL and rows before being confirmed as a genuine defect rather than a harness/checker bug, as documented in Section 15's first bullet).
- **A_commerce's 31.7s Business Learning connect-time duration (Section 9)** is an unexplained outlier against the other three domains' 3–4s and was not investigated further (would require a repeat run or profiling, both out of scope this pass).
