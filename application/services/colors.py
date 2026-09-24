"""A row's colour, derived from the work it belongs to.

``Item.color`` has been settable on a project ever since the project inspector
got a colour picker, and was rendered nowhere — dead data. This module is what
reads it, and what answers for the hundreds of projects nobody will ever pick a
colour for: one derived from the container's id is deterministic, stable and
useful straight away, where an empty field is none of the three.

Two sources, one choice per user (``User.color_by``):

* ``project``  — the owning project/folder. Its own ``color`` when somebody set
  one, **inherited** from the nearest ancestor that did, so a colour on the
  "Acme" folder covers every project under it. Otherwise derived.
* ``customer`` — the customer the subtree bills to, exactly the one
  ``services/containers.customer_labels`` resolves. There is no second customer
  concept and no colour field on ``Customer``: the name is the handle, so the
  colour is derived from it and two rows that read "Acme GmbH" look alike, which
  is the point.

``none`` switches the whole thing off.

**The palette is gruvbox's NEUTRAL set** — the half of the theme that is built
to sit on a light *and* a dark background. The logged-in app is dark today and
the public pages (a shared project, an embedded booking page) are not, and a
colour picked for one of those is unreadable on the other. Nothing is ever
rendered *as text* in these colours either: they are a swatch beside
body-coloured text and a wash under it, so what has to hold is
swatch-against-surface, in both directions.

A colour is a hint, never a statement. It does not touch the completion circle
(priority), the NEXT chip, the blocked dimming or the calendar colour of an
event — each of those means something exact, and a second meaning on the same
pixel would cost them theirs.
"""
from __future__ import annotations

import hashlib
import re

#: Gruvbox's neutral hues, ordered so that neighbours in the palette are easy
#: to tell apart (a hash lands anywhere, so consecutive indices are not
#: consecutive rows — but a list of near-identical greens would still read as
#: one colour wherever two of them met).
PALETTE = (
    "#458588",  # blue
    "#d79921",  # yellow
    "#b16286",  # purple
    "#98971a",  # green
    "#d65d0e",  # orange
    "#689d6a",  # aqua
    "#cc241d",  # red
    "#83a598",  # bright blue
    "#fabd2f",  # bright yellow
    "#d3869b",  # bright purple
    "#b8bb26",  # bright green
    "#8ec07c",  # bright aqua
)

_HEX = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\Z")

#: The modes ``User.color_by`` may hold.
MODES = ("none", "project", "customer")


def normalise(value) -> str:
    """A hex colour this app is willing to put into a ``style`` attribute.

    Anything else answers ``""``. ``Item.color`` is a free-text field that has
    been written by a colour input, by the API and by an importer, and it ends
    up inside ``style="--dot: …"`` — so it is checked here rather than trusted
    there.
    """
    value = (value or "").strip()
    return value if _HEX.match(value) else ""


def palette_color(key) -> str:
    """The palette entry for ``key`` — the same one every time, everywhere.

    ``hashlib``, not the built-in ``hash()``: Python salts string hashing per
    process, so ``hash()`` would give one project a different colour in every
    worker and a new one after every restart.
    """
    key = str(key or "")
    if not key:
        return ""
    digest = hashlib.md5(key.encode("utf-8")).digest()
    return PALETTE[digest[0] % len(PALETTE)]


def row_colors(items, mode: str) -> dict:
    """``{str(item.pk): {"color": "#rrggbb", "label": "Acme / Website"}}``.

    Batched, because this is asked once per rendered list and never per row:
    both resolvers underneath walk the ancestors level by level and finish the
    lookups with one ``$in`` each (see ``services/containers``). A task that
    resolves to nothing is simply absent from the result, and the caller
    renders no swatch.
    """
    items = [i for i in (items or []) if getattr(i, "pk", None)]
    if not items or mode not in ("project", "customer"):
        return {}

    if mode == "customer":
        from application.services.containers import customer_labels
        # The customer resolver answers with names, which is also what the
        # rail groups are keyed by — so a name is the handle here too. Two
        # teams' identically named customers share a colour (harmless), and a
        # renamed customer gets a new one (it is a new name).
        return {pk: {"color": palette_color(f"customer:{name}"), "label": name}
                for pk, name in customer_labels(items).items() if name}

    from application.services.containers import container_accents
    out = {}
    for pk, info in container_accents(items).items():
        color = normalise(info.get("color")) or palette_color(
            f"project:{info.get('pk')}")
        if color:
            out[pk] = {"color": color, "label": info.get("label") or ""}
    return out
