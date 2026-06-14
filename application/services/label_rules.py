"""Apply tag-driven status rules to an Item.

Each ``Tag`` may carry a ``maps_to_status`` value. When a task picks
up such a tag (manually in the inspector, or via an importer), its
``status`` is forced to that value so the user doesn't have to keep
two fields in sync.

First match wins — tags are checked in the order they appear on the
task. The caller is responsible for saving the item afterwards.
"""
from __future__ import annotations

# Single source of truth — derive valid rule targets from the model so a new
# status (e.g. "waiting_reply", "agenda") is automatically accepted. A
# hardcoded copy here once silently rejected new statuses, leaving tickets on
# their importer default ("next") instead of what the rule asked for.
from application.models.item import TASK_STATUS

TASK_STATUSES = set(TASK_STATUS)


def apply_label_rules(item, user, only_tags=None) -> bool:
    """Return True if the item's status changed; caller should save it.

    First tag with a ``maps_to_status`` value wins — iterating in
    ``item.tags`` order. Subsequent tags don't reach this code so the
    order on the task acts as a priority list.

    ``only_tags`` restricts the rule to a subset of tag names (e.g. the
    tags *newly added* in this edit). This is how the inspector keeps a
    status-mapping tag from re-snapping the status on every save: once
    the tag is already on the task, a later manual status change must
    stick. Pass ``None`` (default) to consider every tag — used by the
    importers and the ``apply-label-rules`` CLI re-derive command.
    """
    if not item or getattr(item, "kind", None) != "task":
        return False
    if not item.tags:
        return False
    for name in item.tags:
        if only_tags is not None and name not in only_tags:
            continue
        tag = user.tag_by_name(name)
        if not tag or not tag.maps_to_status:
            continue
        target = tag.maps_to_status
        if target not in TASK_STATUSES:
            continue
        if item.status != target:
            item.status = target
            return True
        return False  # already matches — nothing to do, stop scanning
    return False
