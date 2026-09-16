"""Filing a note by PATH — ``Archiv/Agenda/2026/Kunden`` instead of an id.

An importer knows where a note belongs the way a person says it: a chain of
folder names. It does not know the ids, and making it find out — a listing to
look, a POST when the level isn't there, once per level per note — turns an
import into a conversation.

The WebDAV mirror walks a file's directory the same way
(``notes_sync._Round.folder_for``); that one carries a round's path cache and
its dry-run, this one answers a single call.
"""
from application.models.item import Item


def ensure_path(user, path: str):
    """The notes folder at ``path``, creating the levels that are missing.

    ``None`` for an empty path — the top level — so the answer can go straight
    onto ``Item.parent``. Each segment is matched by title under its parent,
    which is what the navigator shows, so an existing tree is reused instead
    of doubled. A trashed folder is not reused: it is on its way out.
    """
    parent = None
    for part in (path or "").split("/"):
        part = part.strip()
        if not part:
            continue
        folder = Item.objects(owner=user, kind="folder", folder_domain="notes",
                              parent=parent, title=part).first()
        if folder is None:
            folder = Item(owner=user, kind="folder", folder_domain="notes",
                          parent=parent, title=part, status="active").save()
        parent = folder
    return parent
