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

    # The notes mirror writes to a WebDAV server the user names, so the SSRF
    # guard applies (services/http_safety). A NAS on the same LAN or behind a
    # VPN has a private address and is refused by design — set
    # BKTODO_MIRROR_ALLOW_PRIVATE=1 to allow it for this deployment, knowing
    # that it lets a user point the mirror at an internal address.
    MIRROR_ALLOW_PRIVATE = False

    # ---- Brute-force limits on the credential endpoints -------------------
    # Counted per client IP, and — for the two login paths — only on FAILED
    # attempts, so a working client is never throttled no matter how often it
    # authenticates. Values are limits-library strings; override via
    # BKTODO_RATELIMIT_* to loosen or tighten without a code change.
    RATELIMIT_ENABLED = True
    # Web login. Generous enough for a person mistyping a password, far below
    # what password guessing needs.
    RATELIMIT_LOGIN = "10 per 15 minutes"
    # CalDAV Basic auth. Machine-facing and CSRF-exempt, so it's the softest
    # brute-force target — but Apple clients poll constantly and re-send stale
    # credentials after a password change, hence a failure-only limit that's
    # tolerant enough not to lock out a genuinely misconfigured client forever.
    RATELIMIT_CALDAV = "20 per 15 minutes"
    # Password-reset mail: limits both mailbombing a victim and reset-token
    # fishing. Counts every POST, not just failures — sending IS the effect.
    RATELIMIT_PASSWORD_RESET = "5 per hour"
    # Reset-token submission — guessing a 256-bit token is hopeless anyway,
    # this just closes the loop.
    RATELIMIT_RESET_CONFIRM = "10 per hour"
    # Signup, against automated account creation.
    RATELIMIT_REGISTER = "5 per hour"

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
    # Seat-based team billing. The paying admin's subscription carries a seat
    # quantity that tracks the team's billable member count (the paying admin's
    # own seat is free). See application.services.billing.
    #   premium     — €2/mo solo (licensed, qty 1)
    #   seat_org    — €5/mo per member seat (organisation teams)
    #   family_base — €5/mo flat, covers the first 5 family member seats
    #   seat_family — €2/mo per extra family member seat (6th onward)
    STRIPE_PRICE_PREMIUM = ""
    STRIPE_PRICE_SEAT_ORG = ""
    STRIPE_PRICE_FAMILY_BASE = ""
    STRIPE_PRICE_SEAT_FAMILY = ""
    FAMILY_INCLUDED_SEATS = 5
    TRIAL_DAYS = 30
    # Human-readable labels shown on the billing page (display only).
    PRICE_PREMIUM_LABEL = "€2 / month"
    PRICE_TEAM_FAMILY_LABEL = "€5 / month — 5 seats included, then €2 / seat"
    PRICE_TEAM_ORG_LABEL = "€5 / member / month"


class DevConfig(BaseConfig):
    DEBUG = True


class ProdConfig(BaseConfig):
    DEBUG = False
    SECRET_KEY = ""  # supply via BKTODO_SECRET_KEY


class TestConfig(BaseConfig):
    TESTING = True
    MONGO_DB = "bktodo_test"
    WTF_CSRF_ENABLED = False
    # Off by default so a test run doesn't trip the login limiter and start
    # failing unrelated assertions with 429s. Flip it on in the test that
    # actually exercises the limits.
    RATELIMIT_ENABLED = False
