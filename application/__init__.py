"""bktodo Flask application factory."""
import os

from dotenv import load_dotenv
from flask import Flask
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from mongoengine import connect, disconnect
from werkzeug.middleware.proxy_fix import ProxyFix

# Public app version — surfaced in the nav footer + the About page.
APP_VERSION = "1.4.534"

# Pull settings out of a local ``.env`` file if one exists. Doesn't override
# values that are already set in the real environment.
load_dotenv()

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"
csrf = CSRFProtect()

# Brute-force protection for the credential endpoints (see views/auth.py and
# views/caldav.py). Storage is Redis so the counters are shared across gunicorn
# workers *and* across containers — an in-process store would give every worker
# its own quota, multiplying the real limit by the worker count.
#
# Keyed on remote_addr, which is the true client IP because ProxyFix rewrites
# it from X-Forwarded-For below. Without that this would key every request to
# the reverse proxy and lock out all users at once.
#
# Default-off (``default_limits`` empty): only the endpoints that explicitly
# opt in are limited, so normal app usage and the REST API are untouched.
limiter = Limiter(key_func=get_remote_address)


def create_app(config_object: str | None = None) -> Flask:
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder="templates",
        static_folder="static",
    )

    app.config.from_object(config_object or _default_config())
    app.config.from_prefixed_env(prefix="BKTODO")

    # Honour reverse-proxy headers (X-Forwarded-Proto / -Host / -For)
    # so ``url_for(..., _external=True)`` builds https:// URLs when the
    # original request came over HTTPS — without this, the Google OAuth
    # redirect_uri ends up http:// and the consent screen rejects it.
    # Tunable via BKTODO_PROXY_FIX (default 1 hop) for chained proxies.
    hops = int(app.config.get("PROXY_FIX", "1") or 0)
    if hops > 0:
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops, x_prefix=hops,
        )

    _init_mongo(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    _init_limiter(app)

    from application.models.user import User

    @login_manager.user_loader
    def load_user(user_id: str):
        return User.objects(pk=user_id).first()

    from application.views.auth import auth_bp
    from application.views.dashboard import dashboard_bp
    from application.views.tasks import tasks_bp
    from application.views.projects import projects_bp
    from application.views.tags import tags_bp
    from application.views.notes import notes_bp
    from application.views.notifications import notifications_bp
    from application.views.review import review_bp
    from application.views.settings import settings_bp
    from application.views.tree import tree_bp
    from application.views.perspectives import perspectives_bp
    from application.views.admin import admin_bp
    from application.views.teams import teams_bp
    from application.views.public_share import public_bp
    from application.views.timetracking import timetracking_bp
    from application.views.trash import trash_bp
    from application.views.scratchpad import scratchpad_bp
    from application.views.capture import capture_bp
    from application.views.focus import focus_bp
    from application.views.favorites import favorites_bp
    from application.views.habits import habits_bp
    from application.views.templates import templates_bp
    from application.views.caldav import caldav_bp
    from application.views.booking import booking_bp
    from application.views.bookings import bookings_bp
    from application.views.billing import billing_bp, webhook as billing_webhook
    from application.api.v1 import api_v1_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(tags_bp)
    app.register_blueprint(notes_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(review_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(tree_bp)
    app.register_blueprint(perspectives_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(teams_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(timetracking_bp)
    app.register_blueprint(trash_bp)
    app.register_blueprint(scratchpad_bp)
    app.register_blueprint(capture_bp)
    app.register_blueprint(focus_bp)
    app.register_blueprint(favorites_bp)
    app.register_blueprint(habits_bp)
    app.register_blueprint(templates_bp)
    app.register_blueprint(caldav_bp)
    app.register_blueprint(booking_bp)
    app.register_blueprint(bookings_bp)
    # The public booking page is filled in by people who have never seen Cairn
    # and carry no session, so a CSRF token would only turn "cookies blocked"
    # into "can't book". A forged POST there achieves one appointment its owner
    # can see and delete, and the rate limit is what holds that down.
    csrf.exempt(booking_bp)
    csrf.exempt(caldav_bp)
    app.register_blueprint(billing_bp)
    # The Stripe webhook authenticates via its signature header, not a session
    # cookie — exempt only that one route (checkout/portal forms keep CSRF).
    csrf.exempt(billing_webhook)
    app.register_blueprint(api_v1_bp)
    csrf.exempt(api_v1_bp)

    from application.cli import register_cli

    register_cli(app)

    from application.views.filters import register_filters

    register_filters(app)

    # Forbidden → a friendly "no access" page instead of a bare error. Routes
    # abort(403) when an item EXISTS but the user may not view it (vs abort(404)
    # for a genuinely missing item).
    from flask import render_template

    @app.errorhandler(403)
    def _forbidden(_e):
        return render_template("errors/403.html"), 403

    # Under gunicorn, route Flask's logger through gunicorn's error handler so
    # app logs AND unhandled-exception tracebacks (app.log_exception) actually
    # reach the container's stderr — otherwise they vanish in production.
    import logging
    gunicorn_logger = logging.getLogger("gunicorn.error")
    if gunicorn_logger.handlers:
        app.logger.handlers = gunicorn_logger.handlers
        app.logger.setLevel(gunicorn_logger.level)

    return app


def _default_config() -> str:
    return os.environ.get("BKTODO_CONFIG", "application.config.DevConfig")


def _init_limiter(app: Flask) -> None:
    """Wire the rate limiter, preferring Redis so counters are shared.

    Falls back to in-process counters when Redis isn't reachable rather than
    refusing to boot: a deployment without a broker (the documented no-broker
    setup, see the sync-zammad-all CLI) should still start. The fallback is
    weaker — per-worker quotas — so it says so loudly in the log.
    """
    app.config.setdefault("RATELIMIT_HEADERS_ENABLED", True)
    url = app.config.get("REDIS_URL") or ""
    storage = "memory://"
    if url and app.config.get("RATELIMIT_ENABLED", True):
        try:
            import redis as _redis

            _redis.Redis.from_url(url, socket_connect_timeout=1).ping()
            storage = url
        except Exception as exc:  # noqa: BLE001 — any connection problem
            app.logger.warning(
                "Rate limiter: Redis at %s unreachable (%s) — falling back to "
                "in-process counters, which are per-worker and therefore "
                "weaker than the configured limits.", url, exc,
            )
    app.config["RATELIMIT_STORAGE_URI"] = storage
    limiter.init_app(app)


def _init_mongo(app: Flask) -> None:
    disconnect(alias="default")
    connect(
        db=app.config["MONGO_DB"],
        host=app.config["MONGO_HOST"],
        port=app.config["MONGO_PORT"],
        username=app.config.get("MONGO_USER") or None,
        password=app.config.get("MONGO_PASSWORD") or None,
        authentication_source=app.config.get("MONGO_AUTH_SOURCE") or None,
        tz_aware=True,
        alias="default",
    )
