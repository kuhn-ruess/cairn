"""Apply tag-driven rules to an Item.

A ``Tag`` may carry two rules, and :func:`apply_label_rules` runs both:

* ``maps_to_status`` — the task's ``status`` is forced to that value, so
  the user doesn't have to keep two fields in sync.
* ``assigns_to`` — the task is handed to that person, so tagging is the
  whole delegation and nobody has to remember the second step.

First match wins for each rule separately — tags are checked in the order
they appear on the task. The caller is responsible for saving the item
afterwards.
"""
from __future__ import annotations

# Single source of truth — derive valid rule targets from the model so a new
# status (e.g. "waiting_reply") is automatically accepted. A
# hardcoded copy here once silently rejected new statuses, leaving tickets on
# their importer default ("next") instead of what the rule asked for.
from application.models.item import TASK_STATUS

TASK_STATUSES = set(TASK_STATUS)


def apply_label_rules(item, user, only_tags=None) -> bool:
    """Run every tag-driven rule. True if anything changed; caller saves.

    ``only_tags`` is passed through to both rules — see them for what a
    newly-added tag may override and what it may not.
    """
    changed = apply_status_rule(item, user, only_tags)
    return apply_assignee_rule(item, user, only_tags) or changed


def apply_status_rule(item, user, only_tags=None) -> bool:
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


def apply_assignee_rule(item, user, only_tags=None) -> bool:
    """Hand the task to the person a tag names. True if the assignee changed.

    First tag with an ``assigns_to`` value wins, in ``item.tags`` order —
    the same priority list the status rule uses.

    ``only_tags`` (the tags added in *this* edit) makes the rule
    authoritative: tagging is how you delegate, so the tag wins over
    whoever the task was assigned to before. Without it — the importers and
    the ``apply-label-rules`` re-derive, which see every tag on every run —
    the rule only FILLS an empty assignee: the tag has been sitting on the
    task for a while, and a background pass must not keep yanking it back
    from whoever it was handed to since.

    A person who has left the team no longer resolves
    (:func:`resolve_assignable_user`), and their tag simply stops firing.
    """
    if not item or getattr(item, "kind", None) != "task":
        return False
    if not item.tags:
        return False
    from application.services.permissions import resolve_assignable_user
    for name in item.tags:
        if only_tags is not None and name not in only_tags:
            continue
        tag = user.tag_by_name(name)
        if not tag or not getattr(tag, "assigns_to", None):
            continue
        if only_tags is None and item.assigned_to is not None:
            return False  # a full scan never re-delegates — see above
        target = resolve_assignable_user(user, tag.assigns_to)
        if target is None:
            continue  # gone from the team: this tag has no rule any more
        if item.assigned_to == target:
            return False  # already there — stop scanning
        item.assign_to(target, by_user=user)
        return True
    return False
