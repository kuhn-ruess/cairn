"""Authentication blueprint."""
from datetime import datetime, timezone

from urllib.parse import urlparse

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from application import limiter
from application.forms import (
    ForgotPasswordForm, LoginForm, RegisterForm, ResetPasswordForm,
)
from application.models.user import User

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def _login_failed(response) -> bool:
    """Only a failed login consumes the quota.

    A successful login redirects (302); a rejected one re-renders the form
    (200). So someone who signs in correctly is never throttled, however often
    they do it, while guessing burns the budget. GETs of the login page render
    200 too, hence the method check.
    """
    return request.method == "POST" and response.status_code == 200


def _safe_next_url() -> str:
    """Return the post-login destination — ``?next=…`` if it points at
    our own host, dashboard otherwise. Prevents the open-redirect
    phishing primitive."""
    raw = request.args.get("next") or ""
    if raw:
        # Backslashes first: urlparse("/\\evil.com") reports no netloc, so the
        # path-relative branch below would accept it — but browsers normalise
        # \ to / and resolve it as the protocol-relative //evil.com, landing
        # the user off-site after a genuine login. ("//evil.com" and
        # "https://evil.com" are already caught by the netloc check.)
        if "\\" in raw:
            return url_for("dashboard.home")
        parsed = urlparse(raw)
        # Allow only path-relative redirects, or absolute URLs whose
        # netloc matches the current request host.
        if not parsed.netloc and not parsed.scheme and raw.startswith("/"):
            return raw
        if parsed.netloc == request.host and parsed.scheme in ("", "http", "https"):
            return raw
    return url_for("dashboard.home")


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_LOGIN"],
               deduct_when=_login_failed,
               error_message="Too many failed sign-in attempts. "
                             "Please wait a few minutes and try again.")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))

    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.objects(email=email).first()
        if user and user.check_password(form.password.data):
            login_user(user, remember=form.remember.data)
            user.last_login_at = datetime.now(timezone.utc)
            user.save()
            # Focus-on-login: send the user to the folder picker first when
            # either the saved preference or the per-login checkbox asks for
            # it, unless an explicit ?next= deep-link takes precedence.
            if (user.focus_on_login or form.focus.data) and not request.args.get("next"):
                return redirect(url_for("focus.choose"))
            return redirect(_safe_next_url())
        flash("Invalid credentials.", "danger")
    return render_template("auth/login.html", form=form)


# German umlauts/ß transliterated the conventional way *before* NFKD. NFKD
# decomposes umlauts to a/o/u (losing the e) and leaves ß untouched, so
# encode("ascii","ignore") would drop ß entirely — "Rueß" → "rue". Mapping first
# gives "ruess" / "ä→ae" etc.
_DE_TRANSLIT = {
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "Ä": "ae", "Ö": "oe", "Ü": "ue",
}


def _slugify_username(name: str) -> str:
    """Reduce a display name to an ASCII slug fit for the username field
    (lowercase, alnum + dashes, ≤ 64 chars). German umlauts/ß are transliterated
    (ä→ae, ö→oe, ü→ue, ß→ss) rather than dropped, so "Sven Rueß" → "sven-ruess".
    Falls back to ``user`` so we never end up with an empty string."""
    import re
    import unicodedata
    s = "".join(_DE_TRANSLIT.get(ch, ch) for ch in (name or ""))
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s or "user")[:64]


def _unique_username(base: str) -> str:
    """``base``, with a numeric suffix appended if it's already taken."""
    if not User.objects(username=base).first():
        return base
    n = 2
    while True:
        candidate = f"{base[:60]}-{n}" if len(base) > 60 else f"{base}-{n}"
        if not User.objects(username=candidate).first():
            return candidate
        n += 1


@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_REGISTER"],
               methods=["POST"],
               error_message="Too many sign-up attempts. Please try again later.")
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))

    form = RegisterForm()
    if form.validate_on_submit():
        display_name = form.display_name.data.strip()
        email = form.email.data.lower().strip()
        if User.objects(email=email).first():
            flash("Email already registered.", "danger")
        else:
            username = _unique_username(_slugify_username(display_name))
            user = User(
                username=username,
                email=email,
                display_name=display_name,
            )
            user.set_password(form.password.data)
            # New users get a free premium trial; level reflects it immediately.
            user.start_trial(current_app.config.get("TRIAL_DAYS", 30))
            from application.services.billing import recompute_level
            recompute_level(user, save=False)
            user.save()
            login_user(user)
            # If they got here via an invite link, auto-redirect into the
            # accept flow so they can complete it in one go.
            pending = session.pop("pending_team_invite", None)
            if pending:
                flash("Welcome aboard — finish joining your team below.", "success")
                return redirect(url_for("teams.accept", token=pending))
            flash("Welcome aboard!", "success")
            return redirect(url_for("dashboard.home"))
    return render_template("auth/register.html", form=form)


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_PASSWORD_RESET"],
               methods=["POST"],
               error_message="Too many password-reset requests. "
                             "Please wait before requesting another.")
def forgot_password():
    """Kick off a password reset.

    Always reports success to the user — never leak whether an account
    exists. If SMTP is configured we email the link; otherwise we ask the
    user to contact the admin (who can mint a link via the CLI).
    """
    from application.services.mail import send_email, smtp_configured

    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))
    form = ForgotPasswordForm()
    sent = False
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.objects(email=email).first()
        if user and user.is_active_user:
            token = user.issue_password_reset()
            reset_url = url_for("auth.reset_password", token=token, _external=True)
            if smtp_configured():
                send_email(
                    user.email,
                    "Cairn — password reset",
                    f"Hi {user},\n\n"
                    f"Use the link below within the next 2 hours to set a new "
                    f"password. If you didn't request this, ignore this email.\n\n"
                    f"{reset_url}\n\n— Cairn",
                )
            else:
                current_app.logger.warning(
                    "Password reset for %s issued — SMTP not configured. "
                    "Admin: send the user this URL: %s",
                    user.username, reset_url,
                )
        sent = True
    return render_template(
        "auth/forgot_password.html",
        form=form, sent=sent,
        smtp_ok=__import__("application.services.mail", fromlist=["smtp_configured"]).smtp_configured(),
    )


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_RESET_CONFIRM"],
               error_message="Too many attempts. Please request a new reset link.")
def reset_password(token):
    if current_user.is_authenticated:
        logout_user()
    user = User.objects(password_reset_token=token).first()
    if not user or not user.password_reset_valid(token):
        return render_template("auth/reset_password.html", form=None, invalid=True)

    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        user.consume_password_reset()
        flash("Password updated — please sign in.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/reset_password.html", form=form, invalid=False)
