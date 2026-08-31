# BOM Bridge — eBOM → mBOM Conversion Platform

A web platform that manages engineering part/drawing data and converts a
multi-level **engineering BOM (eBOM)** into a **manufacturing BOM (mBOM)**
through a configurable rules engine — with an approval workflow and full
audit trail for every change.

## The problem this solves

An eBOM reflects how a product was *designed* (by CAD/assembly logic). An
mBOM reflects how it's actually *built and procured* (routing, make/buy,
substitute suppliers, packaging, scrap allowance) — a different structure
over the same underlying product. Someone has to bridge that gap. This
project builds the bridge as a real system, not a one-off script: rules live
in the database and can be changed without redeploying code, because in a
real manufacturing environment those rules change constantly.

## What it does

1. **PDM-lite** — parts and drawings with versioned revisions (`DRAFT` →
   `RELEASED` → `OBSOLETE`).
2. **eBOM authoring** — a multi-level assembly tree (`BomItem`, self-referencing).
3. **Conversion engine** (`app/conversion_engine.py`) — a 4-stage pipeline
   driven entirely by data in a `ConversionRule` table:
   - **explode** the eBOM tree, computing each part's *extended quantity* —
     the total needed per one top-level unit, multiplied down every level
     (a part 2-deep with quantity 2, under an assembly needed 4×, needs 8 —
     not 2. See the "Procurement rollup" section of the diff view.)
   - **substitute** approved alternate parts
   - **inject** manufacturing-only items the eBOM never had (packaging, hardware
     kits) — injected quantities respect the same extended-quantity math
   - **classify** make/buy, work center routing, and scrap-adjusted quantity
   - all keyed by part type, so adding a new rule is a database row, not a code change
4. **Diff view** — eBOM and generated mBOM side by side, with injected/substituted
   lines highlighted, so the transformation is visible, not just trusted.
5. **ECR workflow** — a released part changes only through an Engineering
   Change Request that requires approval; approval bumps the part's revision
   and writes an audit log entry. Nothing mutates silently.
6. **API export** (`/api/runs/<id>/mbom.json`) — the integration boundary
   where this would hand off to a real ERP for procurement/production planning.
7. **Role-gated workflow** — Engineer / Manufacturing Engineer / Approver.
   Only an Approver can approve or reject an ECR; only Engineer/Mfg Engineer
   can run a conversion or import an eBOM. There's no password (this is a
   demo, not an auth system) — you pick a seeded user, and the role is
   enforced server-side on every write route, not just hidden in the UI.
8. **eBOM CSV import** (`/parts/<id>/import-ebom`) — simulates a BOM export
   landing from a CAD system: upload a CSV of child parts/quantities under
   any existing assembly, and it creates the parts (if new) and links them
   in. Try it with `sample_data/motor_v2_import.csv`.
9. **GenAI-assisted ECR drafting** (`app/genai_assist.py`) — an Engineer can
   type a rough note ("switch to ductile iron, vibration issue") and get a
   properly-worded ECR description drafted by Claude (`claude-opus-5`), with
   a deterministic template fallback if `ANTHROPIC_API_KEY` isn't set, so the
   app runs for anyone without a key.
10. **Built-in ERP module** (`app/erp_engine.py`) — closed as a real loop
    instead of a JSON stub:
    - **Item master / inventory** (`InventoryItem`) — on-hand stock, reorder
      point, standard cost, and preferred supplier per part, separate from
      the PDM `Part` record the way it would be in a real ERP integration.
    - **"Generate ERP documents"** on a run's diff view explodes its mBOM
      into real documents: BUY lines become **purchase order** lines, netted
      against on-hand stock and grouped by supplier; MAKE lines become **work
      orders**. A part with no item-master record yet gets one created on the
      spot, the way a new part number showing up on a feed would in a live
      integration.
    - **Receiving a PO** adds its lines to stock. **Completing a work order**
      consumes the assembly's mBOM component quantities from inventory and
      adds the finished quantity — and is blocked with a clear error if
      component stock is short, rather than silently going negative.
    - Nothing else is allowed to move on-hand quantity, mirroring how a real
      ERP treats inventory as something only a transaction changes.
11. **CRM / sales orders** (`/crm`) — the sales leg of the platform. A
    minimal `Customer` record (with a `DOMESTIC`/`OVERSEAS` region) and a
    `SalesOrder` for a quantity of a top-level assembly. **Producing** an
    order runs the *same* conversion-engine + erp_engine pipeline used from a
    part's page and links the resulting run back to the order — so demand
    (sales) and supply (PDM → mBOM → ERP) are one data model, not two
    disconnected demos. The dashboard aggregates the order book by status,
    domestic-vs-overseas volume, and top ordered assemblies.
12. **Security hardening** (`app/__init__.py`) — CSRF protection
    (Flask-WTF) on every state-changing route, since every one of them
    trusts the session cookie alone to know who's acting; and baseline
    response headers (`X-Content-Type-Options`, `X-Frame-Options`, a scoped
    `Content-Security-Policy`, `Referrer-Policy`). Both are asserted by
    tests, not just wired up and left unverified — see
    `tests/test_security.py`.

## Architecture

```
Browser (Flask + Jinja templates, Bootstrap)
        │
        ▼
Flask routes (app/routes.py)  ──►  Conversion engine (app/conversion_engine.py)
        │                                  │  pure functions, no DB coupling
        ▼                                  ▼
SQLAlchemy models (app/models.py) ──► SQLite (bombridge.db)
```

The conversion engine is deliberately split into pure functions
(`explode_ebom`, `apply_substitutes`, `inject_items`, `classify_edges`) that
take and return plain dicts. This means the core business logic — the part
that actually matters — can be unit tested with no database, no Flask app
context, and no fixtures beyond a few dicts. See `tests/test_conversion_engine.py`.

The ERP module (`app/erp_engine.py`) follows the same split: a pure
`plan_erp_documents` function (tested in `tests/test_erp_engine.py`) decides
which mBOM lines become PO lines vs. work orders, and thin persistence
wrappers (`generate_erp_documents`, `receive_purchase_order`,
`complete_work_order`) load real data, call it, and write the result —
tested end-to-end over HTTP in `tests/test_erp_routes.py`.

## Running it

```bash
python -m venv .venv
./.venv/Scripts/activate        # .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

python seed.py                  # creates bombridge.db with a sample CNC spindle eBOM + 3 users
python run.py                   # http://127.0.0.1:5000

python -m pytest -q             # run both the engine unit tests and the route integration tests
```

Log in as **Priya Nair** (Engineer) or **Vikram Desai** (Approver) — no
password needed, this demo's "login" is a role picker. Open the seeded
`SPINDLE-ASSY-001` part, click **Convert to mBOM**, and view the diff —
you'll see substituted bearings, injected packaging/fastener kits, make/buy
classification, work-center routing, and scrap-adjusted quantities, all
driven by the 8 seeded rules in `seed.py`. Then click **Generate ERP
documents** on that diff view and open **ERP** in the nav bar: BUY lines
became purchase orders grouped by supplier (net of the starting stock seeded
in `seed.py`), MAKE lines became work orders. Log in as **Ravi Shah**
(Manufacturing Engineer) to receive a PO (adds stock) and complete a work
order (consumes component stock, adds finished stock — try it before
receiving its PO to see it correctly refuse for lack of stock). Then open
**CRM**: `SO-2001` is already produced (its run and ERP documents were
generated by `seed.py`); click **Produce** on `SO-2002` or `SO-2003` to see
the same pipeline run from a sales order, then log back in as Ravi Shah and
**Mark fulfilled**. Then try submitting and approving an ECR, and importing
`sample_data/motor_v2_import.csv` under `MOTOR-ASSY-001` to see the eBOM
grow. To try the real GenAI draft (not the template fallback), set
`ANTHROPIC_API_KEY` before running `python run.py`.

## Deploying it

The repo is deploy-ready for [Render](https://render.com)'s free tier:

1. Push this repo to GitHub.
2. In Render, **New → Blueprint**, point it at the repo — `render.yaml` at
   the root configures the service, build command, and start command
   automatically. (Or configure a Web Service manually with build command
   `pip install -r requirements.txt` and start command
   `gunicorn "app:create_app()" --bind 0.0.0.0:$PORT`.)
3. Optionally set `ANTHROPIC_API_KEY` in the dashboard to enable the real
   GenAI draft path instead of the template fallback.
4. The default SQLite database resets on every redeploy (most free hosts have
   ephemeral disks) — fine for a demo. To persist data, add a managed Postgres
   instance, add `psycopg[binary]` to `requirements.txt`, and set
   `DATABASE_URL` — `app/__init__.py` already reads it with no other code change.

`gunicorn` only runs on Linux/macOS (no `fcntl` on Windows) — for local dev
keep using `python run.py`; the Procfile/render.yaml are for the deployed host.

## Known simplifications (and what I'd do next)

- No real CAD file parsing — eBOM import is simulated; a production version
  would integrate a CAD/PDM system's export API.
- ECR approval bumps the revision but doesn't yet auto-trigger re-conversion —
  regeneration is a manual "Convert to mBOM" click.
- "Generate ERP documents" is a manual click too, and always orders the full
  shortfall against a part's single preferred supplier rather than doing
  supplier comparison, MOQs, or lead-time-aware scheduling — a real ERP
  integration would own purchasing policy, not this app.
- A sales order's `quantity` is a demand signal for the CRM analytics view;
  it doesn't yet rescale the linked run's generated purchase/work order
  quantities (those are always "per 1 unit of the top-level assembly"). A
  production version would multiply the plan by order quantity before
  generating documents.
- Auth is a role picker, not real authentication (no passwords) — deliberate,
  since the point being demonstrated is server-side role enforcement on the
  workflow, not building a login system. CSRF protection and security
  headers are real, but there's no rate limiting, and `SECRET_KEY` defaults
  to a dev value locally (set a real one via the `SECRET_KEY` env var in any
  real deployment).
- Security here is what's honestly buildable inside a single Flask app
  (role enforcement, CSRF, audit trail, response headers). It doesn't
  attempt to simulate EDR tooling, vulnerability scanning, or CSIRT
  incident-response process — those are organizational/operational
  functions, not application features.
- Deploy config is ready (`render.yaml`, `Procfile`) but not yet actually
  deployed to a live URL — that's a `git push` + a few clicks in Render away.
