import csv
import io

from flask import Blueprint, render_template, request, redirect, url_for, jsonify, session, flash

from app.models import db, Part, Revision, BomItem, ConversionRule, ConversionRun, MBomItem, ECR, AuditLog, User, \
    InventoryItem, Supplier, PurchaseOrder, WorkOrder, Customer, SalesOrder, utcnow
from app.conversion_engine import run_conversion_for_part
from app.genai_assist import draft_ecr_description
from app.erp_engine import generate_erp_documents, receive_purchase_order, complete_work_order
from app.auth import current_user, login_required, role_required, ENGINEER, MFG_ENGINEER, APPROVER, ROLE_LABELS

bp = Blueprint("main", __name__)


@bp.context_processor
def inject_user():
    return {"current_user": current_user(), "ROLE_LABELS": ROLE_LABELS}


def build_ebom_tree(part_id, _visited=None):
    """Recursively build a nested dict tree of the eBOM rooted at part_id, for display."""
    _visited = _visited or set()
    part = db.session.get(Part, part_id)
    if part is None or part_id in _visited:
        return None
    _visited = _visited | {part_id}

    children = []
    for bi in BomItem.query.filter_by(parent_part_id=part_id).all():
        child_tree = build_ebom_tree(bi.child_part_id, _visited)
        if child_tree:
            children.append({"quantity": bi.quantity, "ref": bi.reference_designator, "node": child_tree})

    return {"part": part, "children": children}


# --- auth (role picker, not real login) -------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = db.session.get(User, int(request.form["user_id"]))
        if user:
            session["user_id"] = user.id
            flash(f"Logged in as {user.name} ({ROLE_LABELS[user.role]}).", "success")
        return redirect(url_for("main.dashboard"))
    users = User.query.order_by(User.role, User.name).all()
    return render_template("login.html", users=users)


@bp.route("/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    return redirect(url_for("main.dashboard"))


# --- dashboard / parts --------------------------------------------------------

@bp.route("/")
def dashboard():
    parts = Part.query.order_by(Part.part_number).all()
    recent_runs = ConversionRun.query.order_by(ConversionRun.created_at.desc()).limit(5).all()
    pending_ecrs = ECR.query.filter_by(status="PENDING").count()
    open_orders = SalesOrder.query.filter(SalesOrder.status.in_(["NEW", "IN_PRODUCTION"])).count()
    low_stock = InventoryItem.query.filter(InventoryItem.on_hand_qty < InventoryItem.reorder_point).count()
    return render_template(
        "dashboard.html", parts=parts, recent_runs=recent_runs, pending_ecrs=pending_ecrs,
        open_orders=open_orders, low_stock=low_stock,
    )


@bp.route("/parts/<int:part_id>")
def part_detail(part_id):
    part = Part.query.get_or_404(part_id)
    tree = build_ebom_tree(part_id)
    runs = ConversionRun.query.filter_by(root_part_id=part_id).order_by(ConversionRun.created_at.desc()).all()
    return render_template("part_detail.html", part=part, tree=tree, runs=runs)


@bp.route("/parts/<int:part_id>/convert", methods=["POST"])
@role_required(ENGINEER, MFG_ENGINEER)
def convert_part(part_id):
    run = run_conversion_for_part(part_id, actor=current_user().name)
    return redirect(url_for("main.diff_view", run_id=run.id))


@bp.route("/parts/<int:part_id>/import-ebom", methods=["POST"])
@role_required(ENGINEER, MFG_ENGINEER)
def import_ebom(part_id):
    """
    Import child parts/links from a CSV, simulating a BOM export from a CAD
    system. Expected header: child_part_number,child_description,
    child_part_type,quantity,reference_designator
    """
    parent = Part.query.get_or_404(part_id)
    file = request.files.get("file")
    if not file or file.filename == "":
        flash("Choose a CSV file to import.", "warning")
        return redirect(url_for("main.part_detail", part_id=part_id))

    stream = io.StringIO(file.stream.read().decode("utf-8-sig"))
    reader = csv.DictReader(stream)

    created_parts, created_links = 0, 0
    for row in reader:
        part_number = row["child_part_number"].strip()
        child = Part.query.filter_by(part_number=part_number).first()
        if child is None:
            child = Part(
                part_number=part_number,
                description=row.get("child_description", "").strip() or part_number,
                part_type=row.get("child_part_type", "COMPONENT").strip() or "COMPONENT",
                status="RELEASED",
            )
            db.session.add(child)
            db.session.flush()
            db.session.add(Revision(part_id=child.id, rev_code="A", notes="Imported from CAD export"))
            created_parts += 1

        db.session.add(BomItem(
            parent_part_id=parent.id, child_part_id=child.id,
            quantity=float(row.get("quantity", 1) or 1),
            reference_designator=row.get("reference_designator", "").strip() or None,
        ))
        created_links += 1

    db.session.add(AuditLog(
        entity_type="Part", entity_id=parent.id, action="IMPORTED", actor=current_user().name,
        detail=f"eBOM import under {parent.part_number}: {created_parts} new part(s), {created_links} link(s)",
    ))
    db.session.commit()
    flash(f"Imported {created_links} eBOM link(s) ({created_parts} new part(s) created).", "success")
    return redirect(url_for("main.part_detail", part_id=part_id))


@bp.route("/runs/<int:run_id>/diff")
def diff_view(run_id):
    run = ConversionRun.query.get_or_404(run_id)
    ebom_tree = build_ebom_tree(run.root_part_id)
    mbom_items = MBomItem.query.filter_by(run_id=run_id).all()

    # group mBOM lines by parent for a simple flat-by-assembly display
    by_parent = {}
    for mi in mbom_items:
        by_parent.setdefault(mi.parent_part_id, []).append(mi)

    # procurement rollup: total extended quantity needed per leaf part, summed
    # across every assembly it appears under (a part can appear more than once)
    rollup = {}
    for mi in mbom_items:
        entry = rollup.setdefault(mi.child_part_id, {"part": mi.child, "total": 0.0, "make_or_buy": mi.make_or_buy})
        entry["total"] += mi.extended_quantity
    rollup = sorted(rollup.values(), key=lambda e: e["part"].part_number)

    return render_template(
        "diff.html", run=run, ebom_tree=ebom_tree, by_parent=by_parent, rollup=rollup,
    )


# --- ECR workflow --------------------------------------------------------------

@bp.route("/ecrs")
def ecr_list():
    ecrs = ECR.query.order_by(ECR.created_at.desc()).all()
    parts = Part.query.order_by(Part.part_number).all()
    return render_template("ecrs.html", ecrs=ecrs, parts=parts)


@bp.route("/ecrs", methods=["POST"])
@role_required(ENGINEER, MFG_ENGINEER)
def ecr_create():
    ecr = ECR(
        part_id=int(request.form["part_id"]),
        description=request.form["description"],
        requested_by=current_user().name,
    )
    db.session.add(ecr)
    db.session.flush()
    db.session.add(AuditLog(entity_type="ECR", entity_id=ecr.id, action="CREATED", actor=ecr.requested_by,
                             detail=ecr.description))
    db.session.commit()
    return redirect(url_for("main.ecr_list"))


@bp.route("/ecrs/draft", methods=["POST"])
@role_required(ENGINEER, MFG_ENGINEER)
def ecr_draft():
    """GenAI assist: draft an ECR description from a part + rough note."""
    part = db.session.get(Part, int(request.form["part_id"]))
    if part is None:
        return jsonify({"error": "part not found"}), 404
    description, source = draft_ecr_description(
        part.part_number, part.description, request.form.get("rough_note", ""),
    )
    return jsonify({"description": description, "source": source})


@bp.route("/ecrs/<int:ecr_id>/approve", methods=["POST"])
@role_required(APPROVER)
def ecr_approve(ecr_id):
    ecr = ECR.query.get_or_404(ecr_id)
    approver = current_user().name
    ecr.status = "APPROVED"
    ecr.approved_by = approver
    ecr.approved_at = utcnow()

    # approving an ECR bumps the affected part's revision
    part = ecr.part
    prior_rev_code = part.current_revision.rev_code if part.current_revision else "@"
    for rev in part.revisions:
        rev.is_current = False
    next_code = chr(ord(prior_rev_code) + 1)
    db.session.add(Revision(part_id=part.id, rev_code=next_code, notes=f"ECR #{ecr.id}: {ecr.description}"))

    db.session.add(AuditLog(entity_type="ECR", entity_id=ecr.id, action="APPROVED", actor=approver,
                             detail=f"Part {part.part_number} bumped to rev {next_code}"))
    db.session.commit()
    return redirect(url_for("main.ecr_list"))


@bp.route("/ecrs/<int:ecr_id>/reject", methods=["POST"])
@role_required(APPROVER)
def ecr_reject(ecr_id):
    ecr = ECR.query.get_or_404(ecr_id)
    ecr.status = "REJECTED"
    db.session.add(AuditLog(entity_type="ECR", entity_id=ecr.id, action="REJECTED", actor=current_user().name))
    db.session.commit()
    return redirect(url_for("main.ecr_list"))


@bp.route("/audit")
def audit_log():
    entries = AuditLog.query.order_by(AuditLog.timestamp.desc()).all()
    return render_template("audit.html", entries=entries)


# --- ERP module: inventory, purchase orders, work orders ----------------------
# Rather than stopping at a JSON export and calling it an integration point,
# a conversion run's BUY lines become real purchase orders (net of on-hand
# stock, grouped by supplier) and MAKE lines become real work orders.
# Receiving a PO or completing a work order moves actual inventory (see
# app/erp_engine.py).

@bp.route("/runs/<int:run_id>/generate-erp", methods=["POST"])
@role_required(ENGINEER, MFG_ENGINEER)
def generate_erp(run_id):
    result = generate_erp_documents(run_id, actor=current_user().name)
    if result["created"]:
        flash(
            f"Generated {len(result['purchase_orders'])} purchase order(s) and "
            f"{len(result['work_orders'])} work order(s) from this run.", "success",
        )
    else:
        flash("ERP documents were already generated for this run.", "info")
    return redirect(url_for("main.erp_dashboard"))


@bp.route("/erp")
def erp_dashboard():
    inventory = InventoryItem.query.join(Part).order_by(Part.part_number).all()
    purchase_orders = PurchaseOrder.query.order_by(PurchaseOrder.created_at.desc()).all()
    work_orders = WorkOrder.query.order_by(WorkOrder.created_at.desc()).all()
    low_stock = [inv for inv in inventory if inv.below_reorder_point]
    return render_template(
        "erp_dashboard.html", inventory=inventory, purchase_orders=purchase_orders,
        work_orders=work_orders, low_stock=low_stock,
    )


@bp.route("/erp/po/<int:po_id>")
def po_detail(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    return render_template("po_detail.html", po=po)


@bp.route("/erp/po/<int:po_id>/receive", methods=["POST"])
@role_required(MFG_ENGINEER, APPROVER)
def po_receive(po_id):
    po = receive_purchase_order(po_id, actor=current_user().name)
    flash(f"{po.po_number} received — stock updated.", "success")
    return redirect(url_for("main.po_detail", po_id=po.id))


@bp.route("/erp/wo/<int:wo_id>")
def wo_detail(wo_id):
    wo = WorkOrder.query.get_or_404(wo_id)
    components = MBomItem.query.filter_by(run_id=wo.run_id, parent_part_id=wo.part_id).all()
    return render_template("wo_detail.html", wo=wo, components=components)


@bp.route("/erp/wo/<int:wo_id>/release", methods=["POST"])
@role_required(MFG_ENGINEER)
def wo_release(wo_id):
    wo = WorkOrder.query.get_or_404(wo_id)
    if wo.status == "PLANNED":
        wo.status = "RELEASED"
        db.session.add(AuditLog(entity_type="WorkOrder", entity_id=wo.id, action="RELEASED",
                                 actor=current_user().name, detail=f"{wo.wo_number} released to the shop floor"))
        db.session.commit()
        flash(f"{wo.wo_number} released to the shop floor.", "success")
    return redirect(url_for("main.wo_detail", wo_id=wo.id))


@bp.route("/erp/wo/<int:wo_id>/complete", methods=["POST"])
@role_required(MFG_ENGINEER)
def wo_complete(wo_id):
    try:
        wo = complete_work_order(wo_id, actor=current_user().name)
        flash(f"{wo.wo_number} completed — components consumed, finished stock updated.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("main.wo_detail", wo_id=wo_id))


# --- CRM: customers, sales orders, and a small order-book analytics view ------
# This is the "sales" leg of the Cloud ERP scope (accounting/sales/procurement/
# production) and the "customer data analysis platform utilizing CRM" bullet.
# A sales order is the demand-side counterpart to everything else in the app:
# "producing" one runs the same conversion + ERP-generation pipeline used from
# a part's page, and links the result back to the order.

@bp.route("/crm")
def crm_dashboard():
    customers = Customer.query.order_by(Customer.name).all()
    orders = SalesOrder.query.order_by(SalesOrder.created_at.desc()).all()

    by_status = {}
    for o in orders:
        by_status[o.status] = by_status.get(o.status, 0) + 1

    by_region = {"DOMESTIC": 0, "OVERSEAS": 0}
    for o in orders:
        by_region[o.customer.region] = by_region.get(o.customer.region, 0) + o.quantity
    region_total = sum(by_region.values()) or 1

    by_customer = {}
    for o in orders:
        entry = by_customer.setdefault(o.customer_id, {"customer": o.customer, "order_count": 0, "total_qty": 0.0})
        entry["order_count"] += 1
        entry["total_qty"] += o.quantity
    by_customer = sorted(by_customer.values(), key=lambda e: e["total_qty"], reverse=True)

    by_part = {}
    for o in orders:
        entry = by_part.setdefault(o.part_id, {"part": o.part, "total_qty": 0.0})
        entry["total_qty"] += o.quantity
    by_part = sorted(by_part.values(), key=lambda e: e["total_qty"], reverse=True)[:10]
    parts = Part.query.order_by(Part.part_number).all()

    return render_template(
        "crm_dashboard.html", customers=customers, orders=orders, by_status=by_status,
        by_region=by_region, region_total=region_total, by_customer=by_customer, by_part=by_part, parts=parts,
    )


@bp.route("/customers", methods=["POST"])
@login_required
def customer_create():
    customer = Customer(
        name=request.form["name"], region=request.form.get("region", "DOMESTIC"),
        country=request.form.get("country") or None, contact_email=request.form.get("contact_email") or None,
    )
    db.session.add(customer)
    db.session.commit()
    flash(f"Added customer {customer.name}.", "success")
    return redirect(url_for("main.crm_dashboard"))


@bp.route("/sales-orders", methods=["POST"])
@login_required
def sales_order_create():
    next_num = (db.session.query(db.func.max(SalesOrder.id)).scalar() or 0) + 1
    promised_date = request.form.get("promised_date") or None
    order = SalesOrder(
        so_number=f"SO-{2000 + next_num}",
        customer_id=int(request.form["customer_id"]),
        part_id=int(request.form["part_id"]),
        quantity=float(request.form.get("quantity", 1) or 1),
        promised_date=promised_date,
        created_by=current_user().name,
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(AuditLog(entity_type="SalesOrder", entity_id=order.id, action="CREATED", actor=order.created_by,
                             detail=f"{order.so_number}: {order.quantity:g}x {order.part.part_number} "
                                    f"for {order.customer.name}"))
    db.session.commit()
    flash(f"Created {order.so_number}.", "success")
    return redirect(url_for("main.sales_order_detail", order_id=order.id))


@bp.route("/sales-orders/<int:order_id>")
def sales_order_detail(order_id):
    order = SalesOrder.query.get_or_404(order_id)
    return render_template("sales_order_detail.html", order=order)


@bp.route("/sales-orders/<int:order_id>/produce", methods=["POST"])
@role_required(ENGINEER, MFG_ENGINEER)
def sales_order_produce(order_id):
    order = SalesOrder.query.get_or_404(order_id)
    actor = current_user().name
    run = run_conversion_for_part(order.part_id, actor=actor)
    generate_erp_documents(run.id, actor=actor)
    order.run_id = run.id
    order.status = "IN_PRODUCTION"
    db.session.add(AuditLog(entity_type="SalesOrder", entity_id=order.id, action="IN_PRODUCTION", actor=actor,
                             detail=f"{order.so_number}: linked to conversion run #{run.id}"))
    db.session.commit()
    flash(f"{order.so_number} is now in production (run #{run.id}, ERP documents generated).", "success")
    return redirect(url_for("main.sales_order_detail", order_id=order.id))


@bp.route("/sales-orders/<int:order_id>/fulfill", methods=["POST"])
@role_required(MFG_ENGINEER, APPROVER)
def sales_order_fulfill(order_id):
    order = SalesOrder.query.get_or_404(order_id)
    order.status = "FULFILLED"
    order.fulfilled_at = utcnow()
    order.fulfilled_by = current_user().name
    db.session.add(AuditLog(entity_type="SalesOrder", entity_id=order.id, action="FULFILLED",
                             actor=order.fulfilled_by, detail=f"{order.so_number} marked fulfilled"))
    db.session.commit()
    flash(f"{order.so_number} marked fulfilled.", "success")
    return redirect(url_for("main.sales_order_detail", order_id=order.id))


# --- API: the "integration boundary" a real ERP would consume ---

@bp.route("/api/runs/<int:run_id>/mbom.json")
def api_mbom_json(run_id):
    items = MBomItem.query.filter_by(run_id=run_id).all()
    return jsonify([
        {
            "parent_part_number": mi.parent.part_number,
            "child_part_number": mi.child.part_number,
            "quantity": mi.quantity,
            "extended_quantity": mi.extended_quantity,
            "make_or_buy": mi.make_or_buy,
            "work_center": mi.work_center,
            "source": mi.source,
        }
        for mi in items
    ])
