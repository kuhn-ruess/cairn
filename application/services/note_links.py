"""Wiki-style note-to-note links — ``[[Note title]]``.

Notes reference each other by TITLE, the way Evernote/Obsidian do: the user
types ``[[Weekly review]]`` (or ``[[Weekly review|last week's]]`` for a custom
label) and gets a link. Two consumers:

* **rendering** — :func:`expand` rewrites ``[[…]]`` into an ordinary Markdown
  link *before* the ``markdown`` filter runs, so the sanitiser never has to
  learn a custom syntax. A title that matches no note becomes a "create it"
  link rather than a dead end.
* **backlinks** — :func:`rebuild` stores the resolved targets on
  ``Item.note_links``, so the "Linked mentions" panel is one indexed query
  instead of a regex scan over every note body in the collection.

Resolution is by title, case-insensitive, over the notes the user can see, and
happens at *render* and *save* time — not once at typing time. Renaming a note
therefore does not rewrite the notes pointing at it; the stale ``[[Title]]``
degrades to a create-link. That's deliberate: silently rewriting bodies the
user (or a share partner) owns is worse than one visibly broken link.
"""
import re

from mongoengine import Q

# ``[[target]]`` or ``[[target|label]]``. Both sides stay free of brackets and
# pipes so a stray "[[" in code can't swallow the rest of the note.
WIKILINK_RE = re.compile(r"\[\[([^\[\]|\n]{1,200}?)(?:\|([^\[\]|\n]{1,200}?))?\]\]")


def title_index(user, notes=None) -> dict:
    """``{lowercased title: (pk_str, title)}`` for every note ``user`` can see.

    Pass ``notes`` (e.g. the already-loaded sidebar tree) to build the index
    without a second query — the notes pages always have that list in hand.
    Duplicate titles resolve to the first match; the caller's ordering decides.
    """
    if notes is None:
        from application.models.item import Item
        from application.services.permissions import container_visibility_q
        notes = Item.objects(
            container_visibility_q(user) & Q(kind="note")
        ).only("pk", "title")
    index = {}
    for n in notes:
        key = (n.title or "").strip().lower()
        if key and key not in index:
            index[key] = (str(n.pk), n.title)
    return index


def expand(text: str, index: dict, *, new_note_url=None) -> str:
    """Rewrite ``[[…]]`` into Markdown links against ``index``.

    Unresolved targets point at ``new_note_url(title)`` (the pre-filled
    new-note form) and are marked with a trailing ``⁺`` so a typo is obvious
    while reading, without needing a CSS class the sanitiser would strip.
    """
    if not text or "[[" not in text:
        return text or ""

    def repl(m):
        target = (m.group(1) or "").strip()
        label = (m.group(2) or "").strip() or target
        if not target:
            return m.group(0)
        hit = index.get(target.lower())
        if hit:
            return f"[{label}](/notes/{hit[0]})"
        if new_note_url is None:
            return label
        # Parens must not survive into a Markdown link target or they'd close
        # it early ("(…?title=Plan (Q3))" → broken link + leaked text).
        url = new_note_url(target).replace("(", "%28").replace(")", "%29")
        return f"[{label}⁺]({url})"

    return WIKILINK_RE.sub(repl, text)


def targets(text: str, index: dict) -> list:
    """The pk strings this body links to, de-duplicated, order preserved."""
    out, seen = [], set()
    if not text or "[[" not in text:
        return out
    for m in WIKILINK_RE.finditer(text):
        hit = index.get((m.group(1) or "").strip().lower())
        if hit and hit[0] not in seen:
            seen.add(hit[0])
            out.append(hit[0])
    return out


def rebuild(item, index: dict) -> bool:
    """Refresh ``item.note_links`` from its body. Does NOT save — the caller
    is already saving. Returns True if the edge set actually changed.

    Self-links are dropped (a note linking itself would show up in its own
    backlinks panel).
    """
    from application.models.item import Item
    pks = [pk for pk in targets(item.note, index) if pk != str(item.pk)]
    before = {str(r.pk if hasattr(r, "pk") else r.id)
              for r in (item._data.get("note_links") or [])}
    if before == set(pks):
        return False
    item.note_links = list(Item.objects(pk__in=pks)) if pks else []
    return True


def backlinks(item, user):
    """Notes that link TO ``item`` and that ``user`` may see — the "Linked
    mentions" panel. One indexed query on ``note_links``."""
    from application.models.item import Item
    from application.services.permissions import container_visibility_q
    return list(
        Item.objects(
            container_visibility_q(user) & Q(kind="note", note_links=item)
        ).only("pk", "title", "parent").order_by("+title")
    )
