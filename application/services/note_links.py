"""Wiki-style note-to-note links — ``[[Note title]]``.

Notes reference each other by TITLE, the way Evernote/Obsidian do: the user
types ``[[Weekly review]]`` (or ``[[Weekly review|last week's]]`` for a custom
label) and gets a link. Two consumers:

* **rendering** — :func:`expand` rewrites ``[[…]]`` into an ordinary Markdown
  link *before* the ``markdown`` filter runs, so the sanitiser never has to
  learn a custom syntax. A title that matches no note becomes a "create it"
  link rather than a dead end.
* **backlinks** — :func:`rebuild` stores the resolved targets on
  ``Item.note_links``, so :func:`linked_references` finds the pages pointing at
  one in a single indexed query instead of a regex scan over every note body in
  the collection. What it then SHOWS is the referencing blocks themselves, read
  under the chain of parents they live in.

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

# ``[Label](/notes/<id>)`` — an address inside this app rather than a wiki
# link. Written by the "/" menu before it knew better, and by anything that
# pastes a URL out of the address bar. It still says "this note is about that
# one", so it counts as a link: the alternative is a backlink panel that
# quietly disagrees with what the reader can plainly see on the page.
APP_LINK_RE = re.compile(r"\]\(/notes/([0-9a-fA-F]{24})\)")


def pages_q():
    """Everything in the notes tree that HAS a body: a note, or a notes-folder.

    A folder is a container for filing and for access and also a page you can
    write on, so it can hold links and be linked to like any other. Same
    expression the sidebar tree uses, so the two cannot drift apart."""
    return Q(kind="note") | Q(kind="folder", folder_domain="notes")


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
            container_visibility_q(user) & pages_q()
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
    if not text:
        return out
    for m in WIKILINK_RE.finditer(text):
        hit = index.get((m.group(1) or "").strip().lower())
        if hit and hit[0] not in seen:
            seen.add(hit[0])
            out.append(hit[0])
    for m in APP_LINK_RE.finditer(text):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    return out


def rebuild(item, index: dict, extra=()) -> bool:
    """Refresh ``item.note_links`` from its body. Does NOT save — the caller
    is already saving. Returns True if the edge set actually changed.

    ``extra`` are pk strings the body links to by some other route — currently
    ``((block refs))`` (services/block_refs). They join the same edge set on
    purpose: pointing at a block IS pointing at the note holding it, and a
    second backlinks panel saying so separately would only split the reader's
    attention.

    Self-links are dropped (a note linking itself would show up in its own
    backlinks panel).
    """
    from application.models.item import Item
    found = list(targets(item.note, index)) + list(extra)
    pks = list(dict.fromkeys(pk for pk in found if pk != str(item.pk)))
    before = {str(r.pk if hasattr(r, "pk") else r.id)
              for r in (item._data.get("note_links") or [])}
    if before == set(pks):
        return False
    item.note_links = list(Item.objects(pk__in=pks)) if pks else []
    return True


def _crumb(block, limit: int = 40) -> str:
    """One step of a reference's breadcrumb, as prose.

    A path should read "Projects › Q3", not "## Projects › ### Q3" — the
    markers are how the heading is written, not what it is called.
    """
    from application.views.notes import _note_preview
    return _note_preview(block.body, limit) or "…"


def _points_at(content: str, title: str, block_ids, pk: str = "") -> bool:
    """Does this block's text name ``item`` — by title, by one of its blocks,
    or by its address in this app?"""
    for m in WIKILINK_RE.finditer(content or ""):
        if (m.group(1) or "").strip().lower() == title:
            return True
    if pk and APP_LINK_RE.search(content or ""):
        for m in APP_LINK_RE.finditer(content):
            if m.group(1) == pk:
                return True
    if block_ids and "((" in (content or ""):
        from application.services.block_refs import BLOCKREF_RE
        for m in BLOCKREF_RE.finditer(content):
            if m.group(1) in block_ids:
                return True
    return False


def linked_references(item, user) -> list:
    """The BLOCKS that point here, grouped by the page they sit on.

    Logseq's Linked References, and the reason it is worth more than a list of
    page names: what you want to know is what the other note SAYS about this
    one, which is the block, read in the place it stands — hence the chain of
    parents above it.

    Both routes count, because to the reader they are the same statement:
    ``[[Title]]`` naming this page, and ``((id))`` naming one of its blocks.

    One indexed query finds the pages (they are exactly the ones carrying this
    item in ``note_links``); the rest is parsing those few bodies.
    """
    from application.models.item import Item
    from application.services import outline
    from application.services.permissions import container_visibility_q

    title = (item.title or "").strip().lower()
    own_ids = set(item.block_ids or [])
    groups = []
    for note in Item.objects(
        container_visibility_q(user) & pages_q() & Q(note_links=item)
    ).only("pk", "title", "note", "parent").order_by("+title"):
        if note.pk == item.pk:
            continue
        hits, claimed = [], set()
        for block, trail in outline.walk_trail(outline.parse(note.note or "")):
            if id(block) in claimed or not _points_at(
                    block.content, title, own_ids, str(item.pk)):
                continue
            # A block renders with its children, so a matching block inside one
            # already shown is part of that answer rather than a second one.
            for kid in outline.walk(block.children):
                claimed.add(id(kid))
            hits.append({"block": block, "trail": [_crumb(b) for b in trail]})
        # A page can be in note_links without a block matching now — the link
        # was written against a title that has since changed. Say the page
        # links here rather than dropping it without a word.
        groups.append({"note": note, "blocks": hits})
    return groups
