"""In-app notification helpers (the navbar bell).

Thin functions that append an embedded ``Notification`` to a recipient and
save them. Fired from explicit, user-facing choke points only:

- ``Item.assign_to``  → tell the new assignee  (notify_assigned)
- ``Item.complete``   → tell the delegator their delegate finished it (notify_completed)
- team invite of an existing account → tell the invitee  (notify_team_invite)
- ``@name`` in a comment → tell the mentioned teammate  (notify_mention)
- a comment posted by a person → tell the task's own cast  (notify_commented)
- a customer reply on a Zammad ticket whose task is in the trash → tell its
  assignee or owner  (notify_trashed_reply)

A notification must never break the action that triggered it, so every push
is best-effort (failures are swallowed). ``url_for`` is used for links, which
needs an app context — present on every request path these fire from.
"""
import re

from flask import url_for

# ``@name`` in a comment body. Resolved against assignable users only, so a
# stray ``@`` or an email address never notifies a stranger.
_MENTION_RE = re.compile(r"@([\w.\-]+)")


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


def notify_trashed_reply(task) -> None:
    """A ticket whose task sits in the trash got a new reply from outside.

    Goes to the assignee, else the owner: the one who would otherwise have
    seen the reply on the live task. Links to the trash page, because the
    task's own page answers 404 for anything in the trash."""
    recipient = task.assigned_to or task.owner
    _push(
        recipient,
        kind="trashed_reply",
        text=f"New reply on “{task.title}”, which is in the trash",
        link=f"/trash/{task.pk}" if getattr(task, "pk", None) else "",
        icon="bi-trash",
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


def notify_mentions(task, body, *, by_user) -> set:
    """Fire a mention notification for every ``@name`` in a comment body that
    resolves to a teammate, and return the pks told.

    Scoped to assignable users, so a stray ``@`` or an email address never
    notifies a stranger. Only ever called from the two places a PERSON writes
    a comment — an imported thread is never scanned, matching this module's
    "user-facing choke points only" rule.
    """
    if not body or by_user is None:
        return set()
    from application.services.permissions import resolve_assignable_user_by_name
    told = {str(by_user.pk)}          # never notify yourself
    for token in dict.fromkeys(m.lower() for m in _MENTION_RE.findall(body)):
        recipient = resolve_assignable_user_by_name(by_user, token)
        if recipient is None or str(recipient.pk) in told:
            continue
        told.add(str(recipient.pk))
        notify_mention(recipient, by_user=by_user, task=task)
    return told


def notify_commented(task, *, by_user, skip=()) -> None:
    """Somebody commented on ``task`` — tell the people the task already names.

    Deliberately the task's own cast and not everyone who ever wrote in the
    thread: the ASSIGNEE (an answer is usually to them — this is the leg that
    was missing, so a reply to a delegated task sat unread for a day), whoever
    DELEGATED it (``assigned_by``: the other end of that conversation, the half
    ``notify_completed`` already covers), and the OWNER (on a task nobody was
    delegated, a comment otherwise reaches nobody at all).

    ``skip`` carries pks already told another way — being @-mentioned in this
    very comment is one notification, not two — and the author is never told
    about their own comment.
    """
    if by_user is None:
        return
    told = {str(by_user.pk)} | {str(pk) for pk in (skip or ()) if pk}
    for recipient in (task.assigned_to, task.assigned_by, task.owner):
        if recipient is None:
            continue
        key = str(recipient.pk)
        if key in told:
            continue
        told.add(key)
        _push(
            recipient,
            kind="comment",
            text=f"{by_user} commented on “{task.title}”",
            link=_task_link(task),
            icon="bi-chat-left-text",
            actor=str(by_user),
            item_id=task.pk,
        )


def notify_new_comment(task, body, *, by_user) -> None:
    """Everything a freshly posted comment owes people, in one call.

    Both places a person can write one — the comment form and
    ``POST /api/v1/tasks/<id>/comments`` — go through here, so the two cannot
    drift into notifying different sets. Mentions first, because whoever was
    named by hand gets the more specific entry and is then skipped by the
    general one.

    A comment mirrored in from an external system never reaches here: those
    are appended straight to ``Item.comments`` by the importers, not through
    ``add_comment``, and have no app user to notify on behalf of anyway.
    """
    if by_user is None:
        return
    mentioned = notify_mentions(task, body, by_user=by_user)
    notify_commented(task, by_user=by_user, skip=mentioned)
