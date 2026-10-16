/* A task's page on a phone: "Task" (note, subtasks, comments) and "Details"
   (the inspector) as two tabs, so the inspector is one tap away instead of
   at the end of a long scroll. From 768px up the tabs are hidden and both
   panes show side by side or stacked as before; the attribute is then
   ignored by the CSS.

   Delegated on document, because an inspector save can swap <main>. The
   chosen tab survives that swap and a reload of the same task
   (sessionStorage, per task). */
(function () {
  "use strict";
  const KEY = "cairn.taskPane:";

  function apply(pane) {
    const panes = document.querySelector(".task-detail-panes");
    if (!panes) return;
    panes.setAttribute("data-pane", pane);
    document.querySelectorAll("[data-task-pane]").forEach((b) => {
      b.setAttribute("aria-selected", b.dataset.taskPane === pane ? "true" : "false");
    });
  }

  function restore() {
    let pane = "body";
    try { pane = sessionStorage.getItem(KEY + location.pathname) || "body"; } catch (e) {}
    apply(pane);
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-task-pane]");
    if (!btn) return;
    const pane = btn.dataset.taskPane;
    apply(pane);
    try { sessionStorage.setItem(KEY + location.pathname, pane); } catch (e2) {}
    window.scrollTo({ top: 0 });
  });
  document.addEventListener("bktodo:refresh-needed", () => setTimeout(restore, 0));
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", restore);
  else restore();
})();
