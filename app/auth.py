"""
Minimal role-gated "auth". There are no passwords -- you pick a seeded user
from a dropdown and the session remembers who you are. The point isn't to
build a login system; it's to demonstrate that approval routing in a PLM
workflow actually enforces something (an Engineer cannot approve their own
ECR, only an Approver can).
"""
from functools import wraps

from flask import session, redirect, url_for, flash

ENGINEER = "ENGINEER"
MFG_ENGINEER = "MFG_ENGINEER"
APPROVER = "APPROVER"

ROLE_LABELS = {
    ENGINEER: "Engineer",
    MFG_ENGINEER: "Manufacturing Engineer",
    APPROVER: "Approver",
}


def current_user():
    from app.models import db, User
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.session.get(User, user_id)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            flash("Pick a user to continue.", "warning")
            return redirect(url_for("main.login", next=None))
        return view(*args, **kwargs)
    return wrapped


def role_required(*allowed_roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                flash("Pick a user to continue.", "warning")
                return redirect(url_for("main.login"))
            if user.role not in allowed_roles:
                labels = ", ".join(ROLE_LABELS[r] for r in allowed_roles)
                flash(f"That action requires role: {labels}. You are logged in as "
                      f"{user.name} ({ROLE_LABELS[user.role]}).", "danger")
                return redirect(url_for("main.dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorator
