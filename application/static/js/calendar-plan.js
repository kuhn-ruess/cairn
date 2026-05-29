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
    // Only handle OUR pill drags here. Row drags from the task list are
    // SortableJS-managed and handled by onAdd below — never read
    // dataTransfer here (SortableJS puts the row's text in it, which would
    // be POSTed as a bogus task id).
    const id = draggingId;
    draggingId = null;
    day.classList.remove("cal-drop-hover");
    if (!id) return;
    ev.preventDefault();
    await planTask(id, day.dataset.date);
  });

  async function planTask(id, date) {
    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    fd.append("date", date);
    try {
      const resp = await fetch(`/tasks/${id}/schedule`, {
        method: "POST", body: fd, credentials: "same-origin",
      });
      if (!resp.ok) throw new Error(resp.status);
      window.location.reload();
    } catch (e) {
      alert("Planen fehlgeschlagen (" + e.message + ").");
    }
  }

  // Let the "Unscheduled" list (and any other task list) drop tasks onto a
  // day. Those rows are SortableJS items in the "bktodo-tasks" group, so we
  // make each day a drop target in the same group; onAdd plans the task.
  // (sortable-tasks.js skips its own move handler for calendar-day drops.)
  if (window.Sortable) {
    cal.querySelectorAll(".calendar-day[data-date]").forEach((day) => {
      new Sortable(day, {
        group: {
          name: "bktodo-tasks",
          pull: false,
          put: (_to, _from, dragEl) => !!(dragEl && dragEl.dataset && dragEl.dataset.taskId),
        },
        sort: false,
        draggable: "[data-task-id]",
        // The dragged row is a full-width element; inside a small calendar
        // cell its ghost looks broken. Hide it and highlight the day instead
        // (see .cal-drop-ghost CSS).
        ghostClass: "cal-drop-ghost",
        onAdd: (evt) => {
          const id = evt.item && evt.item.dataset && evt.item.dataset.taskId;
          if (id) planTask(id, day.dataset.date);
          else window.location.reload();
        },
      });
    });
  }
})();
