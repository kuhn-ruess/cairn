/* Today List view — drag a "Next actions" pill up into the list to plan it.
 *
 * The single-day List view renders a "Next actions" rail (#today-next) of
 * next-marked tasks that aren't on the plate yet. Dragging one onto the drop
 * strip (#today-next-drop) POSTs /tasks/<id>/schedule with just a date, which
 * sets plan_date to the viewed day so the task joins the list.
 *
 * Every handler is delegated on `document` and resolves its elements at fire
 * time, so it keeps working after a background <main> swap (the list rebinds
 * itself). The Plan timeline's plan-view.js shares the .plan-card-task markup
 * but early-returns when there's no #plan-track, so the two never collide. */
(function () {
  "use strict";

  function csrf() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }
  function zone() { return document.getElementById("today-next-drop"); }

  let dragId = null, dragDate = null;

  document.addEventListener("dragstart", (ev) => {
    const item = ev.target.closest &&
      ev.target.closest(".today-next-item[data-task-id]");
    if (!item) return;
    dragId = item.dataset.taskId;
    const sec = document.getElementById("today-next");
    dragDate = sec ? sec.dataset.date : null;
    if (ev.dataTransfer) {
      ev.dataTransfer.effectAllowed = "move";
      try { ev.dataTransfer.setData("text/plain", dragId); } catch (e) {}
    }
    item.classList.add("is-dragging");
    document.body.classList.add("today-next-dragging");
  });

  document.addEventListener("dragend", (ev) => {
    const item = ev.target.closest &&
      ev.target.closest(".today-next-item");
    if (item) item.classList.remove("is-dragging");
    document.body.classList.remove("today-next-dragging");
    const z = zone();
    if (z) z.classList.remove("is-over");
    dragId = null;
    dragDate = null;
  });

  document.addEventListener("dragover", (ev) => {
    if (!dragId) return;
    const z = ev.target.closest && ev.target.closest("#today-next-drop");
    if (!z) return;
    ev.preventDefault();
    if (ev.dataTransfer) ev.dataTransfer.dropEffect = "move";
    z.classList.add("is-over");
  });

  document.addEventListener("dragleave", (ev) => {
    const z = ev.target.closest && ev.target.closest("#today-next-drop");
    if (z && !z.contains(ev.relatedTarget)) z.classList.remove("is-over");
  });

  document.addEventListener("drop", async (ev) => {
    if (!dragId) return;
    const z = ev.target.closest && ev.target.closest("#today-next-drop");
    if (!z) return;
    ev.preventDefault();
    const id = dragId, date = dragDate;
    if (!date) return;
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    fd.append("date", date);
    try {
      const resp = await fetch(`/tasks/${id}/schedule`, {
        method: "POST",
        body: fd,
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrf(), "X-Requested-With": "XMLHttpRequest" },
      });
      if (!resp.ok) {
        let detail = "";
        try { detail = (await resp.json()).error || ""; }
        catch (e) { detail = (await resp.text()).slice(0, 200); }
        alert(`Couldn't plan that task (${resp.status})${detail ? ": " + detail : ""}.`);
        return;
      }
      // Rebuild <main> so the task moves into the list and drops out of the
      // Next-actions pool (recomputed server-side). Falls back to a reload.
      if (typeof window.bktodoRefreshMain === "function") {
        window.bktodoRefreshMain();
      } else {
        window.location.reload();
      }
    } catch (err) {
      alert("Couldn't plan that task (network).");
    }
  });
})();
