"""In-app notification helpers (the navbar bell).

Thin functions that append an embedded ``Notification`` to a recipient and
save them. Fired from explicit, user-facing choke points only:

- ``Item.assign_to``  → tell the new assignee  (notify_assigned)
- ``Item.complete``   → tell the delegator their delegate finished it (notify_completed)
- team invite of an existing account → tell the invitee  (notify_team_invite)
- ``@name`` in a comment → tell the mentioned teammate  (notify_mention)

A notification must never break the action that triggered it, so every push
is best-effort (failures are swallowed). ``url_for`` is used for links, which
needs an app context — present on every request path these fire from.
"""
from flask import url_for


def _task_link(task) -> str:
    if not getattr(task, "pk", None):
        return ""
    try:
        return url_for("tasks.detail", task_id=task.pk)
    except Exception:
        # No request to build against — a background sync, the CLI. url_for
        # needs SERVER_NAME for that and raises without it, which used to leave
        # the bell entry linking nowhere. The route is stable; write the path.
        return f"/tasks/{task.pk}"


def _push(recipient, *, kind, text, link="", icon="bi-bell", actor="", item_id=None):
    if recipient is None:
        return
    try:
        recipient.add_notification(
            kind=kind, text=text, link=link or "", icon=icon, actor=actor or "",
            item_id=item_id,
        )
        recipient.save()
    except Exception:
        pass


def notify_assigned(task, *, by_user) -> None:
    """A task was delegated to ``task.assigned_to`` by ``by_user``."""
    assignee = task.assigned_to
    if assignee is None or assignee == by_user:
        return
    _push(
        assignee,
        kind="assigned",
        text=f"{by_user} assigned you “{task.title}”",
        link=_task_link(task),
        icon="bi-person-check",
        actor=str(by_user),
        item_id=task.pk,
    )


def notify_completed(task, *, by_user) -> None:
    """A delegated task was completed — tell whoever delegated it."""
    assigner = task.assigned_by
    if assigner is None or assigner == by_user:
        return
    _push(
        assigner,
        kind="completed",
        text=f"{by_user} completed “{task.title}”",
        link=_task_link(task),
        icon="bi-check2-circle",
        actor=str(by_user),
        item_id=task.pk,
    )


def notify_handed_back(task, *, by_user, to_users) -> None:
    """A delegated task was handed back — tell whoever delegated it.

    The counterpart to :func:`notify_assigned`, and the missing half of the
    round trip: taking your name off a task puts it back in somebody else's
    list, usually with work done on it and a comment nobody has read. Without
    this it reappears silently, which is exactly how a handed-back task sits
    untouched for a week."""
    for recipient in to_users:
        if recipient is None or recipient == by_user:
            continue
        _push(
            recipient,
            kind="unassigned",
            text=f"{by_user} handed “{task.title}” back to you",
            link=_task_link(task),
            icon="bi-arrow-return-left",
            actor=str(by_user),
            item_id=task.pk,
        )


def notify_mention(recipient, *, by_user, task) -> None:
    """``recipient`` was @-mentioned in a comment on ``task`` by ``by_user``."""
    if recipient is None or recipient == by_user:
        return
    _push(
        recipient,
        kind="mention",
        text=f"{by_user} mentioned you in a comment on “{task.title}”",
        link=_task_link(task),
        icon="bi-at",
        actor=str(by_user),
        item_id=task.pk,
    )


def notify_team_invite(recipient, *, team, inviter, link) -> None:
    """An existing account was invited to a team — surface it in-app so they
    don't have to find the emailed link."""
    if recipient is None or recipient == inviter:
        return
    _push(
        recipient,
        kind="team_invite",
        text=f"{inviter} invited you to the team “{team.name}”",
        link=link or "",
        icon="bi-people",
        actor=str(inviter),
    )
