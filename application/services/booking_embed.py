"""How the public booking page dresses for somebody else's site.

All of it comes from the URL the host pastes into their page, so all of it is
read here, checked here, and nothing a stranger types reaches a stylesheet
unchecked:

* ``embed=1``    framed: no outer margins, no background, reports its height.
* ``bare=1``     only the booking itself: no photo, title, name, intro and no
                 "Powered by" line. The surrounding page has its own heading.
* ``compact=1``  tighter spacing and a smaller month grid, for a narrow column.
* ``accent=hex`` the colour of the primary buttons and links (``4f8a2c`` or
                 ``#4f8a2c``, three or six digits), so the widget wears the
                 site's colour instead of Cairn's yellow.
* ``lang=de``    German wording; anything else is the English page.
* ``theme=``     ``light``/``dark`` (see ``views/booking.py``).

``bare``, ``compact`` and ``accent`` only count inside an embed: the page on
Cairn's own address stays the page Cairn shows. ``lang`` counts everywhere,
because the direct link beside an embed should speak the same language.

Pure on purpose: no request, no app, so it is tested without either.
"""
import re

from markupsafe import Markup

_HEX = re.compile(r"^[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$")

LANGS = ("en", "de")


def _flag(value) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def accent(value):
    """``(hex, rgb, fg)`` for a usable colour, else ``None``.

    ``hex`` is six lowercase digits without ``#``; ``fg`` is the text colour
    that stays readable on it (dark on a light accent, white on a dark one)."""
    raw = (value or "").strip().lstrip("#")
    if not _HEX.match(raw):
        return None
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    raw = raw.lower()
    rgb = tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))

    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    lum = 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2])
    # The crossover where white and #1d2021 text have the same contrast.
    fg = "#1d2021" if lum > 0.21 else "#ffffff"
    return raw, rgb, fg


def options(args) -> dict:
    """What the URL asked for, checked. ``args`` is any mapping with ``get``."""
    embed = bool(args.get("embed"))
    lang = (args.get("lang") or "").strip().lower()
    lang = lang if lang in LANGS else ""
    color = accent(args.get("accent")) if embed else None
    return {
        "embed": embed,
        "bare": embed and _flag(args.get("bare")),
        "compact": embed and _flag(args.get("compact")),
        "accent": color,
        "lang": lang,
    }


def carried(opts: dict, theme: str) -> dict:
    """The query a follow-up URL needs to stay dressed the same way: the form
    POST and the confirmation it answers with. Empty values are left out, so a
    plain page keeps its plain URLs."""
    keep = {
        "embed": 1 if opts["embed"] else None,
        "bare": 1 if opts["bare"] else None,
        "compact": 1 if opts["compact"] else None,
        "accent": opts["accent"][0] if opts["accent"] else None,
        "lang": opts["lang"] or None,
        "theme": theme or None,
    }
    return {k: v for k, v in keep.items() if v is not None}


# The visitor's side of the page in German. The key is the English text, so a
# missing entry falls back to what the page always said. The owner's own
# panel ("what blocks this day") never shows inside an embed and stays English
# like the rest of the app.
GERMAN = {
    # booking.html
    "Book time with {name}": "Termin mit {name}",
    "How long?": "Wie lange?",
    "Pick a time": "Zeit wählen",
    "Times in": "Zeiten in",
    "switch": "wechseln",
    "Loading free times…": "Freie Zeiten werden geladen…",
    "Your details": "Ihre Angaben",
    "Leave this field empty": "Dieses Feld leer lassen",
    "Your name": "Ihr Name",
    "Your email": "Ihre E-Mail-Adresse",
    "Your meeting link (optional)": "Ihr Meeting-Link (optional)",
    "teams_hint": "Fügen Sie Ihren eigenen Raum ein, um sich dort zu treffen. "
                  "Ohne Eintrag erhalten Sie einen Microsoft-Teams-Link.",
    "link_hint": "Fügen Sie Ihren eigenen Raum ein, um sich dort zu treffen. "
                 "Ohne Eintrag gilt der Link von {name}.",
    "own_hint": "Wenn Sie sich lieber in Ihrem eigenen Raum treffen, fügen Sie "
                "den Link hier ein. Er steht dann in beiden Bestätigungen und "
                "im Kalendereintrag.",
    "What's it about? (optional)": "Worum geht es? (optional)",
    "Confirm booking": "Termin buchen",
    "Pick another time": "Andere Zeit wählen",
    "busy_note": "Ihr Termin wird gebucht, bitte warten. Das kann einige "
                 "Sekunden dauern. Bitte laden Sie die Seite nicht neu.",
    "{tz} is {name}'s timezone": "{tz} ist die Zeitzone von {name}",
    # booking-page.js
    "No free times in the next weeks. Try a shorter appointment.":
        "In den nächsten Wochen ist nichts frei. Versuchen Sie einen kürzeren Termin.",
    "1 day with free times": "1 Tag mit freien Zeiten",
    "{n} days with free times": "{n} Tage mit freien Zeiten",
    "{n} free times": "{n} freie Zeiten",
    "Nothing free on this day.": "An diesem Tag ist nichts frei.",
    "Previous month": "Vorheriger Monat",
    "Next month": "Nächster Monat",
    "Free times couldn't be loaded.": "Die freien Zeiten konnten nicht geladen werden.",
    "{name}'s time": "Zeit von {name}",
    "Booking…": "Wird gebucht…",
    # booking_done.html
    "Booking confirmed": "Termin bestätigt",
    "You're booked": "Ihr Termin steht",
    "{n} minutes with {name}": "{n} Minuten mit {name}",
    "a confirmation is on its way to {email}": "die Bestätigung geht an {email}",
    "Join link": "Zum Meeting",
    "It's in your confirmation email too.": "Der Link steht auch in Ihrer Bestätigung.",
    "What you wrote:": "Ihre Nachricht:",
}

# The English text for the keys that are not themselves the English text.
ENGLISH = {
    "teams_hint": "Paste your own room to meet there instead. Leave it empty "
                  "and you get a Microsoft Teams link.",
    "link_hint": "Paste your own room to meet there instead. Leave it empty "
                 "and {name}'s link is used.",
    "own_hint": "If you'd like to meet in your own room, paste the link. It "
                "goes into both confirmations and the calendar entry.",
    "busy_note": "Booking your appointment, please wait. This can take a few "
                 "seconds. Please don't reload the page.",
}

# What booking-page.js says to a visitor; handed to it as JSON.
JS_KEYS = (
    "No free times in the next weeks. Try a shorter appointment.",
    "1 day with free times", "{n} days with free times", "{n} free times",
    "Nothing free on this day.", "Previous month", "Next month",
    "Loading free times…", "Free times couldn't be loaded.",
    "{name}'s time", "Booking…", "Confirm booking",
)


def translator(lang: str):
    """``t(text, **fields)`` in that language, English when there is none.

    The wording is ours and trusted, the fields are not: ``Markup.format``
    escapes every field (a host name, a guest's address) on its way in."""
    table = GERMAN if lang == "de" else {}

    def t(text, **fields):
        out = Markup(table.get(text) or ENGLISH.get(text, text))
        return out.format(**fields) if fields else out

    return t


def js_strings(lang: str) -> dict:
    """The script's wording; empty for English, which the script has built in."""
    if lang != "de":
        return {}
    return {k: GERMAN[k] for k in JS_KEYS if k in GERMAN}
