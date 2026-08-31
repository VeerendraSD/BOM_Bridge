"""
Integration tests for the ERP module over HTTP: generating purchase/work
orders from a conversion run, receiving a PO, and completing a work order.
Same fixture style as test_routes.py.
"""
import os
import tempfile

import pytest

from app import create_app
from app.auth import ENGINEER, MFG_ENGINEER, APPROVER
from app.models import (
    db, Part, Revision, BomItem, ConversionRule, User,
    ConversionRun, PurchaseOrder, WorkOrder, InventoryItem,
)
from app.conversion_engine import run_conversion_for_part


@pytest.fixture
def app():
    fd, db_file = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    flask_app = create_app(db_path=db_file)
    # WTF_CSRF_ENABLED=False: these tests post form data directly without a
    # browser session to carry the CSRF token: real usage is protected (see
    # app/__init__.py), this fixture just isn't simulating a full page load.
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with flask_app.app_context():
        db.create_all()

        engineer = User(name="Test Engineer", role=ENGINEER)
        mfg = User(name="Test MfgEng", role=MFG_ENGINEER)
        approver = User(name="Test Approver", role=APPROVER)
        db.session.add_all([engineer, mfg, approver])

        # ASSY -> SUB (MAKE, x1) -> COMP (BUY, x2)
        assy = Part(part_number="ASSY-1", description="Test Assembly", part_type="ASSEMBLY", status="RELEASED")
        sub = Part(part_number="SUB-1", description="Test Subassembly", part_type="SUBASSEMBLY", status="RELEASED")
        comp = Part(part_number="COMP-1", description="Test Component", part_type="COMPONENT", status="RELEASED")
        db.session.add_all([assy, sub, comp])
        db.session.flush()
        db.session.add_all([
            Revision(part_id=assy.id, rev_code="A"),
            Revision(part_id=sub.id, rev_code="A"),
            Revision(part_id=comp.id, rev_code="A"),
        ])
        db.session.add(BomItem(parent_part_id=assy.id, child_part_id=sub.id, quantity=1))
        db.session.add(BomItem(parent_part_id=sub.id, child_part_id=comp.id, quantity=2))
        db.session.add_all([
            ConversionRule(name="Subassemblies are MAKE", rule_type="MAKE_OR_BUY",
                            applies_to_part_type="SUBASSEMBLY", make_or_buy_value="MAKE"),
            ConversionRule(name="Components are BUY", rule_type="MAKE_OR_BUY",
                            applies_to_part_type="COMPONENT", make_or_buy_value="BUY"),
        ])
        db.session.commit()

        run = run_conversion_for_part(assy.id, actor="Test Engineer")

        flask_app.test_ids = {
            "assy": assy.id, "sub": sub.id, "comp": comp.id, "run": run.id,
            "engineer": engineer.id, "mfg": mfg.id, "approver": approver.id,
        }

        yield flask_app

        db.session.remove()
        db.drop_all()
        db.engine.dispose()

    os.remove(db_file)


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, user_id):
    return client.post("/login", data={"user_id": user_id}, follow_redirects=False)


def test_generate_erp_requires_login(client, app):
    resp = client.post(f"/runs/{app.test_ids['run']}/generate-erp")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_generate_erp_creates_po_for_buy_and_wo_for_make(client, app):
    login(client, app.test_ids["engineer"])
    resp = client.post(f"/runs/{app.test_ids['run']}/generate-erp")
    assert resp.status_code == 302
    with app.app_context():
        pos = PurchaseOrder.query.filter_by(run_id=app.test_ids["run"]).all()
        wos = WorkOrder.query.filter_by(run_id=app.test_ids["run"]).all()
        assert len(pos) == 1
        assert len(pos[0].lines) == 1
        assert pos[0].lines[0].part_id == app.test_ids["comp"]
        assert pos[0].lines[0].quantity == 2  # no prior stock -> full extended quantity
        assert len(wos) == 1
        assert wos[0].part_id == app.test_ids["sub"]
        assert wos[0].quantity == 1


def test_generate_erp_is_idempotent(client, app):
    login(client, app.test_ids["engineer"])
    client.post(f"/runs/{app.test_ids['run']}/generate-erp")
    client.post(f"/runs/{app.test_ids['run']}/generate-erp")
    with app.app_context():
        assert PurchaseOrder.query.filter_by(run_id=app.test_ids["run"]).count() == 1
        assert WorkOrder.query.filter_by(run_id=app.test_ids["run"]).count() == 1


def test_receiving_po_adds_to_stock(client, app):
    login(client, app.test_ids["engineer"])
    client.post(f"/runs/{app.test_ids['run']}/generate-erp")
    with app.app_context():
        po_id = PurchaseOrder.query.filter_by(run_id=app.test_ids["run"]).first().id

    login(client, app.test_ids["mfg"])
    resp = client.post(f"/erp/po/{po_id}/receive")
    assert resp.status_code == 302
    with app.app_context():
        po = db.session.get(PurchaseOrder, po_id)
        assert po.status == "RECEIVED"
        inv = InventoryItem.query.filter_by(part_id=app.test_ids["comp"]).first()
        assert inv.on_hand_qty == 2


def test_complete_work_order_fails_without_enough_component_stock(client, app):
    login(client, app.test_ids["engineer"])
    client.post(f"/runs/{app.test_ids['run']}/generate-erp")
    with app.app_context():
        wo_id = WorkOrder.query.filter_by(run_id=app.test_ids["run"]).first().id

    login(client, app.test_ids["mfg"])
    resp = client.post(f"/erp/wo/{wo_id}/complete", follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        wo = db.session.get(WorkOrder, wo_id)
        assert wo.status == "PLANNED"  # blocked -- no component stock yet


def test_complete_work_order_consumes_components_and_adds_finished_stock(client, app):
    login(client, app.test_ids["engineer"])
    client.post(f"/runs/{app.test_ids['run']}/generate-erp")
    with app.app_context():
        po_id = PurchaseOrder.query.filter_by(run_id=app.test_ids["run"]).first().id
        wo_id = WorkOrder.query.filter_by(run_id=app.test_ids["run"]).first().id

    login(client, app.test_ids["mfg"])
    client.post(f"/erp/po/{po_id}/receive")  # stocks the 2 components the work order needs
    resp = client.post(f"/erp/wo/{wo_id}/complete")
    assert resp.status_code == 302
    with app.app_context():
        wo = db.session.get(WorkOrder, wo_id)
        assert wo.status == "COMPLETE"
        comp_inv = InventoryItem.query.filter_by(part_id=app.test_ids["comp"]).first()
        assert comp_inv.on_hand_qty == 0  # 2 consumed
        sub_inv = InventoryItem.query.filter_by(part_id=app.test_ids["sub"]).first()
        assert sub_inv.on_hand_qty == 1  # 1 built


def test_engineer_cannot_receive_po_or_complete_wo(client, app):
    login(client, app.test_ids["engineer"])
    client.post(f"/runs/{app.test_ids['run']}/generate-erp")
    with app.app_context():
        po_id = PurchaseOrder.query.filter_by(run_id=app.test_ids["run"]).first().id
        wo_id = WorkOrder.query.filter_by(run_id=app.test_ids["run"]).first().id

    resp = client.post(f"/erp/po/{po_id}/receive", follow_redirects=True)
    assert resp.status_code == 200
    resp = client.post(f"/erp/wo/{wo_id}/complete", follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(PurchaseOrder, po_id).status == "DRAFT"
        assert db.session.get(WorkOrder, wo_id).status == "PLANNED"
