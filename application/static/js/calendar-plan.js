/* Project calendar — drag a task pill onto another day to (re)plan it.
 *
 * The month grid (.task-calendar) shows tasks on their due/plan date. Dragging
 * a pill onto a day POSTs to /tasks/<id>/schedule, which sets plan_date (and
 * mirrors a Google Calendar event when write-back is on), then the calendar
 * reloads so counts/positions stay correct. */
(function () {
  const cal = document.querySelector(".task-calendar");
  if (!cal) return;

  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  let draggingId = null;

  cal.addEventListener("dragstart", (ev) => {
    const pill = ev.target.closest && ev.target.closest(".calendar-task-pill[data-task-id]");
    if (!pill) return;
    draggingId = pill.dataset.taskId;
    if (ev.dataTransfer) {
      ev.dataTransfer.effectAllowed = "move";
      try { ev.dataTransfer.setData("text/plain", draggingId); } catch (e) {}
    }
    pill.classList.add("dragging");
  });

  cal.addEventListener("dragend", (ev) => {
    const pill = ev.target.closest && ev.target.closest(".calendar-task-pill");
    if (pill) pill.classList.remove("dragging");
    cal.querySelectorAll(".cal-drop-hover").forEach((el) => el.classList.remove("cal-drop-hover"));
    draggingId = null;
  });

  cal.addEventListener("dragover", (ev) => {
    if (!draggingId) return;
    const day = ev.target.closest && ev.target.closest(".calendar-day[data-date]");
    if (!day) return;
    ev.preventDefault();
    if (ev.dataTransfer) ev.dataTransfer.dropEffect = "move";
    cal.querySelectorAll(".cal-drop-hover").forEach((el) => el.classList.remove("cal-drop-hover"));
    day.classList.add("cal-drop-hover");
  });

  cal.addEventListener("drop", async (ev) => {
    const day = ev.target.closest && ev.target.closest(".calendar-day[data-date]");
    if (!day) return;
    ev.preventDefault();
    const id = draggingId || (ev.dataTransfer && ev.dataTransfer.getData("text/plain"));
    draggingId = null;
    day.classList.remove("cal-drop-hover");
    if (!id) return;
    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    fd.append("date", day.dataset.date);
    try {
      const resp = await fetch(`/tasks/${id}/schedule`, {
        method: "POST", body: fd, credentials: "same-origin",
      });
      if (!resp.ok) throw new Error(resp.status);
      window.location.reload();
    } catch (e) {
      alert("Planen fehlgeschlagen (" + e.message + ").");
    }
  });
})();
