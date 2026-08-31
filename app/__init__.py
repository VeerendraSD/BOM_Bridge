import os

from flask import Flask
from flask_wtf import CSRFProtect
from flask_wtf.csrf import generate_csrf
from markupsafe import Markup

from app.models import db

csrf = CSRFProtect()


def create_app(db_path="bombridge.db"):
    app = Flask(__name__)
    # DATABASE_URL lets a deployment point at Postgres (e.g. Render's free tier)
    # instead of the default SQLite file, which doesn't survive a redeploy on
    # most hosts with ephemeral disks -- no code change needed, just the env var
    # (plus adding psycopg[binary] to requirements.txt). Render/Heroku-style
    # URLs use the old "postgres://" scheme; SQLAlchemy 1.4+ requires "postgresql://".
    database_url = os.environ.get("DATABASE_URL")
    if database_url and database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url or f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    # SECRET_KEY only signs the session cookie that remembers which demo user
    # is "logged in" -- there's no real auth here (see app/auth.py), so a
    # dev default is fine locally; set a real SECRET_KEY env var in production.
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-not-for-production")

    db.init_app(app)

    # CSRF protection on every state-changing route.
    # This is a real, load-bearing control, not a checkbox: every POST route
    # in app/routes.py relies on the session cookie alone to know who's
    # acting, so without it any page a logged-in user visits could silently
    # submit approvals/receipts/etc. on their behalf. The GenAI-drafting
    # endpoint is exempted below because it performs no state change -- it
    # only calls an LLM and returns text, so forging a request to it costs
    # API spend at worst, not data integrity.
    csrf.init_app(app)

    from app.routes import bp as main_bp
    app.register_blueprint(main_bp)
    from app.routes import ecr_draft
    csrf.exempt(ecr_draft)

    @app.context_processor
    def inject_csrf_field():
        # `csrf_field()` renders a ready-to-use hidden <input>, so templates
        # don't each have to know the field name Flask-WTF expects.
        def csrf_field():
            return Markup(f'<input type="hidden" name="csrf_token" value="{generate_csrf()}">')
        return {"csrf_field": csrf_field}

    @app.after_request
    def set_security_headers(response):
        # Baseline hardening headers -- cheap, standard, and worth having on
        # any server-rendered app: block MIME-sniffing, block being framed
        # (clickjacking), and don't leak the full referrer cross-origin. CSP
        # is scoped to what this app actually loads (self + the Bootstrap CDN
        # for CSS, plus 'unsafe-inline' for the small inline <script> on the
        # ECR page) rather than left wide open.
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data:"
        )
        return response

    return app
