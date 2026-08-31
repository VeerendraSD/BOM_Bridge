"""
Tests for the security hardening in app/__init__.py: CSRF protection is
actually enforced (not just wired up and never exercised), and the baseline
response headers are present. Unlike the other route tests, this fixture
deliberately leaves CSRF *enabled* -- the whole point is to prove a request
forged without a token is rejected, and one carrying the real token succeeds.
"""
import os
import tempfile
import re

import pytest

from app import create_app
from app.auth import ENGINEER
from app.models import db, User


@pytest.fixture
def app():
    fd, db_file = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    flask_app = create_app(db_path=db_file)
    flask_app.config.update(TESTING=True)  # CSRF stays enabled -- that's what this file tests

    with flask_app.app_context():
        db.create_all()
        engineer = User(name="Test Engineer", role=ENGINEER)
        db.session.add(engineer)
        db.session.commit()
        flask_app.test_ids = {"engineer": engineer.id}
        yield flask_app
        db.session.remove()
        db.drop_all()
        db.engine.dispose()

    os.remove(db_file)


@pytest.fixture
def client(app):
    return app.test_client()


def test_post_without_csrf_token_is_rejected(client, app):
    resp = client.post("/login", data={"user_id": app.test_ids["engineer"]})
    assert resp.status_code == 400


def test_post_with_valid_csrf_token_succeeds(client, app):
    # A real page load carries the token in a hidden field -- pull it from
    # the login form the same way a browser would.
    page = client.get("/login")
    match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert match, "login page should render a csrf_field()"
    token = match.group(1).decode()

    resp = client.post("/login", data={"user_id": app.test_ids["engineer"], "csrf_token": token})
    assert resp.status_code == 302


def test_security_headers_present(client):
    resp = client.get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
