"""
Seed the database with a realistic sample eBOM (a CNC spindle unit) and a
set of conversion rules that exercise every rule type the engine supports.

Run: python seed.py
"""
from app import create_app
from app.models import db, Part, Revision, BomItem, ConversionRule, ECR, User, Supplier, InventoryItem, \
    Customer, SalesOrder
from app.auth import ENGINEER, MFG_ENGINEER, APPROVER
from app.conversion_engine import run_conversion_for_part
from app.erp_engine import generate_erp_documents


def make_part(part_number, description, part_type, material=None, status="RELEASED", rev="A"):
    part = Part(part_number=part_number, description=description, part_type=part_type,
                material=material, status=status, cad_file_ref=f"{part_number}.sldprt")
    db.session.add(part)
    db.session.flush()
    db.session.add(Revision(part_id=part.id, rev_code=rev, notes="Initial release", is_current=True))
    return part


def link(parent, child, quantity, ref=None):
    db.session.add(BomItem(parent_part_id=parent.id, child_part_id=child.id,
                            quantity=quantity, reference_designator=ref))


def main():
    app = create_app()
    with app.app_context():
        db.drop_all()
        db.create_all()

        # --- Users: demo the role-gated approval workflow ----------------------
        db.session.add_all([
            User(name="Priya Nair", role=ENGINEER),
            User(name="Ravi Shah", role=MFG_ENGINEER),
            User(name="Vikram Desai", role=APPROVER),
        ])

        # --- Parts: the eBOM tree -------------------------------------------------
        spindle = make_part("SPINDLE-ASSY-001", "CNC Spindle Unit", "ASSEMBLY")

        housing_assy = make_part("HOUSING-ASSY-001", "Spindle Housing Assembly", "SUBASSEMBLY")
        housing = make_part("HOUSING-001", "Cast Iron Housing", "COMPONENT", material="Cast Iron")
        bearing = make_part("BEARING-001", "Precision Ball Bearing", "COMPONENT", material="Chromium Steel")
        bearing_alt = make_part("BEARING-001-ALT", "Precision Ball Bearing (Alt. Supplier)", "COMPONENT",
                                 material="Chromium Steel")

        motor_assy = make_part("MOTOR-ASSY-001", "Drive Motor Assembly", "SUBASSEMBLY")
        motor = make_part("MOTOR-001", "AC Servo Motor", "COMPONENT")
        coupling = make_part("COUPLING-001", "Flexible Shaft Coupling", "COMPONENT")

        shaft = make_part("SHAFT-001", "Precision Ground Shaft", "COMPONENT", material="Alloy Steel")
        encoder = make_part("SENSOR-001", "Rotary Encoder", "COMPONENT")

        # a genuine multi-level fan-out: 4 bracket assemblies per spindle, each
        # needing its own hardware -- this is what makes extended_quantity
        # (procurement's "total needed", not just the drawing-level quantity)
        # actually differ from the direct eBOM quantity
        bracket_assy = make_part("BRACKET-ASSY-001", "Mounting Bracket Assembly", "SUBASSEMBLY")
        bolt = make_part("BOLT-001", "M8 Hex Bolt", "COMPONENT", material="Steel")
        bushing = make_part("BUSHING-001", "Vibration Damping Bushing", "COMPONENT", material="Rubber")

        # manufacturing-only items -- these exist ONLY on the mBOM side, injected by rules
        packaging = make_part("PACKAGING-001", "Export Packaging Kit", "MFG_ITEM")
        fastener_kit = make_part("FASTENER-KIT-001", "Hardware Fastener Kit", "MFG_ITEM")

        db.session.flush()

        # --- eBOM structure ---------------------------------------------------
        link(spindle, housing_assy, 1, "A1")
        link(spindle, motor_assy, 1, "A2")
        link(spindle, shaft, 1, "A3")
        link(spindle, encoder, 1, "A4")
        link(spindle, bracket_assy, 4, "A5")  # 4 brackets per spindle unit

        link(housing_assy, housing, 1, "H1")
        link(housing_assy, bearing, 2, "H2")

        link(bracket_assy, bolt, 2, "B1")     # -> 4 x 2 = 8 bolts needed per spindle unit
        link(bracket_assy, bushing, 1, "B2")  # -> 4 x 1 = 4 bushings needed per spindle unit

        link(motor_assy, motor, 1, "M1")
        link(motor_assy, coupling, 1, "M2")

        # --- Conversion rules: one of each rule type ---------------------------
        db.session.add_all([
            ConversionRule(
                name="Purchased components are BUY", rule_type="MAKE_OR_BUY",
                applies_to_part_type="COMPONENT", make_or_buy_value="BUY", priority=10,
            ),
            ConversionRule(
                name="Sub-assemblies are built in-house", rule_type="MAKE_OR_BUY",
                applies_to_part_type="SUBASSEMBLY", make_or_buy_value="MAKE", priority=10,
            ),
            ConversionRule(
                name="5% scrap allowance on purchased components", rule_type="SCRAP_FACTOR",
                applies_to_part_type="COMPONENT", scrap_factor=1.05, priority=20,
            ),
            ConversionRule(
                name="Components route through receiving inspection", rule_type="ROUTING",
                applies_to_part_type="COMPONENT", work_center="RECEIVING-INSPECTION", priority=30,
            ),
            ConversionRule(
                name="Sub-assemblies route through Assembly Line 2", rule_type="ROUTING",
                applies_to_part_type="SUBASSEMBLY", work_center="ASSY-LINE-2", priority=30,
            ),
            ConversionRule(
                name="Approved alternate bearing supplier", rule_type="SUBSTITUTE_PART",
                substitute_target_part_id=bearing.id, substitute_replacement_part_id=bearing_alt.id, priority=5,
            ),
            ConversionRule(
                name="Add export packaging to top-level assemblies", rule_type="ADD_ITEM",
                applies_to_part_type="ASSEMBLY", add_item_part_id=packaging.id, add_item_quantity=1, priority=40,
            ),
            ConversionRule(
                name="Add fastener kit to sub-assemblies", rule_type="ADD_ITEM",
                applies_to_part_type="SUBASSEMBLY", add_item_part_id=fastener_kit.id, add_item_quantity=1,
                priority=40,
            ),
        ])

        # --- ERP master data: suppliers + starting inventory -------------------
        # Deliberately mixed so the ERP module demonstrates all three cases once
        # a conversion is run: some parts are already below reorder point (visible
        # immediately on the ERP dashboard), some have enough on-hand stock to
        # fully cover what the mBOM needs (no PO line generated), and some are
        # short and will generate a purchase order. `packaging`/`fastener_kit`
        # are deliberately left with no inventory record at all, to demonstrate
        # the ERP engine lazily creating a new item-master record the first time
        # a manufacturing-injected item shows up on a run.
        sup_bearings = Supplier(name="Precision Bearings Co.", lead_time_days=10,
                                 contact="orders@precisionbearings.example")
        sup_motion = Supplier(name="Motion Systems Inc.", lead_time_days=21,
                               contact="sales@motionsystems.example")
        sup_metal = Supplier(name="Ironclad Metal Works", lead_time_days=14,
                              contact="procurement@ironcladmetal.example")
        sup_fastener = Supplier(name="Fastener Direct", lead_time_days=5,
                                 contact="orders@fastenerdirect.example")
        db.session.add_all([sup_bearings, sup_motion, sup_metal, sup_fastener])
        db.session.flush()

        db.session.add_all([
            InventoryItem(part_id=housing.id, on_hand_qty=2, reorder_point=5, unit_cost=45.00,
                          preferred_supplier_id=sup_metal.id),
            InventoryItem(part_id=bearing.id, on_hand_qty=50, reorder_point=5, unit_cost=11.00,
                          preferred_supplier_id=sup_bearings.id),
            InventoryItem(part_id=bearing_alt.id, on_hand_qty=1, reorder_point=10, unit_cost=12.50,
                          preferred_supplier_id=sup_bearings.id),
            InventoryItem(part_id=motor.id, on_hand_qty=3, reorder_point=2, unit_cost=220.00,
                          preferred_supplier_id=sup_motion.id),
            InventoryItem(part_id=coupling.id, on_hand_qty=0, reorder_point=5, unit_cost=18.00,
                          preferred_supplier_id=sup_motion.id),
            InventoryItem(part_id=shaft.id, on_hand_qty=4, reorder_point=3, unit_cost=65.00,
                          preferred_supplier_id=sup_metal.id),
            InventoryItem(part_id=encoder.id, on_hand_qty=1, reorder_point=3, unit_cost=95.00,
                          preferred_supplier_id=sup_motion.id),
            InventoryItem(part_id=bolt.id, on_hand_qty=20, reorder_point=50, unit_cost=0.35,
                          preferred_supplier_id=sup_fastener.id),
            InventoryItem(part_id=bushing.id, on_hand_qty=2, reorder_point=10, unit_cost=3.20,
                          preferred_supplier_id=sup_fastener.id),
        ])

        # --- a sample pending ECR, so the workflow has something to demo -------
        db.session.add(ECR(
            part_id=housing.id,
            description="Switch housing material from cast iron to ductile iron for improved vibration damping",
            requested_by="Priya Nair",
        ))

        # --- CRM: a small mixed domestic/overseas order book -------------------
        acme = Customer(name="Acme Precision Tools", region="DOMESTIC", country="Japan",
                         contact_email="procurement@acme-precision.example")
        globex = Customer(name="Globex Machining Ltd.", region="OVERSEAS", country="Vietnam",
                           contact_email="buyer@globex-machining.example")
        northwind = Customer(name="Northwind Manufacturing", region="OVERSEAS", country="Germany",
                              contact_email="orders@northwind-mfg.example")
        db.session.add_all([acme, globex, northwind])
        db.session.flush()

        db.session.add_all([
            SalesOrder(so_number="SO-2001", customer_id=acme.id, part_id=spindle.id, quantity=2,
                       created_by="Priya Nair"),
            SalesOrder(so_number="SO-2002", customer_id=globex.id, part_id=spindle.id, quantity=5,
                       created_by="Ravi Shah"),
            SalesOrder(so_number="SO-2003", customer_id=northwind.id, part_id=motor_assy.id, quantity=3,
                       created_by="Priya Nair"),
        ])

        db.session.commit()

        # SO-2001 is produced end-to-end (conversion + ERP documents) right in
        # the seed, so the ERP/CRM dashboards have real linked data on first
        # load, not just an empty "click produce" demo.
        run = run_conversion_for_part(spindle.id, actor="Priya Nair")
        generate_erp_documents(run.id, actor="Priya Nair")
        so_2001 = SalesOrder.query.filter_by(so_number="SO-2001").first()
        so_2001.run_id = run.id
        so_2001.status = "IN_PRODUCTION"
        print(f"Seeded {User.query.count()} users, {Part.query.count()} parts, "
              f"{BomItem.query.count()} eBOM edges, {ConversionRule.query.count()} conversion rules, "
              f"{Supplier.query.count()} suppliers, {InventoryItem.query.count()} inventory records, "
              f"{Customer.query.count()} customers, {SalesOrder.query.count()} sales orders.")
        print("Log in as Priya Nair (Engineer), Ravi Shah (Mfg Engineer), or Vikram Desai (Approver).")
        print(f"Try: python run.py, then open the '{spindle.part_number}' part and click 'Convert to mBOM',")
        print("then 'Generate ERP documents' on the diff view to see purchase & work orders appear under ERP.")
        print("Or open CRM and click 'Produce' on SO-2002/SO-2003 to run the same pipeline from a sales order.")
        print("A sample_data/motor_v2_import.csv is included to try the eBOM CSV import feature.")


if __name__ == "__main__":
    main()
