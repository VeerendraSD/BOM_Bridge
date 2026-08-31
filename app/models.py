"""
Database schema for BOM Bridge (eBOM -> mBOM conversion platform).

Design notes
------------
- Part / Revision separate "what a part is" from "the history of its changes".
  A Part's `current_revision` always points at the latest approved Revision.
- BomItem is the eBOM: a self-referencing parent/child structure over Parts.
  This is the tree an engineer builds/imports directly from CAD.
- ConversionRule rows drive the eBOM -> mBOM transform (see conversion_engine.py).
  Keeping rules as data (not hardcoded if/else) is the whole point of the engine:
  manufacturing rules change constantly and shouldn't require a redeploy.
- ConversionRun + MBomItem represent ONE execution of the conversion engine, so
  the diff view can compare "the eBOM" against "one specific mBOM snapshot" and
  a part can be re-converted later without losing history.
- ECR (Engineering Change Request) + AuditLog model the fact that, in a real
  PLM system, released data doesn't just get edited -- it changes through an
  approval workflow, and every change is traceable.
- InventoryItem / PurchaseOrder / WorkOrder are the ERP side: the "peripheral
  system" the mBOM API was previously just a JSON stub for. A generated mBOM
  is exploded into real ERP documents (see app/erp_engine.py) -- BUY lines
  become purchase order lines against a preferred supplier, MAKE lines become
  shop work orders -- and those documents move real inventory when a PO is
  received or a work order is completed. This is the PDM/PLM -> ERP handoff,
  modeled as data instead of asserted in a README.
- Customer / SalesOrder are the sales side of the same ERP. A sales order
  names a customer, a top-level assembly, and a quantity; "producing" it
  runs the same conversion + ERP-generation pipeline used elsewhere and
  links the resulting run back to the order, so demand (sales) and supply
  (PDM->mBOM->ERP) are the same data, not two disconnected demos.
"""
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


class User(db.Model):
    """
    Deliberately minimal: no passwords. This is a portfolio demo, so "login"
    is a role picker (see app/auth.py), not real authentication. The point is
    to demonstrate role-gated approval routing, which is a real PLM concern --
    not to build a login system.
    """
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    role = db.Column(db.String(20), nullable=False)  # ENGINEER / MFG_ENGINEER / APPROVER


class Part(db.Model):
    __tablename__ = "parts"

    id = db.Column(db.Integer, primary_key=True)
    part_number = db.Column(db.String(40), unique=True, nullable=False)
    description = db.Column(db.String(200), nullable=False)
    material = db.Column(db.String(100))
    # ASSEMBLY parts have children in BomItem; COMPONENT/RAW parts are leaves.
    part_type = db.Column(db.String(20), nullable=False, default="COMPONENT")
    status = db.Column(db.String(20), nullable=False, default="DRAFT")  # DRAFT / RELEASED / OBSOLETE
    cad_file_ref = db.Column(db.String(200))  # simulated CAD/drawing filename
    created_at = db.Column(db.DateTime, default=utcnow)

    revisions = db.relationship(
        "Revision", back_populates="part", order_by="Revision.created_at",
        cascade="all, delete-orphan",
    )

    @property
    def current_revision(self):
        current = [r for r in self.revisions if r.is_current]
        return current[0] if current else None

    def __repr__(self):
        return f"<Part {self.part_number} rev={self.current_revision.rev_code if self.current_revision else '?'}>"


class Revision(db.Model):
    __tablename__ = "revisions"

    id = db.Column(db.Integer, primary_key=True)
    part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=False)
    rev_code = db.Column(db.String(5), nullable=False)  # A, B, C, ...
    notes = db.Column(db.String(300))
    is_current = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    part = db.relationship("Part", back_populates="revisions")


class BomItem(db.Model):
    """One edge of the eBOM tree: parent contains `quantity` of child."""
    __tablename__ = "bom_items"

    id = db.Column(db.Integer, primary_key=True)
    parent_part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=False)
    child_part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=1)
    reference_designator = db.Column(db.String(50))  # e.g. "RD-12", CAD callout

    parent = db.relationship("Part", foreign_keys=[parent_part_id])
    child = db.relationship("Part", foreign_keys=[child_part_id])


class ConversionRule(db.Model):
    """
    A single manufacturing rule applied during eBOM -> mBOM conversion.

    rule_type is one of:
      ADD_ITEM          - inject a manufacturing-only item under any assembly
                           of a given part_type (e.g. "add packaging under FINISHED_ASSY")
      MAKE_OR_BUY        - classify parts of a given part_type as MAKE or BUY
      SCRAP_FACTOR       - multiply quantity by a yield factor for a part_type
      SUBSTITUTE_PART    - replace a specific part with its approved substitute
      ROUTING            - assign a work center to parts of a given part_type
    """
    __tablename__ = "conversion_rules"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    rule_type = db.Column(db.String(20), nullable=False)
    active = db.Column(db.Boolean, default=True)
    priority = db.Column(db.Integer, default=100)  # lower runs first

    # generic condition: which part_type this rule applies to (nullable = applies to all)
    applies_to_part_type = db.Column(db.String(20))

    # ADD_ITEM params
    add_item_part_id = db.Column(db.Integer, db.ForeignKey("parts.id"))
    add_item_quantity = db.Column(db.Float)

    # MAKE_OR_BUY params
    make_or_buy_value = db.Column(db.String(4))  # "MAKE" or "BUY"

    # SCRAP_FACTOR params
    scrap_factor = db.Column(db.Float)  # e.g. 1.05 = 5% scrap allowance

    # SUBSTITUTE_PART params
    substitute_target_part_id = db.Column(db.Integer, db.ForeignKey("parts.id"))
    substitute_replacement_part_id = db.Column(db.Integer, db.ForeignKey("parts.id"))

    # ROUTING params
    work_center = db.Column(db.String(50))

    add_item_part = db.relationship("Part", foreign_keys=[add_item_part_id])
    substitute_target_part = db.relationship("Part", foreign_keys=[substitute_target_part_id])
    substitute_replacement_part = db.relationship("Part", foreign_keys=[substitute_replacement_part_id])


class Supplier(db.Model):
    __tablename__ = "suppliers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    lead_time_days = db.Column(db.Integer, default=14)
    contact = db.Column(db.String(120))


class InventoryItem(db.Model):
    """
    The ERP "item master" record for a part: on-hand stock, reorder point,
    standard cost, and the preferred supplier to buy it from. Deliberately a
    separate table from Part rather than more columns bolted onto it -- in a
    real integration this data usually *lives* in the ERP system and is keyed
    off the part number, not owned by the PDM system. One row per part that
    the ERP module tracks (created lazily -- see app/erp_engine.py).
    """
    __tablename__ = "inventory_items"

    id = db.Column(db.Integer, primary_key=True)
    part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), unique=True, nullable=False)
    on_hand_qty = db.Column(db.Float, nullable=False, default=0)
    reorder_point = db.Column(db.Float, nullable=False, default=0)
    unit_cost = db.Column(db.Float, default=0)
    uom = db.Column(db.String(10), nullable=False, default="EA")
    preferred_supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"))
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    part = db.relationship("Part", foreign_keys=[part_id])
    preferred_supplier = db.relationship("Supplier", foreign_keys=[preferred_supplier_id])

    @property
    def below_reorder_point(self):
        return self.on_hand_qty < self.reorder_point


class PurchaseOrder(db.Model):
    """
    One PO to one supplier, auto-generated from a conversion run's BUY lines
    (see erp_engine.generate_erp_documents). Receiving it adds stock; nothing
    else does, mirroring how a real ERP treats on-hand quantity as something
    only a receipt transaction (or a physical count) may change.
    """
    __tablename__ = "purchase_orders"

    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(20), unique=True, nullable=False)
    run_id = db.Column(db.Integer, db.ForeignKey("conversion_runs.id"))
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="DRAFT")  # DRAFT / RECEIVED
    created_at = db.Column(db.DateTime, default=utcnow)
    created_by = db.Column(db.String(80), default="system")
    received_at = db.Column(db.DateTime)
    received_by = db.Column(db.String(80))

    run = db.relationship("ConversionRun", foreign_keys=[run_id])
    supplier = db.relationship("Supplier", foreign_keys=[supplier_id])
    lines = db.relationship(
        "PurchaseOrderLine", back_populates="po", cascade="all, delete-orphan", order_by="PurchaseOrderLine.id",
    )

    @property
    def total_cost(self):
        return sum(l.quantity * l.unit_cost for l in self.lines)


class PurchaseOrderLine(db.Model):
    __tablename__ = "purchase_order_lines"

    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"), nullable=False)
    part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    unit_cost = db.Column(db.Float, default=0)

    po = db.relationship("PurchaseOrder", back_populates="lines")
    part = db.relationship("Part", foreign_keys=[part_id])

    @property
    def line_total(self):
        return self.quantity * self.unit_cost


class WorkOrder(db.Model):
    """
    A shop order to build `quantity` of a MAKE part, auto-generated from a
    conversion run. Completing it is the other half of the ERP loop: it
    consumes the assembly's mBOM component quantities from inventory and adds
    the finished quantity of the assembly itself -- so running the numbers
    through actually moves stock, not just labels lines MAKE vs BUY.
    """
    __tablename__ = "work_orders"

    id = db.Column(db.Integer, primary_key=True)
    wo_number = db.Column(db.String(20), unique=True, nullable=False)
    run_id = db.Column(db.Integer, db.ForeignKey("conversion_runs.id"))
    part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=False)  # the assembly being built
    quantity = db.Column(db.Float, nullable=False)
    work_center = db.Column(db.String(50))
    status = db.Column(db.String(20), nullable=False, default="PLANNED")  # PLANNED / RELEASED / COMPLETE
    created_at = db.Column(db.DateTime, default=utcnow)
    created_by = db.Column(db.String(80), default="system")
    completed_at = db.Column(db.DateTime)
    completed_by = db.Column(db.String(80))

    run = db.relationship("ConversionRun", foreign_keys=[run_id])
    part = db.relationship("Part", foreign_keys=[part_id])


class ConversionRun(db.Model):
    """One execution of the conversion engine against a root assembly."""
    __tablename__ = "conversion_runs"

    id = db.Column(db.Integer, primary_key=True)
    root_part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    created_by = db.Column(db.String(80), default="system")

    root_part = db.relationship("Part", foreign_keys=[root_part_id])
    mbom_items = db.relationship(
        "MBomItem", back_populates="run", cascade="all, delete-orphan",
        order_by="MBomItem.id",
    )


class MBomItem(db.Model):
    """One line of a generated manufacturing BOM."""
    __tablename__ = "mbom_items"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey("conversion_runs.id"), nullable=False)
    parent_part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=False)
    child_part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False)  # direct quantity within the immediate parent
    extended_quantity = db.Column(db.Float, nullable=False)  # quantity per 1 unit of the top-level assembly

    make_or_buy = db.Column(db.String(4))
    work_center = db.Column(db.String(50))

    # provenance: did this line come from the eBOM as-is, get injected, or
    # replace an eBOM line via a substitution rule?
    source = db.Column(db.String(20), default="FROM_EBOM")  # FROM_EBOM / INJECTED / SUBSTITUTED
    source_bom_item_id = db.Column(db.Integer, db.ForeignKey("bom_items.id"), nullable=True)
    substituted_from_part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=True)

    run = db.relationship("ConversionRun", back_populates="mbom_items")
    parent = db.relationship("Part", foreign_keys=[parent_part_id])
    child = db.relationship("Part", foreign_keys=[child_part_id])


class Customer(db.Model):
    """
    A minimal CRM record -- just enough to demonstrate a customer data model
    and drive the sales-order analytics dashboard, not a full CRM. `region`
    is DOMESTIC/OVERSEAS deliberately: this project is written for a company
    whose IT strategy is about connecting domestic and overseas locations, so
    even a toy CRM should be able to answer "how much of our order book is
    overseas" instead of pretending every customer is in one place.
    """
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    region = db.Column(db.String(10), nullable=False, default="DOMESTIC")  # DOMESTIC / OVERSEAS
    country = db.Column(db.String(80))
    contact_email = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=utcnow)


class SalesOrder(db.Model):
    """
    A customer's order for a quantity of a top-level assembly. "Producing" an
    order (see app/routes.py) runs the same conversion-engine + erp_engine
    pipeline used everywhere else in the app and links the resulting run back
    here -- so a sales order isn't a disconnected CRM record, it's the demand
    side of the same PDM -> mBOM -> ERP pipeline the rest of the app builds.

    Simplification, called out in the README: the linked conversion run's
    quantities are always "per 1 unit of the top-level assembly" (the engine
    doesn't scale by order quantity) -- `quantity` here is a demand signal for
    the analytics dashboard, not yet fed back into rescaling the generated
    purchase/work order quantities.
    """
    __tablename__ = "sales_orders"

    id = db.Column(db.Integer, primary_key=True)
    so_number = db.Column(db.String(20), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=1)
    status = db.Column(db.String(20), nullable=False, default="NEW")  # NEW / IN_PRODUCTION / FULFILLED / CANCELLED
    promised_date = db.Column(db.Date)
    run_id = db.Column(db.Integer, db.ForeignKey("conversion_runs.id"))
    created_at = db.Column(db.DateTime, default=utcnow)
    created_by = db.Column(db.String(80), default="system")
    fulfilled_at = db.Column(db.DateTime)
    fulfilled_by = db.Column(db.String(80))

    customer = db.relationship("Customer", foreign_keys=[customer_id])
    part = db.relationship("Part", foreign_keys=[part_id])
    run = db.relationship("ConversionRun", foreign_keys=[run_id])


class ECR(db.Model):
    """Engineering Change Request: the only sanctioned way a released part changes."""
    __tablename__ = "ecrs"

    id = db.Column(db.Integer, primary_key=True)
    part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=False)
    description = db.Column(db.String(500), nullable=False)
    requested_by = db.Column(db.String(80), nullable=False)
    status = db.Column(db.String(20), default="PENDING")  # PENDING / APPROVED / REJECTED
    created_at = db.Column(db.DateTime, default=utcnow)
    approved_by = db.Column(db.String(80))
    approved_at = db.Column(db.DateTime)

    part = db.relationship("Part", foreign_keys=[part_id])


class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    entity_type = db.Column(db.String(40), nullable=False)  # "Part", "ECR", "ConversionRun", ...
    entity_id = db.Column(db.Integer, nullable=False)
    action = db.Column(db.String(40), nullable=False)  # "CREATED", "APPROVED", "CONVERTED", ...
    actor = db.Column(db.String(80), default="system")
    detail = db.Column(db.String(400))
    timestamp = db.Column(db.DateTime, default=utcnow)
