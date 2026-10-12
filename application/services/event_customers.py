"""Whose meeting a calendar event is.

Events are read from the calendars on every page and never stored, so the
answer lives beside them (``models.calendar.EventCustomer``), keyed by the
event's SERIES uid: a weekly jour fixe with Acme is filed once.

Two ways an event gets a customer, the first one that answers wins:

* **said by hand** — a row for the series, which may also say "no customer"
  on purpose (that is how a wrong suggestion is silenced);
* **suggested from the title** (:func:`suggest`) — "Acme Jour Fixe" is Acme's.
  Only an unambiguous match counts: a title naming two customers, or a name two
  customers share (two teams' "Acme GmbH"), suggests nothing, because a guess
  here ends up preselecting a billing project.

What it is used for: the customer chip on the event, the Log time dialog
preselecting that customer, the customer's open tasks in the chip's panel, and
"Plan my day" putting the Next actions of the day's meeting customers first.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# Legal forms are not part of the name anybody types into a calendar entry:
# the customer is "Acme GmbH & Co. KG", the meeting is "Acme Jour Fixe".
_LEGAL = frozenset("""
    gmbh mbh ag kg kgaa ohg gbr ug haftungsbeschränkt co e v ev eg se
    ltd limited inc llc corp plc bv nv sa sarl sas spa srl ab as oy
""".split())
_NON_WORD = re.compile(r"[^\w]+")
# The shortest name worth matching on: anything shorter ("IT", "HR") is a word
# in half the titles of a calendar.
_MIN_CORE = 3

# A recurring event arrives as one event per occurrence, each with its own id:
# ``<uid>#<start>`` from CalDAV (services/calendar_sync), Google's
# ``<id>_20261012T080000Z`` (``singleEvents``). The series is the part before.
_CALDAV_OCCURRENCE = re.compile(r"#.*\Z")
_GOOGLE_OCCURRENCE = re.compile(r"_\d{8}(T\d{6}Z?)?\Z")


def series_uid(uid: str) -> str:
    """The id the whole series of ``uid`` is filed under."""
    uid = _CALDAV_OCCURRENCE.sub("", uid or "")
    return _GOOGLE_OCCURRENCE.sub("", uid)


def _words(text: str) -> list[str]:
    return [w for w in _NON_WORD.sub(" ", (text or "").lower()).split() if w]


def core_name(name: str) -> str:
    """A customer's name as a title would mention it: lowercase, words only,
    legal forms dropped. ``""`` when nothing worth matching on is left."""
    core = " ".join(w for w in _words(name) if w not in _LEGAL)
    return core if len(core) >= _MIN_CORE else ""


def suggest(title: str, customers) -> str | None:
    """The id of the one customer ``title`` names, or ``None``.

    ``customers`` is ``[(id, name)]``. A name matches as whole words. When
    several match and one name contains the others ("Acme" and "Acme
    Services" in "Acme Services Call"), the longer one is meant; anything else
    with more than one customer left is ambiguous and suggests nothing.

    Pure on purpose — no database, so the rule can be pinned in a test.
    """
    hay = f" {' '.join(_words(title))} "
    if not hay.strip():
        return None
    hits = []
    for cid, name in customers:
        core = core_name(name)
        if core and f" {core} " in hay:
            hits.append((str(cid), core))
    hits = [(cid, core) for cid, core in hits
            if not any(other != core and f" {core} " in f" {other} "
                       for _, other in hits)]
    ids = {cid for cid, _ in hits}
    return ids.pop() if len(ids) == 1 else None


def _customers(user):
    """``[(id, name)]`` of the customers the user may name — the same reach
    as ``customers.customer_choices``, raw names for matching."""
    from application.models.timetracking import Customer
    from application.services.timetracking import user_teams
    teams = user_teams(user)
    if not teams:
        return []
    return [(str(c.pk), c.name or "") for c in
            Customer.objects(team__in=teams, archived__ne=True).only("pk", "name")]


def for_events(user, events) -> dict:
    """``{event uid: {"id", "name", "suggested", "color"}}`` for the events
    that resolve to a customer. Two queries for any number of events."""
    from application.models.calendar import EventCustomer
    from application.services.colors import palette_color

    events = [e for e in (events or []) if getattr(e, "uid", "")]
    if not events:
        return {}
    customers = _customers(user)
    if not customers:
        return {}
    names = dict(customers)
    said = {}
    for row in (EventCustomer.objects(owner=user,
                                      uid__in=list({series_uid(e.uid) for e in events}))
                .no_dereference().only("uid", "customer")):
        ref = row._data.get("customer")
        said[row.uid] = str(getattr(ref, "id", ref)) if ref else ""
    out = {}
    for ev in events:
        sid = series_uid(ev.uid)
        if sid in said:
            cid, suggested = said[sid], False
        else:
            cid, suggested = suggest(ev.title, customers), True
        # A customer filed by hand and archived (or in a team the user left
        # since) simply reads as none.
        if cid and cid in names:
            out[ev.uid] = {"id": cid, "name": names[cid], "suggested": suggested,
                           "color": palette_color(f"customer:{names[cid]}")}
    return out


def assign(user, uid: str, customer_id: str, title: str = "") -> None:
    """File the series of ``uid``: a customer id, ``"none"`` for "no customer"
    on purpose, ``""`` to forget and go back to the title's suggestion.

    Raises ``ValueError`` for a customer outside the user's teams."""
    from application.models.calendar import EventCustomer
    from application.services.customers import customer_for_user

    sid = series_uid(uid)
    if not sid:
        raise ValueError("no event")
    if customer_id == "":
        EventCustomer.objects(owner=user, uid=sid).delete()
        return
    customer = None
    if customer_id != "none":
        customer = customer_for_user(user, customer_id)
        if customer is None:
            raise ValueError("unknown customer")
    EventCustomer.objects(owner=user, uid=sid).update_one(
        upsert=True, set__customer=customer, set__title=(title or "")[:500],
        set__updated_at=datetime.now(timezone.utc))


def open_tasks(user, customer_name: str, *, limit: int = 15):
    """``(tasks, total)``: the user's open tasks whose subtree bills to the
    customer called ``customer_name`` — Next actions first, then by priority.

    Matched by NAME because that is what ``containers.customer_labels``
    answers with, the same handle the Plan rails group by."""
    from mongoengine.queryset.visitor import Q
    from application.models.item import Item
    from application.services.containers import customer_labels

    if not customer_name:
        return [], 0
    tasks = list(Item.objects((Q(owner=user) | Q(assigned_to=user)),
                              kind="task",
                              status__nin=["done", "dropped", "someday"])
                 .only("pk", "title", "parent", "status", "priority",
                       "sort_order"))
    labels = customer_labels(tasks)
    mine = [t for t in tasks if labels.get(str(t.pk)) == customer_name]
    mine.sort(key=lambda t: (t.status != "next", t.priority or 99,
                             t.sort_order or 0))
    return mine[:limit], len(mine)
