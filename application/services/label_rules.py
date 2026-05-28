"""Apply tag-driven status rules to an Item.

Each ``Tag`` may carry a ``maps_to_status`` value. When a task picks
up such a tag (manually in the inspector, or via an importer), its
``status`` is forced to that value so the user doesn't have to keep
two fields in sync.

First match wins — tags are checked in the order they appear on the
task. The caller is responsible for saving the item afterwards.
"""
from __future__ import annotations


TASK_STATUSES = {
    "open", "next", "waiting", "scheduled", "someday", "done", "dropped",
}


def apply_label_rules(item, user) -> bool:
    """Return True if the item's status changed; caller should save it."""
    if not item or getattr(item, "kind", None) != "task":
        return False
    if not item.tags:
        return False
    for name in item.tags:
        tag = user.tag_by_name(name)
        if not tag or not tag.maps_to_status:
            continue
        target = tag.maps_to_status
        if target not in TASK_STATUSES:
            continue
        if item.status != target:
            item.status = target
            # ``completed_at`` is bookkeeping the model maintains on
            # the next save, no need to set it here.
            return True
        return False
    return False
