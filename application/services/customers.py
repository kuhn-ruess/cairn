"""Customers, as one concept for the whole app.

There is exactly **one** customer entity in Cairn — ``Customer``, the one the
billing module already had. It is not duplicated for the GTD tree or for
appointments; those point at it:

* a folder/project names its customer (``Item.customer``) and everything below
  inherits it, the same climb ``time_project`` gets;
* an appointment booked through the public page names its customer
  (``Booking.customer``), matched from the **mail domain** the guest booked
  with.

Two rules keep the domain match from doing damage, and both are here rather
than at the call site because they are what makes an automatic guess safe:

* **A free-mail domain never matches.** Somebody books from
  ``anna@gmail.com``; a single customer row carrying ``gmail.com`` would
  quietly adopt every stranger who ever books a call, and the mistake surfaces
  as a wrong invoice. The domains are still *storable* — a report can group by
  them — they are simply never matched on.
* **Ambiguity resolves to nothing.** If two customers claim the same domain the
  appointment gets no customer, because a coin toss here is worse than the empty
  field the list asks you to fill in.
"""

# Mail providers where the domain says nothing about who somebody works for.
# Deliberately short: this is not a blocklist of the world's free mail, it is
# the handful that actually turn up in a German-speaking address book plus the
# global ones. A domain missing here is only ever a match somebody typed in.
FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "outlook.de", "hotmail.com",
    "hotmail.de", "live.com", "live.de", "msn.com", "yahoo.com", "yahoo.de",
    "ymail.com", "aol.com", "icloud.com", "me.com", "mac.com",
    "gmx.de", "gmx.net", "gmx.at", "gmx.ch", "gmx.com",
    "web.de", "t-online.de", "freenet.de", "posteo.de", "mailbox.org",
    "protonmail.com", "proton.me", "pm.me", "tutanota.com", "tuta.io",
    "mail.com", "zoho.com", "yandex.com", "yandex.ru", "fastmail.com",
    "hey.com", "gmx.co.uk", "aol.de", "arcor.de", "1und1.de", "online.de",
})


def normalise_domain(raw: str) -> str:
    """``" @Acme.DE "`` → ``"acme.de"``. Empty for anything unusable.

    Takes a bare domain or a whole address, because both get pasted into the
    field — and a row reading ``anna@acme.de`` would otherwise match nothing
    for ever, silently."""
    d = (raw or "").strip().lower().lstrip("@")
    if "@" in d:
        d = d.rsplit("@", 1)[-1]
    d = d.strip().strip(".")
    # A domain has a dot and no whitespace/path in it. Anything else is a typo,
    # and storing it would only ever produce a match that never fires.
    if not d or " " in d or "/" in d or "." not in d:
        return ""
    return d


def parse_domains(raw: str) -> list[str]:
    """The domain field as typed — commas, spaces or newlines — deduplicated
    and in the order given."""
    out: list[str] = []
    for chunk in (raw or "").replace(",", " ").replace(";", " ").split():
        d = normalise_domain(chunk)
        if d and d not in out:
            out.append(d)
    return out


def domain_of(email: str) -> str:
    """The domain part of an address, normalised. Empty when there isn't one."""
    addr = (email or "").strip().lower()
    if "@" not in addr:
        return ""
    return normalise_domain(addr.rsplit("@", 1)[-1])


def is_freemail(domain: str) -> bool:
    return normalise_domain(domain) in FREEMAIL_DOMAINS


def customer_for_email(email: str, teams) -> "object | None":
    """The customer whose domain this address belongs to, or ``None``.

    ``None`` for: no address, a free-mail address, no customer claiming the
    domain, or **more than one** claiming it — see the module docstring. One
    indexed query (``(team, email_domains)``).
    """
    from application.models.timetracking import Customer

    domain = domain_of(email)
    if not domain or domain in FREEMAIL_DOMAINS:
        return None
    teams = [t for t in (teams or []) if t is not None]
    if not teams:
        return None
    hits = list(Customer.objects(team__in=teams, archived__ne=True,
                                 email_domains=domain).limit(2))
    return hits[0] if len(hits) == 1 else None


def resolve_customer(item):
    """The customer a single Cairn item belongs to, or ``None``.

    Climbs the parents and takes the first answer it finds, exactly like
    ``services.timetracking.resolve_time_project``: the container's own
    ``customer``, else the customer of the container's billing project. Bounded
    and cycle-guarded; an archived or deleted customer resolves to ``None``
    rather than raising.

    For a *list* of items use ``services.containers.customer_labels`` — this
    dereferences per level and is meant for one item.
    """
    seen, node, depth = set(), item, 0
    while node is not None and depth < 50:
        if node.pk in seen:
            break
        seen.add(node.pk)
        # A binding belongs on a folder/project, never on a task — the same rule
        # ``resolve_time_project`` states, and for the same reason.
        if getattr(node, "kind", None) != "task":
            try:
                cust = node.customer
                if cust is not None and not cust.archived:
                    return cust
            except Exception:
                return None
            tp = getattr(node, "time_project", None)
            if tp:
                try:
                    if not tp.archived and not tp.customer.archived:
                        return tp.customer
                except Exception:
                    return None
        node = node.parent
        depth += 1
    return None


def container_customer(item):
    """The customer a folder/project itself answers with, or ``None``.

    The one named on it, else the one its billing project belongs to — the
    container's OWN answer, with no climb (that is ``resolve_customer``). A
    container bound to a billing project needs no second act of naming: it
    already says whose work it is, and this is what the picker shows for it
    before anybody presses save.

    Archived is not an answer, on either side — the same rule the rest of the
    module keeps.
    """
    if item is None:
        return None
    try:
        cust = item.customer
        if cust is not None:
            return None if cust.archived else cust
    except Exception:          # a customer that was merged or deleted away
        return None
    try:
        tp = item.time_project
        if tp is None or tp.archived or tp.customer.archived:
            return None
        return tp.customer
    except Exception:
        return None


def customer_choices(user):
    """``[(id, "Acme GmbH"), …]`` — every customer the user may name, sorted.

    Across *all* the teams they participate in, because the folder they are
    editing may well be filed for a colleague's team. Archived customers are
    left out: they are out of the pickers everywhere else too.

    That reach is also the one way this list can offer the same company twice:
    a customer belongs to exactly one team, so somebody in two teams that both
    invoice Acme gets two rows called "Acme GmbH" with nothing to choose
    between them. A name that appears twice therefore carries its team — the
    same rule the merge page follows, and for the same reason: picking the
    wrong one of two identical rows is a mistake that reaches an invoice.
    """
    from application.models.timetracking import Customer
    from application.services.timetracking import (
        ambiguous_names, qualified_name, user_teams,
    )

    teams = user_teams(user)
    if not teams:
        return []
    team_names = {str(t.pk): t.name for t in teams}
    rows = sorted(Customer.objects(team__in=teams, archived__ne=True)
                  .no_dereference().only("pk", "name", "team"),
                  key=lambda c: (c.name or "").lower())
    if len(team_names) < 2:
        return [(str(c.pk), c.name) for c in rows]
    twice = ambiguous_names(c.name for c in rows)
    out = []
    for c in rows:
        ref = c._data.get("team")
        out.append((str(c.pk),
                    qualified_name(c.name, twice,
                               team_names.get(str(getattr(ref, "id", ref)), ""))))
    return out


def customer_for_user(user, customer_id: str):
    """Resolve a posted customer id inside the caller's own teams, or ``None``.

    The counterpart of ``services.timetracking.project_for_user``: a tampered
    POST must not be able to bind a folder to a stranger's customer.
    """
    from application.models.timetracking import Customer
    from application.services.timetracking import to_oid, user_teams

    oid = to_oid(customer_id)
    if oid is None:
        return None
    teams = user_teams(user)
    if not teams:
        return None
    return Customer.objects(pk=oid, team__in=teams).first()
