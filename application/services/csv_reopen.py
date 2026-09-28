"""Undo "complete tasks missing from the CSV" for a time window.

Until the close-missing step of the CSV import was scoped to the project and
ticket system of the file (see ``views.projects.import_csv``), an import
closed every imported task of the user that the file did not list — a Helix
export closed the Jira tickets, a Jira export the Helix ones and those of
other projects. This finds what such runs closed and reopens it.

What identifies them: an imported item (``external_id`` ``csv:…``) of that
user, closed inside the window, and closed the way the import closed it —
before the fix a plain status write (``completed_via`` "user"), after it
"csv-import"; sub-tasks the close cascaded to ("cascade"); and tickets the
import had promoted to a container, which the old code set to the task status
"done" (not a container status, so nothing else produces it). A task the user
closed by hand inside the same window looks the same — which is why this
reports first and writes only when asked.
"""
from application.models.item import Item

_VIA = ("user", "csv-import")


def closed_by_import(user, since, until, source=None) -> list:
    """The items an import run may have closed in ``[since, until]``, oldest
    close first. ``source`` narrows to one system ("jira", "helix", "csv")."""
    base = dict(owner=user, external_id__startswith="csv:")
    if source:
        base["source"] = source
    tasks = list(Item.objects(
        kind="task", status="done", completed_via__in=_VIA,
        completed_at__gte=since, completed_at__lte=until, **base))
    containers = list(Item.objects(
        kind__in=["project", "section"], status="done",
        updated_at__gte=since, updated_at__lte=until, **base))
    found = tasks + containers
    seen = {i.pk for i in found}
    for root in list(found):
        pks = [pk for pk in root._descendant_task_pks() if pk not in seen]
        if not pks:
            continue
        for sub in Item.objects(
                pk__in=pks, status="done", completed_via="cascade",
                completed_at__gte=since, completed_at__lte=until):
            seen.add(sub.pk)
            found.append(sub)
    return sorted(found, key=lambda i: i.completed_at or i.updated_at)


def reopen(items) -> int:
    """Reopen what ``closed_by_import`` found: tasks to "open", containers to
    "active". Saving clears the completion record."""
    n = 0
    for item in items:
        item.status = "open" if item.kind == "task" else "active"
        item.save()
        n += 1
    return n
