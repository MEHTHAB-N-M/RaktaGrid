# RaktaGrid: Emergency Blood Dispatch Framework

**RaktaGrid** is a production-grade Django 5.x monolithic web application engineered to coordinate civic emergency blood distribution across hospitals, regional blood banks, and voluntary donors.

> **CRITICAL CLINICAL & REGULATORY NOTICE**:
> RaktaGrid is a coordination and triage platform designed for emergency logistical routing; it is **not a licensed blood bank management system or laboratory information system (LIS)**. All demo organisations, staff profiles, donors, and inventory numbers are **strictly fictional**. **Real patient names, national identity numbers, or raw medical record identifiers must NEVER be entered into this system.**

---

## 1. Problem Statement: The Three Flaws RaktaGrid Fixes

Traditional emergency blood coordination in municipal regions suffers from three operational bottlenecks:

1. **Race Conditions & Double-Allocation**:
   In high-pressure mass-casualty or trauma scenarios, multiple hospital triage units telephone or submit requisition forms for the same scarce unit (such as O-negative whole blood). Without atomic row-level isolation, multiple requisitions book the same physical bag, resulting in devastating clinical delays when the physical cold-box arrives empty.
2. **Medical Cooldown Violations & Donor Depletion**:
   Emergency blood calls broadcast over social networks or static directories frequently contact individuals who donated whole blood within the last few weeks. Contacting donors before the clinical 90-day whole-blood recovery window endangers donor health and yields unusable units.
3. **Emergency Access Friction**:
   Frontline paramedics, triage nurses, and citizens in transit cannot afford authentication barriers, CAPTCHA delays, or complex portal logins when ascertaining whether life-saving units exist in a district.

---

## 2. How It Matches the Real Blood Supply Chain

RaktaGrid mirrors the physical movement of whole blood from donor phlebotomy to patient cross-match:

- **Donation & Quarantine**: Whole blood collected by blood bank phlebotomists enters the ledger strictly in `QUARANTINED` status. Units cannot be dispatched or reserved while awaiting transmissible disease screening (HIV, HBV, HCV, Syphilis, Malaria).
- **Release to Stock**: Only after laboratory safety clearance does the owning facility release quarantined bags into `AVAILABLE` cold storage.
- **Three-Tier Movement Waterfall**:
  1. **Tier 1 (Own Hospital Stock)**: The hospital checks its own cold storage first. The engine attempts exact ABO/Rh matching, followed by safe red-cell compatibility (saving O-negative universal donors for last). If found, an atomic 2-hour reservation lock is granted.
  2. **Tier 2 (Regional Blood Banks)**: If local hospital inventory is empty, the requisition routes to registered active regional blood banks holding matching units, ordered by geographic Haversine proximity. Blood bank officers review their queue to accept/reserve or clinically reject.
  3. **Tier 3 (Voluntary Donor Proximity Cascade)**: When regional cold storage across all blood banks is depleted, the system automatically activates voluntary donors within 50 km, filtering only donors who have cleared the mandatory 90-day cooldown.
- **Laboratory Cross-Matching & Dispatch**: Blood cannot be issued until laboratory cross-matching with patient blood samples is completed. Ticking the physical cross-match checklist transitions the bag to `DISPATCHED`, fulfills the requisition, and issues a tamper-evident chain-of-custody dispatch manifest.
- **Expiry & Automatic Reservation Lapsing**: Unused units past their CPDA-1 storage expiration date (35–42 days) are expired by background jobs. Units reserved but not issued within 2 hours automatically lapse back to available stock so other critical patients can be saved.

---

## 3. Roles & Scoped Access Control

- **Hospital Staff**:
  - Bound to their specific Hospital (`org_type='HOSPITAL'`).
  - Create emergency requisitions for their hospital.
  - Check own cold storage inventory.
  - Route requisitions to nearby blood banks.
  - Complete cross-match verification and issue blood from own stock.
  - Access the voluntary donor proximity directory and reveal contact numbers during fallback.
  - Add and CSV-import hospital inventory bags.
  - View hospital-scoped audit logs.
- **Blood Bank Staff**:
  - Bound to their specific Blood Bank (`org_type='BLOOD_BANK'`).
  - Monitor incoming emergency requisitions routed to their facility.
  - Accept & reserve matching units or clinically reject with reason notes.
  - Issue dispatched blood units and generate manifests.
  - Record whole blood donations from donors (rejecting anyone on active cooldown).
  - Add and release quarantined bags.
  - Batch CSV-import inventory bags and registered donors.
  - View blood-bank-scoped audit logs.
- **Platform Administrators (Superusers)**:
  - System-wide visibility across all facilities, requisitions, inventory, and audit trails.
  - Read-only audit log inspection.
- **Public Citizens & First Responders (No Login)**:
  - Zero-login inspection of live, active cold-storage counts across verified facilities.
  - Strict privacy: No donor data, no patient references, and no bag barcode numbers are exposed.

---

## 4. Key Feature List

- **Three-Tier Allocation Engine**: Automated failover from Hospital &rarr; Regional Blood Bank &rarr; Voluntary Donors.
- **Atomic Concurrency Protection**: Row-level database locking using PostgreSQL `select_for_update(skip_locked=True)`.
- **ABO/Rh Red-Cell Compatibility Matrix**: Exact match prioritized; compatible groups ordered to conserve universal O-negative units.
- **Voluntary Donor Proximity Cascade**: Great-circle Haversine distance ranking (&le; 50 km) with masked phone numbers unmasked only via audited actions.
- **Print-Friendly HTML Dispatch Manifest**: Official chain-of-custody document with browser-native printing (replaces heavy PDF engines).
- **Tamper-Evident Audit Trail**: Every reservation, rejection, lapse, donation, and contact unmasking written atomically in PostgreSQL.
- **HMAC-SHA256 Anonymization**: Donor government and civic IDs are never stored in raw text.
- **Batch CSV Import**: Validation-before-commit engine with per-row diagnostic reports.
- **Civic Rate Limiter**: Database-backed sliding window cache limiting public search queries to 30 requests/min per IP.

---

## 5. Technology Stack (Strict)

- **Backend Framework**: Python 3.12+ / 3.13, Django 5.x
- **Database Driver**: `psycopg[binary]`
- **Database**: PostgreSQL 16+
- **Frontend Structure**: HTML5 Semantic Architecture, CSS3, Vanilla Django Templates
- **UI & Icons**: Bootstrap 5.3 (CDN Bundle), Bootstrap Icons CDN
- **Typography**: System UI / Modern Sans
- **Dependencies**: Zero external Python packages besides Django and psycopg. No ReportLab, no Redis, no Celery, no custom JavaScript.

---

## 6. Local Setup & Installation

### Step 1: Clone & Configure PostgreSQL
Ensure PostgreSQL 16+ is running locally on port 5432. Create the project database:
```sql
CREATE DATABASE ebdf_db;
```

### Step 2: Configure Environment Variables
Set environment variables or create a `.env` file with your local database credentials:
```bash
# Windows PowerShell
$env:DB_NAME="ebdf_db"
$env:DB_USER="postgres"
$env:DB_PASSWORD="your_postgres_password"
$env:DB_HOST="localhost"
$env:DB_PORT="5432"
$env:SECRET_KEY="django-insecure-change-in-production-ebdf-key"
$env:DONOR_ID_PEPPER="ebdf-secure-donor-pepper-salt-2026"
$env:DEBUG="True"
```

### Step 3: Install Python Dependencies
```bash
pip install django "psycopg[binary]"
```

### Step 4: Run Migrations & Initialize Cache Table
```bash
python manage.py migrate
python manage.py createcachetable
```

### Step 5: Seed Demo Environment
Seed fictional hospitals, regional blood banks, inventory bags, donors, and test scenarios:
```bash
python manage.py seed_demo --reset
```

### Step 6: Start the Development Server
```bash
python manage.py runserver
```
Visit the public portal at `http://127.0.0.1:8000/`.

---

## 7. Demo Accounts & Credentials

| Role / Facility | Username | Password | District |
| :--- | :--- | :--- | :--- |
| **Superuser / Admin** | `admin` | `admin123` | System-wide |
| **Demo Metro Hospital A** | `hosp_a_doc` | `password123` | Ernakulam |
| **Demo District Hospital B** | `hosp_b_doc` | `password123` | Thrissur |
| **Demo North Trauma Center C** | `hosp_c_doc` | `password123` | Kozhikode |
| **Demo Central Blood Bank** | `bb_central_staff` | `password123` | Ernakulam |
| **Demo Malabar Zonal Blood Bank** | `bb_malabar_staff` | `password123` | Kozhikode |

### Pre-Seeded Scenarios to Test
1. **Scenario A (Hospital Own-Stock Fulfillment)**:
   Log in as `hosp_a_doc` and open requisition `REQ-00004` (O+). Click **Check Own Stock Now**. Bag `#BAG-HOSPA-O-01` is reserved immediately. Tick the cross-match box and click **Issue Blood** to view the dispatch manifest.
2. **Scenario B (Regional Blood Bank Sourcing)**:
   Log in as `hosp_b_doc` and open requisition `REQ-00005` (A+). Hospital B has zero A+ units. Select **Demo Central Blood Bank** (holding stock) and click **Request from this blood bank**. Log out, log in as `bb_central_staff`, review the incoming queue, and click **Accept and Reserve**.
3. **Scenario C (Voluntary Donor Fallback)**:
   Log in as `hosp_a_doc` and open requisition `REQ-00006` (B-). Because zero B- and zero O- units exist in any facility, click **Activate Voluntary Donor Proximity Cascade**. Inspect nearby eligible donors sorted by Haversine proximity, click **Reveal Contact**, and verify the unmasking audit log.

---

## 8. Architectural & Design Decisions

### A. Row-Level Locking with `select_for_update(skip_locked=True)`
RaktaGrid relies on PostgreSQL's row-level locking primitives. When reserving candidate units:
```python
candidate_bag = BloodInventory.objects.select_for_update(skip_locked=True).filter(
    owner=organisation, blood_type=group, status='AVAILABLE', expiry_date__gte=today
).order_by('expiry_date', 'id').first()
```
`skip_locked=True` prevents thread deadlocks and contention bottlenecks. If another concurrent emergency requisition is currently locking a bag, this transaction skips directly to the next candidate unit.

### B. Why PostgreSQL Over SQLite
SQLite supports only coarse database-level file locks. PostgreSQL provides true multi-version concurrency control (MVCC), granular row-level write locks, native JSONField querying for audit metadata, and robust connection isolation required for concurrent trauma center workloads.

### C. The Clinical 90-Day Cooldown Window
Whole blood donation removes approximately 450–500 mL of erythrocytes. Physiological iron store and hemoglobin reconstitution requires at least 8 to 12 weeks. ActiveDonorManager implements:
```python
cooldown_cutoff = timezone.now().date() - timedelta(days=90)
Q(last_donated_date__isnull=True) | Q(last_donated_date__lte=cooldown_cutoff)
```
Donors who have donated within 89 days are excluded from emergency cascade queries.

### D. Reserve-Then-Issue Separation
Requisition allocation is decoupled into two explicit medical phases:
1. **Reservation**: The physical unit is locked in cold storage for 2 hours (`RESERVED`) so other surgeons cannot take it while samples are sent to the laboratory.
2. **Issue**: Physical release occurs only after serological cross-match verification is confirmed by the transfusion technologist.

### E. Cryptographic HMAC-SHA256 Donor Anonymization
To adhere to civic data protection regulations, raw national identification numbers are never written to disk or database tables. Hashes are derived using a secret server-side pepper:
```python
hmac.new(settings.DONOR_ID_PEPPER.encode('utf-8'), raw_id.strip().encode('utf-8'), hashlib.sha256).hexdigest()
```

### F. ABO/Rh Red-Cell Compatibility Hierarchy
Red-cell transfusions observe strict isohemagglutinin rules:
- `AB+`: Receives `AB+`, `AB-`, `A+`, `A-`, `B+`, `B-`, `O+`, `O-`
- `AB-`: Receives `AB-`, `A-`, `B-`, `O-`
- `A+`: Receives `A+`, `A-`, `O+`, `O-`
- `A-`: Receives `A-`, `O-`
- `B+`: Receives `B+`, `B-`, `O+`, `O-`
- `B-`: Receives `B-`, `O-`
- `O+`: Receives `O+`, `O-`
- `O-`: Receives `O-` only

Exact ABO/Rh matches are always attempted first. When falling back to compatible alternatives, universal O-negative units are conserved and evaluated last.

---

## 9. Connecting Real Data (CSV Import)

Authorised staff can batch-import legacy or external laboratory records using downloadable CSV templates:

### A. Blood Inventory Import (`/import/bags/`)
Columns:
`bag_number, blood_type, volume_ml, expiry_date, status`
- Valid statuses: `AVAILABLE`, `QUARANTINED`, `EXPIRED`, `DISPATCHED`, `RESERVED`
- Dates must be in `YYYY-MM-DD` ISO format.

### B. Registered Donor Import (`/import/donors/`)
Columns (Blood Bank Staff Only):
`donor_reference, full_name, blood_type, contact_number, district, latitude, longitude, last_donated_date`
- `donor_reference` is automatically hashed using HMAC-SHA256 upon ingestion.
- District must match one of the 14 recognized Kerala districts.
- Latitude and Longitude must fall within valid geographic bounds (-90 to 90, -180 to 180).

**All-or-Nothing Guarantee**: If a single row fails validation, the entire transaction is rolled back, and an itemized line-by-line error diagnostic is returned.

---

## 10. Automated Testing Suite

RaktaGrid includes a test suite covering models, managers, multi-tenant boundaries, rate limits, CSV validation, and concurrency:

```bash
python manage.py test
```

### Test Coverage Highlights
- `DonorCooldownTests`: Validates 0 days remaining for new donors, exclusion at 89 days, and eligibility at 90 days.
- `BloodInventoryValidationTests`: Rejects past-expiry bags from being marked `AVAILABLE` or `RESERVED`.
- `ThreeTierWorkflowTests`: Validates own-stock allocation, blood bank failover, rejection rollback, and donor cascade activation.
- `IssueSafetyTests`: Prohibits issue without laboratory cross-match confirmation; verifies quarantined and expired bags are never reserved.
- `BloodCompatibilityTests`: Validates compatible substitute reservation and conservation of O-negative units.
- `ExpireBagsCommandTests`: Verifies automated expiration of overdue stock and releasing of lapsed reservations.
- `RecordDonationAndReleaseTests`: Rejects phlebotomy on cooldown donors and validates quarantine release.
- `OrganisationIsolationTests`: Enforces HTTP 403 Forbidden across multi-tenant boundaries.
- `CSVImportTests`: Proves atomic all-or-nothing rollback on invalid CSV rows.
- `PublicPageAndRateLimitTests`: Tests zero-login access and HTTP 429 enforcement after 30 requests/min.
- `ManifestAndProximityTests`: Verifies Haversine formula precision (111.19 km per degree at equator) and manifest layout.
- `ConcurrencyLockingTests`: Uses `TransactionTestCase` with 5 concurrent threads attempting to reserve 1 scarce unit; verifies exactly 1 reservation succeeds without double-booking.

---

## 11. Maintenance Commands

### Expire Overdue Inventory & Release Lapsed Reservations
```bash
python manage.py expire_bags
```
Safely scans cold storage, marks overdue units as `EXPIRED`, and releases lapsed reservations back to `AVAILABLE` stock while resetting requisitions. Safe for recurring cron execution.

---

## 12. Screenshot Placeholders

Visual assets and walkthrough screenshots are cataloged in `docs/screenshots/`:
- `docs/screenshots/public_search.png`: Zero-login live civic cold storage dashboard.
- `docs/screenshots/hospital_dashboard.png`: Hospital requisition center and inventory management.
- `docs/screenshots/blood_bank_queue.png`: Blood bank incoming requisition dispatch and donor phlebotomy.
- `docs/screenshots/dispatch_manifest.png`: Official chain-of-custody dispatch manifest.
- `docs/screenshots/donor_directory.png`: Voluntary donor proximity fallback with masked contacts.

---

## 13. Not Modelled (Future Scope)

To maintain focused operational clarity, the following clinical and commercial facets are not modelled in this monolith:
1. **Clinician Requisition & Hemovigilance Forms**: Patient clinical history, transfusion reaction reporting, and clinician sign-off protocols.
2. **Laboratory Specimen Handling**: Physical blood tubes, centrifugation, antibody screening (indirect Coombs test), and Rh phenotyping.
3. **Component Fractionation**: Separation of whole blood into Packed Red Blood Cells (PRBC), Fresh Frozen Plasma (FFP), Cryoprecipitate, and Platelet Concentrates (RDP/SDP).
4. **Commercial Processing Fees**: Government-mandated processing, screening, and testing recovery fees.
5. **Replacement-Donor Mandates**: Hospital replacement donor pledges and institutional exchange credits.
