"""Configuration objects for bktodo."""


class BaseConfig:
    SECRET_KEY = "dev-change-me"
    MONGO_HOST = "localhost"
    MONGO_PORT = 27017
    MONGO_DB = "bktodo"
    MONGO_USER = ""
    MONGO_PASSWORD = ""
    MONGO_AUTH_SOURCE = ""
    WTF_CSRF_TIME_LIMIT = None
    TASKS_PER_PAGE = 50
    REVIEW_DEFAULT_INTERVAL_DAYS = 7

    # Redis broker/result backend for the Celery background workers
    # (distributed Zammad auto-sync). Override with BKTODO_REDIS_URL; in
    # Docker that's ``redis://redis:6379/0``.
    REDIS_URL = "redis://localhost:6379/0"
    # How stale a user's last Zammad sync may get before the beat scheduler
    # re-enqueues them. Beat ticks every minute; this spaces the actual syncs.
    ZAMMAD_SYNC_INTERVAL_MIN = 5

    # IANA timezone name attached to Google Calendar events when the
    # task's datetime is naive (the datetime-local form input has no
    # offset). Set via BKTODO_DEFAULT_TZ in env, e.g. ``Europe/Berlin``.
    DEFAULT_TZ = "UTC"

    # Single Google OAuth client for the whole deployment (SaaS model).
    # Set via BKTODO_GOOGLE_CLIENT_ID + BKTODO_GOOGLE_CLIENT_SECRET in env;
    # users never see these — they just click "Connect Google".
    GOOGLE_CLIENT_ID = ""
    GOOGLE_CLIENT_SECRET = ""

    # SMTP for password-reset emails (and future notifications).
    # Empty SMTP_HOST disables mail — the password-reset flow then asks
    # the user to contact the admin (who can use ``./cairn reset-password``).
    SMTP_HOST = ""
    SMTP_PORT = "587"
    SMTP_USER = ""
    SMTP_PASSWORD = ""
    SMTP_FROM = ""
    SMTP_TLS = "1"

    # ---- Stripe subscription billing ----
    # New users get TRIAL_DAYS of premium for free; afterwards they either
    # subscribe (premium €2/mo, team €5/mo) or fall back to free.
    # Leave STRIPE_SECRET_KEY empty to disable billing entirely (the /billing
    # page then explains it isn't configured). All keys come from env as
    # BKTODO_STRIPE_* — the two price ids are created in the Stripe dashboard.
    STRIPE_SECRET_KEY = ""
    STRIPE_PUBLISHABLE_KEY = ""
    STRIPE_WEBHOOK_SECRET = ""
    STRIPE_PRICE_PREMIUM = ""       # recurring €2/mo price id
    STRIPE_PRICE_TEAM = ""          # recurring €5/mo price id (commercial)
    STRIPE_PRICE_TEAM_FAMILY = ""   # recurring €2/mo price id (family / non-profit)
    TRIAL_DAYS = 30
    # Human-readable labels shown on the billing page (display only).
    PRICE_PREMIUM_LABEL = "€2 / month"
    PRICE_TEAM_LABEL = "€5 / month"
    PRICE_TEAM_FAMILY_LABEL = "€2 / month"


class DevConfig(BaseConfig):
    DEBUG = True


class ProdConfig(BaseConfig):
    DEBUG = False
    SECRET_KEY = ""  # supply via BKTODO_SECRET_KEY


class TestConfig(BaseConfig):
    TESTING = True
    MONGO_DB = "bktodo_test"
    WTF_CSRF_ENABLED = False
