"""
Integration tests for the CRM / sales-order module: creating a customer and
a sales order, "producing" one (which runs the real conversion + ERP
pipeline), marking it fulfilled, and role gating on both transitions.
"""
import os
import tempfile

import pytest

from app import create_app
from app.auth import ENGINEER, MFG_ENGINEER, APPROVER
from app.models import db, Part, Revision, BomItem, ConversionRule, User, Customer, SalesOrder, PurchaseOrder, WorkOrder


@pytest.fixture
def app():
    fd, db_file = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    flask_app = create_app(db_path=db_file)
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with flask_app.app_context():
        db.create_all()

        engineer = User(name="Test Engineer", role=ENGINEER)
        mfg = User(name="Test MfgEng", role=MFG_ENGINEER)
        approver = User(name="Test Approver", role=APPROVER)
        db.session.add_all([engineer, mfg, approver])

        assy = Part(part_number="ASSY-1", description="Test Assembly", part_type="ASSEMBLY", status="RELEASED")
        comp = Part(part_number="COMP-1", description="Test Component", part_type="COMPONENT", status="RELEASED")
        db.session.add_all([assy, comp])
        db.session.flush()
        db.session.add_all([
            Revision(part_id=assy.id, rev_code="A"),
            Revision(part_id=comp.id, rev_code="A"),
        ])
        db.session.add(BomItem(parent_part_id=assy.id, child_part_id=comp.id, quantity=2))
        db.session.add(ConversionRule(
            name="Components are BUY", rule_type="MAKE_OR_BUY",
            applies_to_part_type="COMPONENT", make_or_buy_value="BUY",
        ))
        customer = Customer(name="Acme Overseas", region="OVERSEAS", country="Vietnam")
        db.session.add(customer)
        db.session.commit()

        flask_app.test_ids = {
            "assy": assy.id, "comp": comp.id, "customer": customer.id,
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
    return client.post("/login", data={"user_id": user_id})


def create_order(client, app):
    resp = client.post("/sales-orders", data={
        "customer_id": app.test_ids["customer"], "part_id": app.test_ids["assy"], "quantity": 3,
    })
    order_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    return order_id


def test_crm_dashboard_loads_without_login(client):
    resp = client.get("/crm")
    assert resp.status_code == 200
    assert b"Acme Overseas" in resp.data


def test_creating_customer_and_order_requires_login(client, app):
    resp = client.post("/customers", data={"name": "New Co", "region": "DOMESTIC"})
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_create_sales_order_as_logged_in_user(client, app):
    login(client, app.test_ids["engineer"])
    order_id = create_order(client, app)
    with app.app_context():
        order = db.session.get(SalesOrder, order_id)
        assert order.status == "NEW"
        assert order.quantity == 3
        assert order.customer_id == app.test_ids["customer"]


def test_producing_order_runs_conversion_and_generates_erp_documents(client, app):
    login(client, app.test_ids["engineer"])
    order_id = create_order(client, app)

    resp = client.post(f"/sales-orders/{order_id}/produce")
    assert resp.status_code == 302
    with app.app_context():
        order = db.session.get(SalesOrder, order_id)
        assert order.status == "IN_PRODUCTION"
        assert order.run_id is not None
        # a BUY component with no rule-based work order means at least one PO exists
        assert PurchaseOrder.query.filter_by(run_id=order.run_id).count() == 1


def test_engineer_cannot_fulfill_order(client, app):
    login(client, app.test_ids["engineer"])
    order_id = create_order(client, app)
    client.post(f"/sales-orders/{order_id}/produce")

    resp = client.post(f"/sales-orders/{order_id}/fulfill", follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(SalesOrder, order_id).status == "IN_PRODUCTION"  # unchanged


def test_mfg_engineer_can_fulfill_order(client, app):
    login(client, app.test_ids["engineer"])
    order_id = create_order(client, app)
    client.post(f"/sales-orders/{order_id}/produce")

    login(client, app.test_ids["mfg"])
    resp = client.post(f"/sales-orders/{order_id}/fulfill")
    assert resp.status_code == 302
    with app.app_context():
        order = db.session.get(SalesOrder, order_id)
        assert order.status == "FULFILLED"
        assert order.fulfilled_by == "Test MfgEng"
