"""
Integration tests over HTTP via Flask's test client: login/role gating, the
conversion route, ECR approval, and CSV import. These exercise the app the
way a browser actually would, on top of the pure-function tests in
test_conversion_engine.py.
"""
import io
import os
import tempfile

import pytest

from app import create_app
from app.auth import ENGINEER, MFG_ENGINEER, APPROVER
from app.models import db, Part, Revision, BomItem, ConversionRule, ECR, User


@pytest.fixture
def app():
    fd, db_file = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    flask_app = create_app(db_path=db_file)
    # CSRF is real (see app/__init__.py) but these tests post form data
    # directly without a browser session carrying the token.
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
        db.session.add(ECR(part_id=assy.id, description="Test change", requested_by=engineer.name))
        db.session.commit()

        # stash ids the tests need, since objects go stale outside this context
        flask_app.test_ids = {
            "assy": assy.id, "comp": comp.id,
            "engineer": engineer.id, "mfg": mfg.id, "approver": approver.id,
            "ecr": ECR.query.first().id,
        }

        yield flask_app

        db.session.remove()
        db.drop_all()
        db.engine.dispose()  # release the sqlite file handle -- required on Windows before os.remove

    os.remove(db_file)


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, user_id):
    return client.post("/login", data={"user_id": user_id}, follow_redirects=False)


def test_dashboard_loads_without_login(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"ASSY-1" in resp.data


def test_convert_requires_login(client, app):
    resp = client.post(f"/parts/{app.test_ids['assy']}/convert")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_convert_succeeds_as_engineer(client, app):
    login(client, app.test_ids["engineer"])
    resp = client.post(f"/parts/{app.test_ids['assy']}/convert")
    assert resp.status_code == 302
    assert "/diff" in resp.headers["Location"]


def test_engineer_cannot_approve_ecr(client, app):
    login(client, app.test_ids["engineer"])
    resp = client.post(f"/ecrs/{app.test_ids['ecr']}/approve", follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        ecr = db.session.get(ECR, app.test_ids["ecr"])
        assert ecr.status == "PENDING"  # unchanged -- role check blocked it


def test_approver_can_approve_ecr_and_bumps_revision(client, app):
    login(client, app.test_ids["approver"])
    resp = client.post(f"/ecrs/{app.test_ids['ecr']}/approve")
    assert resp.status_code == 302
    with app.app_context():
        ecr = db.session.get(ECR, app.test_ids["ecr"])
        assert ecr.status == "APPROVED"
        part = db.session.get(Part, app.test_ids["assy"])
        assert part.current_revision.rev_code == "B"


def test_csv_import_creates_new_parts_and_links(client, app):
    login(client, app.test_ids["mfg"])
    csv_content = (
        "child_part_number,child_description,child_part_type,quantity,reference_designator\n"
        "NEW-PART-1,Newly Imported Part,COMPONENT,3,R1\n"
    )
    data = {"file": (io.BytesIO(csv_content.encode("utf-8")), "import.csv")}
    resp = client.post(
        f"/parts/{app.test_ids['assy']}/import-ebom",
        data=data, content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    with app.app_context():
        new_part = Part.query.filter_by(part_number="NEW-PART-1").first()
        assert new_part is not None
        link = BomItem.query.filter_by(parent_part_id=app.test_ids["assy"], child_part_id=new_part.id).first()
        assert link is not None
        assert link.quantity == 3
