"""Block references — ``((uuid))`` and ``{{embed ((uuid))}}``.

Where :mod:`application.services.note_links` points at a whole note by title,
this points at ONE block by id, across notes. Built the same way, for the same
reason — *expand* when rendering, *rebuild* when saving:

* **rendering** — :func:`expand` rewrites ``((uuid))`` into an ordinary Markdown
  link before the ``markdown`` filter runs, so the sanitiser never has to learn
  a custom syntax. The link's text is the referenced block, so a reference reads
  as what it points at rather than as an opaque id.
* **inventory** — :func:`rebuild` stores every ``id::`` the note contains on
  ``Item.block_ids``. Resolving a page's references is then ONE indexed query
  over the notes that own those blocks, instead of parsing every body in the
  collection to find out where a block lives.

A reference also counts as a link to the note holding the block, so it shows up
in that note's "Linked mentions" — being referenced is being referenced, and a
second panel saying the same thing twice would only split the reader's
attention.

Embeds are whole-block only: ``{{embed ((uuid))}}`` as a block's entire content
renders the target and its children in place (the outline macro does that, since
nested blocks are markup the sanitiser would strip out of a text rewrite). An
embed written into the middle of a sentence degrades to the inline link, which
is the readable half of what was asked for rather than a dead directive.
"""
import re

from mongoengine import Q

# ``((0193f2a1-6c2e-7a10-9f1b-2c4d5e6f7a8b))``. Deliberately narrow: prose is
# full of doubled parentheses, and only something shaped like an id may match.
BLOCKREF_RE = re.compile(r"\(\(([0-9a-fA-F][0-9a-fA-F-]{7,63})\)\)")
# ``{{embed ((uuid))}}``. Written by hand as often as pasted, so every
# reasonable spelling of it counts: spaces inside the braces or none at all,
# any capitalisation. A directive that is only ALMOST right renders as its own
# source text, which reads as the feature doing nothing.
_EMBED = r"\{\{\s*embed\s*\(\(([0-9a-fA-F][0-9a-fA-F-]{7,63})\)\)\s*\}\}"
# As the whole of a block's content: that is when it renders as the block.
EMBED_RE = re.compile(r"^" + _EMBED + r"$", re.I)
# Anywhere at all: mid-sentence it cannot render nested blocks, but it must not
# leave its braces lying in the text either — see :func:`expand`.
EMBED_ANY_RE = re.compile(_EMBED, re.I)

# How much of a referenced block is shown as the link's text.
LABEL_LIMIT = 120


def refs(text: str) -> list:
    """The block ids ``text`` references, de-duplicated, order preserved."""
    out, seen = [], set()
    if not text or "((" not in text:
        return out
    for m in BLOCKREF_RE.finditer(text):
        uid = m.group(1)
        if uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out


def embed_target(content: str) -> str:
    """The id a block embeds, if embedding is ALL this block says. Else ``""``."""
    m = EMBED_RE.match((content or "").strip())
    return m.group(1) if m else ""


def resolve(user, uuids) -> dict:
    """``{uuid: {"note", "block"}}`` for the ids ``user`` may see.

    One query, however many references the page has. Each note that owns any of
    them is parsed once, not once per reference.
    """
    from application.models.item import Item
    from application.services import outline
    from application.services.note_links import pages_q
    from application.services.permissions import container_visibility_q

    uuids = [u for u in dict.fromkeys(uuids or []) if u]
    if not uuids:
        return {}
    out = {}
    for note in Item.objects(
        container_visibility_q(user) & pages_q() & Q(block_ids__in=uuids)
    ).only("pk", "title", "note", "parent"):
        blocks = outline.parse(note.note or "")
        for uid in uuids:
            if uid in out:
                continue
            block = outline.find(blocks, uid)
            if block is not None:
                out[uid] = {"note": note, "block": block}
    return out


def label_for(block) -> str:
    """A referenced block as one line of link text.

    Brackets and parentheses are dropped rather than escaped: this text is
    about to be planted inside ``[…](…)``, where a stray one closes the link
    early and spills the rest of the block into the page.
    """
    text = (block.body or "").split("\n", 1)[0].strip()
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)   # links → their text
    text = re.sub(r"\[\[([^\[\]|]*)(?:\|([^\[\]]*))?\]\]", r"\2\1", text)
    text = re.sub(r"[\[\]()]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "empty block"
    return text[:LABEL_LIMIT] + "…" if len(text) > LABEL_LIMIT else text


def expand(text: str, resolved: dict) -> str:
    """Rewrite ``((…))`` into Markdown links against :func:`resolve`'s map.

    An embed that is not a block's whole content cannot render as one — there
    is no room for a subtree inside a sentence — so it is reduced to the plain
    reference first. It reads as a link rather than leaving ``{{embed`` and
    ``}}`` sitting in the text, which is what "the embed does nothing" looks
    like from the outside.

    An id that resolves to nothing stays visible as the plain word "block
    reference" — quiet, but not silently vanished: a reference whose target was
    deleted is something the reader should be able to notice.
    """
    if not text or "((" not in text:
        return text or ""
    text = EMBED_ANY_RE.sub(lambda m: f"(({m.group(1)}))", text)

    def repl(m):
        hit = resolved.get(m.group(1))
        if not hit:
            return "block reference"
        return (f"[{label_for(hit['block'])}]"
                f"(/notes/{hit['note'].pk}?zoom={m.group(1)})")

    return BLOCKREF_RE.sub(repl, text)


def rebuild(item) -> bool:
    """Refresh ``item.block_ids`` from its body. Does NOT save — the caller is
    already saving. Returns True if the set actually changed."""
    from application.services import outline
    ids = outline.collect_ids(outline.parse(item.note or ""))
    if list(item.block_ids or []) == ids:
        return False
    item.block_ids = ids
    return True


def referenced_note_ids(item, user) -> list:
    """The pk strings of the notes whose blocks ``item`` references.

    Feeds ``note_links.rebuild``, so a block reference lands in the target
    note's "Linked mentions" beside the ``[[Title]]`` ones.
    """
    uuids = refs(item.note or "")
    if not uuids:
        return []
    own = set(item.block_ids or [])
    resolved = resolve(user, [u for u in uuids if u not in own])
    return list(dict.fromkeys(str(hit["note"].pk) for hit in resolved.values()))
