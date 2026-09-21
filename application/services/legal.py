"""Who runs *this* instance — the identity behind Impressum and Datenschutz.

Cairn ships the **text** of both legal pages: what the software stores, which
cookies it sets, which third parties it talks to when you switch them on. That
part is the same wherever Cairn runs, so it stays in the templates.

Who the Diensteanbieter is, where they can be reached and under which VAT id
they trade is *not* the same anywhere, and it is the one thing a self-hoster
must never inherit from somebody else. It therefore comes from configuration
only (``BKTODO_LEGAL_*``), and an instance that has not set it renders a notice
saying so instead of naming a person who has nothing to do with it.

``operator()`` is the single place that reads those values, so the templates
never touch ``current_app.config`` and a test can hand in a plain dict.
"""

from flask import current_app

#: config key ← attribute of the returned mapping.
_KEYS = {
    "name": "LEGAL_OPERATOR_NAME",
    "addition": "LEGAL_OPERATOR_ADDITION",
    "address": "LEGAL_OPERATOR_ADDRESS",
    "email": "LEGAL_OPERATOR_EMAIL",
    "phone": "LEGAL_OPERATOR_PHONE",
    "representative": "LEGAL_OPERATOR_REPRESENTATIVE",
    "register": "LEGAL_OPERATOR_REGISTER",
    "vat_id": "LEGAL_OPERATOR_VAT_ID",
    "insurance": "LEGAL_OPERATOR_INSURANCE",
    "hosting": "LEGAL_HOSTING_PROVIDER",
}

#: Values rendered as a block of lines rather than as a single string.
_MULTILINE = ("address", "insurance")

#: Without these three there is no Impressum worth printing: § 5 DDG asks for
#: the name, an address that can be served at, and a fast electronic contact.
#: Everything else on the page is optional *for some operators* and is simply
#: left out when it is not set — a sole proprietor has no Registergericht, an
#: operator outside Germany has no USt-IdNr.
REQUIRED = ("name", "address", "email")


def _lines(value):
    """Split a configured block into display lines.

    An env var is one line of text, so ``|`` separates what belongs on
    separate lines; a real newline (from a config object or a quoted .env
    value) works too.
    """
    if not value:
        return []
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "|")
    return [part.strip() for part in text.split("|") if part.strip()]


def operator(config=None):
    """Return the configured operator identity.

    ``configured`` is False when anything in :data:`REQUIRED` is missing — a
    half-filled Impressum is not one, so the pages show the "not configured"
    notice rather than part of an identity. ``missing`` names the env vars
    still to set, which is what that notice prints.
    """
    cfg = current_app.config if config is None else config
    data = {}
    for attr, key in _KEYS.items():
        raw = cfg.get(key) or ""
        data[attr] = _lines(raw) if attr in _MULTILINE else str(raw).strip()
    data["missing"] = [
        "BKTODO_" + _KEYS[attr] for attr in REQUIRED if not data[attr]
    ]
    data["configured"] = not data["missing"]
    return data
