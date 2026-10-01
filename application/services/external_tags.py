"""Shared helper for importer-managed tag fields.

Importers (Zammad, Todoist, CSV) replace ``Item.external_tags`` with
whatever the upstream system says on every sync. ``Item.tags`` is the
user-facing list and is computed as (user-added) ∪ (external_tags), so
manually added tags survive the next sync.

Call ``merge_external_tags`` from inside the importer when applying
the new set of upstream tags.
"""
from __future__ import annotations


def merge_external_tags(
    existing_tags: list[str] | None,
    existing_external: list[str] | None,
    new_external: list[str] | None,
    managed_names: set[str] | None = None,
) -> list[str]:
    """Return the merged ``tags`` list after a sync.

    ``user_added = existing_tags − existing_external`` is preserved in
    order; the new external tags are then appended (deduplicated).

    ``managed_names`` is a set of tag names the upstream system fully owns
    (e.g. status tags driven by a Zammad workflow). Any such name is
    dropped from the preserved set, so it survives only if it's in
    ``new_external``. This self-heals tasks imported before
    ``external_tags`` tracking, where a stale status tag would otherwise
    look "user-added" and never get removed. Plain tags are untouched.
    """
    ext_old = set(existing_external or [])
    managed = set(managed_names or [])
    user_added = [
        t for t in (existing_tags or [])
        if t not in ext_old and t not in managed
    ]
    merged: list[str] = list(user_added)
    for t in (new_external or []):
        if t and t not in merged:
            merged.append(t)
    return merged


def overwrite_group_tags(
    current_tags: list[str] | None,
    group_owned: set[str] | None,
    live_tags: list[str] | None,
) -> list[str]:
    """Replace an importer-group's tags wholesale, leave everything else.

    ``group_owned`` is the set of tag names the importer owns (e.g. every
    tag parented under the "Zammad" group). Any such name on the task is
    dropped unless it's in ``live_tags``; tags outside the group — user
    contexts, other importers — are preserved untouched and in order.

    This is the simple, reliable alternative to ``merge_external_tags``:
    group membership is an explicit, persisted fact (the tag's parent),
    so a stale upstream tag can't masquerade as user-added the way it can
    when the boundary is inferred from an ``external_tags`` snapshot.
    """
    owned = set(group_owned or [])
    live = [t for t in (live_tags or []) if t]
    result = [t for t in (current_tags or []) if t not in owned and t not in live]
    for t in live:
        if t not in result:
            result.append(t)
    return result


def ensure_user_has_tags(user, names, *, group: str | None = None) -> None:
    """Make sure every name in ``names`` exists as a Tag on the user.

    Importers call this so externally-imported labels render with the
    same colour / context affordance as user-created tags rather than
    as bare strings on the task chip strip.

    ``group`` (optional) is the name of a parent tag. If passed, the
    group tag is auto-created on first sight, and every NEW tag from
    ``names`` is placed under it — so a user importing from Jira sees
    "Jira: Status" → "Offen", "Backlog", … in the tag tree by default.
    Existing tags are never re-parented: once the user moves a tag
    around, the next sync respects their choice.
    """
    from application.models.tag import Tag

    new_names = [n for n in (names or []) if n and not user.tag_by_name(n)]
    touched = False
    if group and not user.tag_by_name(group):
        user.tags.append(Tag(name=group, color="#6c757d"))
        touched = True
    for name in new_names:
        user.tags.append(Tag(
            name=name, color="#6c757d",
            parent=(group if group else None),
        ))
        touched = True
    if touched:
        user.save()
