"""Writing a note's body — and everything that has to move with it.

``Item.note`` is one Markdown string, but three things are DERIVED from it
and go stale the moment it is written by someone who forgets them:

* the block-id inventory (``block_refs``) — or a block just referenced by
  ``((id))`` cannot be found,
* the wiki/block link index (``note_links``) — or the backlinks under the
  target note are wrong,
* the note's own title, while ``title_auto`` says nobody has claimed it.

The editor's save route did all three inline. The API writes the same notes,
so the rule lives here and there is one of it — a second one is how the two
would drift apart. Saving stays the caller's: the web route has a version
stamp to check first, the API does not.
"""


def apply_body(item, text, user) -> dict:
    """Put ``text`` in the note and bring the derived parts back in step.

    Returns the wiki title index when one had to be built — the editor renders
    the block it just wrote with it, an API caller ignores it."""
    from application.services import block_refs, note_links

    # multipart/form-data normalises every newline to CRLF. That was invisible
    # while a note was one long string; an outline is read line by line, so a
    # stray "\r" would ride along inside every block's text.
    item.note = (text or "").replace("\r\n", "\n").replace("\r", "\n")

    # The id:: inventory is a parse of this one body, no query — it has to be
    # current or a block someone just referenced can't be found.
    block_refs.rebuild(item)

    # The link lookups ARE queries, so only pay for them when there is
    # something to resolve — the editor autosaves every few seconds.
    index = {}
    body = item.note or ""
    if ("[[" in body or "((" in body or "](/notes/" in body
            or item._data.get("note_links")):
        index = note_links.title_index(user)
        note_links.rebuild(item, index,
                           extra=block_refs.referenced_note_ids(item, user))

    # A name nobody has claimed follows the body. Folders are left out: a
    # folder's name is its place in the tree, and renaming the container while
    # you write inside it would be a surprise, not a convenience.
    if item.title_auto and item.kind == "note":
        from application.views.notes import _derived_title
        item.title = _derived_title(item) or "Untitled note"
    return index


def apply_title(item, title) -> None:
    """Naming it makes the name the writer's; emptying the field hands the
    name back to the first block."""
    title = (title or "").strip()
    item.title_auto = not title
    if title:
        item.title = title
    if not item.title:
        item.title = ("Untitled folder" if item.kind == "folder"
                      else "Untitled note")
