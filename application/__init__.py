"""bktodo Flask application factory."""
import os

from dotenv import load_dotenv
from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from mongoengine import connect, disconnect
from werkzeug.middleware.proxy_fix import ProxyFix

# Public app version — surfaced in the nav footer + the About page.
APP_VERSION = "1.0.10"

# Pull settings out of a local ``.env`` file if one exists. Doesn't override
# values that are already set in the real environment.
load_dotenv()

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"
csrf = CSRFProtect()


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

    from application.models.user import User

    @login_manager.user_loader
    def load_user(user_id: str):
        return User.objects(pk=user_id).first()

    from application.views.auth import auth_bp
    from application.views.dashboard import dashboard_bp
    from application.views.tasks import tasks_bp
    from application.views.projects import projects_bp
    from application.views.tags import tags_bp
    from application.views.review import review_bp
    from application.views.settings import settings_bp
    from application.views.tree import tree_bp
    from application.views.perspectives import perspectives_bp
    from application.views.admin import admin_bp
    from application.views.teams import teams_bp
    from application.views.public_share import public_bp
    from application.views.trash import trash_bp
    from application.api.v1 import api_v1_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(tags_bp)
    app.register_blueprint(review_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(tree_bp)
    app.register_blueprint(perspectives_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(teams_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(trash_bp)
    app.register_blueprint(api_v1_bp)
    csrf.exempt(api_v1_bp)

    from application.cli import register_cli

    register_cli(app)

    from application.views.filters import register_filters

    register_filters(app)

    return app


def _default_config() -> str:
    return os.environ.get("BKTODO_CONFIG", "application.config.DevConfig")


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
