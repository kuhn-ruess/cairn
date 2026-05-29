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


class DevConfig(BaseConfig):
    DEBUG = True


class ProdConfig(BaseConfig):
    DEBUG = False
    SECRET_KEY = ""  # supply via BKTODO_SECRET_KEY


class TestConfig(BaseConfig):
    TESTING = True
    MONGO_DB = "bktodo_test"
    WTF_CSRF_ENABLED = False
