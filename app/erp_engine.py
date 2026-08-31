"""
The ERP module: turns a generated mBOM into the documents a real ERP system
would actually run production off of.

Previously the "Cloud ERP" side of this project was just a JSON export
(`/api/runs/<id>/mbom.json`) with a comment saying an ERP would consume it.
This closes that loop *inside* the app instead: a conversion run explodes
into purchase orders (for BUY lines, net of on-hand stock, grouped by
supplier) and work orders (for MAKE lines), and those documents move real
inventory when a PO is received or a work order is completed -- see
app/routes.py's `/erp/...` routes.

Same split as conversion_engine.py: a pure planning function that takes and
returns plain dicts (unit tested with no database), and a thin persistence
wrapper that loads real data, calls it, and writes the result.
"""


def plan_erp_documents(rollup, inventory_by_part_id, default_supplier_id):
    """
    rollup: list of dicts, one per distinct child part across an mBOM run:
        {part_id, total_qty, make_or_buy, work_center}
    inventory_by_part_id: dict[part_id] -> {on_hand_qty, unit_cost, preferred_supplier_id}
        (parts with no inventory record yet aren't in this dict -- treated as
        zero on-hand stock, bought from the default supplier)
    default_supplier_id: fallback supplier for a part with no preferred one

    Returns a dict:
        po_lines_by_supplier: dict[supplier_id] -> list of {part_id, quantity, unit_cost}
        work_orders: list of {part_id, quantity, work_center}
        covered_by_stock: list of part_id fully satisfied by on-hand stock (no PO line needed)

    Classification: an edge with make_or_buy == "MAKE" becomes a work order.
    Everything else (explicit "BUY", or unclassified -- e.g. an injected
    manufacturing item with no MAKE_OR_BUY rule covering its part type) is
    treated as purchased, since "no rule says we build it" defaults to "buy
    it" in practice.
    """
    po_lines_by_supplier = {}
    work_orders = []
    covered_by_stock = []

    for edge in rollup:
        part_id = edge["part_id"]
        total_qty = edge["total_qty"]
        make_or_buy = edge.get("make_or_buy") or "BUY"

        if make_or_buy == "MAKE":
            work_orders.append({
                "part_id": part_id, "quantity": total_qty, "work_center": edge.get("work_center"),
            })
            continue

        inv = inventory_by_part_id.get(part_id, {})
        on_hand = inv.get("on_hand_qty", 0)
        net_required = total_qty - on_hand
        if net_required <= 0:
            covered_by_stock.append(part_id)
            continue

        supplier_id = inv.get("preferred_supplier_id") or default_supplier_id
        po_lines_by_supplier.setdefault(supplier_id, []).append({
            "part_id": part_id, "quantity": net_required, "unit_cost": inv.get("unit_cost", 0),
        })

    return {
        "po_lines_by_supplier": po_lines_by_supplier,
        "work_orders": work_orders,
        "covered_by_stock": covered_by_stock,
    }


# ---------------------------------------------------------------------------
# Persistence wrapper: loads a conversion run's mBOM, rolls it up per part,
# runs the pure planner above, and writes PurchaseOrder(+Lines)/WorkOrder rows.
# ---------------------------------------------------------------------------

def generate_erp_documents(run_id, actor="system"):
    from app.models import (
        db, MBomItem, ConversionRun, InventoryItem, Supplier,
        PurchaseOrder, PurchaseOrderLine, WorkOrder, AuditLog,
    )

    run = db.session.get(ConversionRun, run_id)
    if run is None:
        raise ValueError(f"No conversion run #{run_id}")

    # Idempotent: re-clicking "Generate ERP Documents" on the same run should
    # not spawn duplicate POs/work orders (e.g. a re-conversion of the same
    # part). Point the caller at what already exists instead.
    existing_pos = PurchaseOrder.query.filter_by(run_id=run_id).all()
    existing_wos = WorkOrder.query.filter_by(run_id=run_id).all()
    if existing_pos or existing_wos:
        return {"created": False, "purchase_orders": existing_pos, "work_orders": existing_wos}

    items = MBomItem.query.filter_by(run_id=run_id).all()
    rollup_by_part = {}
    for mi in items:
        entry = rollup_by_part.setdefault(mi.child_part_id, {
            "part_id": mi.child_part_id, "total_qty": 0.0,
            "make_or_buy": mi.make_or_buy, "work_center": mi.work_center,
        })
        entry["total_qty"] += mi.extended_quantity

    # A part landing in an mBOM for the first time (freshly imported, or an
    # injected manufacturing item) may not have an item-master record yet --
    # create one lazily with zero stock, the same way a new part number
    # showing up on a BOM feed would create a new ERP item record.
    part_ids = list(rollup_by_part.keys())
    inventory_rows = InventoryItem.query.filter(InventoryItem.part_id.in_(part_ids)).all() if part_ids else []
    inventory_by_part = {inv.part_id: inv for inv in inventory_rows}
    new_item_count = 0
    for part_id in part_ids:
        if part_id not in inventory_by_part:
            inv = InventoryItem(part_id=part_id, on_hand_qty=0, reorder_point=0, unit_cost=0)
            db.session.add(inv)
            inventory_by_part[part_id] = inv
            new_item_count += 1
    db.session.flush()

    default_supplier = Supplier.query.filter_by(name="Unassigned / Direct Buy").first()
    if default_supplier is None:
        default_supplier = Supplier(name="Unassigned / Direct Buy", lead_time_days=21)
        db.session.add(default_supplier)
        db.session.flush()

    inventory_dicts = {
        pid: {
            "on_hand_qty": inv.on_hand_qty, "unit_cost": inv.unit_cost,
            "preferred_supplier_id": inv.preferred_supplier_id,
        }
        for pid, inv in inventory_by_part.items()
    }
    rollup = list(rollup_by_part.values())
    plan = plan_erp_documents(rollup, inventory_dicts, default_supplier.id)

    next_po_num = (db.session.query(db.func.max(PurchaseOrder.id)).scalar() or 0) + 1
    next_wo_num = (db.session.query(db.func.max(WorkOrder.id)).scalar() or 0) + 1

    created_pos = []
    for supplier_id, lines in plan["po_lines_by_supplier"].items():
        po = PurchaseOrder(
            po_number=f"PO-{1000 + next_po_num}", run_id=run_id, supplier_id=supplier_id, created_by=actor,
        )
        db.session.add(po)
        db.session.flush()
        for line in lines:
            db.session.add(PurchaseOrderLine(
                po_id=po.id, part_id=line["part_id"], quantity=line["quantity"], unit_cost=line["unit_cost"],
            ))
        created_pos.append(po)
        next_po_num += 1

    created_wos = []
    for wo_plan in plan["work_orders"]:
        wo = WorkOrder(
            wo_number=f"WO-{1000 + next_wo_num}", run_id=run_id, part_id=wo_plan["part_id"],
            quantity=wo_plan["quantity"], work_center=wo_plan["work_center"], created_by=actor,
        )
        db.session.add(wo)
        created_wos.append(wo)
        next_wo_num += 1

    detail = (
        f"From run #{run_id}: {len(created_pos)} PO(s) across "
        f"{len(plan['po_lines_by_supplier'])} supplier(s), {len(created_wos)} work order(s), "
        f"{len(plan['covered_by_stock'])} line(s) fully covered by stock"
        + (f", {new_item_count} new item master record(s) created" if new_item_count else "")
    )
    db.session.add(AuditLog(entity_type="ConversionRun", entity_id=run_id, action="ERP_GENERATED", actor=actor,
                             detail=detail))
    db.session.commit()
    return {
        "created": True, "purchase_orders": created_pos, "work_orders": created_wos,
        "covered_by_stock": plan["covered_by_stock"],
    }


def receive_purchase_order(po_id, actor="system"):
    """Mark a PO received and add each line's quantity to on-hand stock."""
    from app.models import db, PurchaseOrder, InventoryItem, AuditLog, utcnow

    po = db.session.get(PurchaseOrder, po_id)
    if po is None:
        raise ValueError(f"No purchase order #{po_id}")
    if po.status == "RECEIVED":
        return po

    for line in po.lines:
        inv = InventoryItem.query.filter_by(part_id=line.part_id).first()
        if inv is None:
            inv = InventoryItem(part_id=line.part_id, on_hand_qty=0, reorder_point=0, unit_cost=line.unit_cost)
            db.session.add(inv)
        inv.on_hand_qty += line.quantity

    po.status = "RECEIVED"
    po.received_at = utcnow()
    po.received_by = actor
    db.session.add(AuditLog(entity_type="PurchaseOrder", entity_id=po.id, action="RECEIVED", actor=actor,
                             detail=f"{po.po_number}: {len(po.lines)} line(s) added to stock"))
    db.session.commit()
    return po


def complete_work_order(wo_id, actor="system"):
    """
    Complete a work order: consume its assembly's direct-child component
    quantities (this run's mBOM lines under the work order's part) times the
    work order quantity from inventory, then add the built quantity of the
    assembly itself to stock. Raises ValueError if any component is short,
    so a demo can show a blocked completion, not silently go negative.
    """
    from app.models import db, WorkOrder, MBomItem, InventoryItem, AuditLog, utcnow

    wo = db.session.get(WorkOrder, wo_id)
    if wo is None:
        raise ValueError(f"No work order #{wo_id}")
    if wo.status == "COMPLETE":
        return wo

    component_lines = MBomItem.query.filter_by(run_id=wo.run_id, parent_part_id=wo.part_id).all()
    required = {}  # part_id -> qty needed for this work order
    for line in component_lines:
        required[line.child_part_id] = required.get(line.child_part_id, 0) + line.quantity * wo.quantity

    shortages = []
    inv_by_part = {}
    for part_id, needed in required.items():
        inv = InventoryItem.query.filter_by(part_id=part_id).first()
        have = inv.on_hand_qty if inv else 0
        inv_by_part[part_id] = inv
        if have < needed:
            shortages.append((part_id, needed, have))

    if shortages:
        from app.models import Part
        detail = "; ".join(
            f"{db.session.get(Part, pid).part_number}: need {needed:.2f}, have {have:.2f}"
            for pid, needed, have in shortages
        )
        raise ValueError(f"Insufficient component stock to complete {wo.wo_number}: {detail}")

    for part_id, needed in required.items():
        inv_by_part[part_id].on_hand_qty -= needed

    assy_inv = InventoryItem.query.filter_by(part_id=wo.part_id).first()
    if assy_inv is None:
        assy_inv = InventoryItem(part_id=wo.part_id, on_hand_qty=0, reorder_point=0)
        db.session.add(assy_inv)
    assy_inv.on_hand_qty += wo.quantity

    wo.status = "COMPLETE"
    wo.completed_at = utcnow()
    wo.completed_by = actor
    db.session.add(AuditLog(entity_type="WorkOrder", entity_id=wo.id, action="COMPLETED", actor=actor,
                             detail=f"{wo.wo_number}: built {wo.quantity:g} of part {wo.part_id}, "
                                    f"consumed {len(required)} component(s)"))
    db.session.commit()
    return wo
