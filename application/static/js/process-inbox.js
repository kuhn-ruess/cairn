/* Process-inbox ↔ inspector glue.
 *
 * The processing screen shows a stripped-down triage form, but sometimes you
 * need the whole ticket — comments, subtasks, attachments, dependencies. The
 * "Open full ticket" button loads it into the normal inspector offcanvas, so
 * nothing new had to be built for the modal itself.
 *
 * What this file adds is the flow part: once the inspector closes, a task that
 * no longer belongs in the inbox (filed under a project, completed, deferred,
 * deleted) has to drop out of the run immediately. Reloading is what makes
 * that happen — tasks.process_inbox re-queries the queue on every GET and
 * already redirects to the next task when the current one has left it, so a
 * reload advances the run for free.
 *
 * Only reload when something actually changed, so closing the inspector after
 * a look-around doesn't cost a round trip. The inspector fires
 * bktodo:refresh-needed after every successful save; its side-forms (complete,
 * delete, convert) hide the panel and reload on their own, which is why they
 * need no handling here.
 *
 * Lives in a static file rather than inline in the template because a <main>
 * swap (bktodo:refresh-needed) wipes inline scripts — hence the document-level
 * listeners and the guard below rather than a page-load check.
 */
(function () {
  const panel = document.getElementById("inspector-panel");
  if (!panel) return;

  let changed = false;

  // Fires on every successful inspector save, from any page — only count it
  // while we're actually processing, or a save made elsewhere would arm a
  // reload that fires the next time the inspector closes here.
  document.addEventListener("bktodo:refresh-needed", () => {
    if (document.querySelector("[data-process-inbox]")) changed = true;
  });

  panel.addEventListener("hidden.bs.offcanvas", () => {
    if (!changed) return;
    changed = false;
    if (document.querySelector("[data-process-inbox]")) window.location.reload();
  });
})();
