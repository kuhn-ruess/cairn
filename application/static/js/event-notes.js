/* Notes for a calendar event OR a task, from the day / plan / list views.
 *
 * Each event/task renders a ``.event-notes-btn`` carrying its identity in
 * data-* attributes (see tasks/_event_note_macros.html). Clicking it opens a
 * small panel listing notes already linked and a "New note" action that
 * creates a note pre-linked to the event/task and opens it.
 *
 *   - Events link via Item.linked_events (keyed by uid):
 *       GET /notes/for-event?uid=…   POST /notes/new-for-event
 *   - Tasks link via the symmetric Item.related field:
 *       GET /tasks/<id>/related-notes  POST /tasks/<id>/new-note
 *
 * Loaded globally and delegated on document, so it keeps working after a
 * main-swap navigation (inline view scripts would not — see project notes).
 */
(function () {
  let panel = null;
  let openerBtn = null;

  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  // Build the per-target context (list/create endpoints + create payload) from
  // the button's data-* attributes. Tasks take priority over event fields.
  function ctxFor(btn) {
    const d = btn.dataset;
    if (d.taskId) {
      return {
        headTitle: d.taskTitle || "Task",
        listUrl: "/tasks/" + encodeURIComponent(d.taskId) + "/related-notes",
        createUrl: "/tasks/" + encodeURIComponent(d.taskId) + "/new-note",
        createFields: { title: d.taskTitle || "" },
      };
    }
    if (d.eventUid) {
      return {
        headTitle: d.eventTitle || "Event",
        listUrl: "/notes/for-event?uid=" + encodeURIComponent(d.eventUid),
        createUrl: "/notes/new-for-event",
        createFields: {
          uid: d.eventUid,
          calendar_pk: d.calendarPk || "",
          title: d.eventTitle || "",
          start: d.eventStart || "",
          all_day: d.eventAllday || "",
          url: d.eventUrl || "",
        },
      };
    }
    return null;
  }

  function closePanel() {
    if (panel) { panel.remove(); panel = null; }
    openerBtn = null;
    document.removeEventListener("keydown", onKeydown, true);
  }

  function onKeydown(e) {
    if (e.key === "Escape") closePanel();
  }

  function position(el, btn) {
    const r = btn.getBoundingClientRect();
    let left = Math.min(r.left, window.innerWidth - el.offsetWidth - 8);
    left = Math.max(8, left);
    let top = r.bottom + 4;
    if (top + el.offsetHeight > window.innerHeight - 8) {
      top = Math.max(8, r.top - el.offsetHeight - 4);  // flip above
    }
    el.style.left = left + "px";
    el.style.top = top + "px";
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function render(cx, notes) {
    panel.textContent = "";
    panel.appendChild(el("div", "enp-head", cx.headTitle));

    if (notes.length) {
      notes.forEach((n) => {
        const a = el("a", "enp-item");
        a.href = n.url;
        a.appendChild(el("i", "bi bi-journal-text"));
        a.appendChild(el("span", null, n.title));
        panel.appendChild(a);
      });
    } else {
      panel.appendChild(el("div", "enp-empty", "No notes linked yet."));
    }

    const add = el("button", "enp-new");
    add.type = "button";
    add.innerHTML = '<i class="bi bi-plus-lg"></i> New note';
    add.addEventListener("click", () => createNote(cx, add));
    panel.appendChild(add);

    if (openerBtn) position(panel, openerBtn);
  }

  async function createNote(cx, btn) {
    btn.disabled = true;
    btn.innerHTML = '<i class="bi bi-hourglass-split"></i> Creating…';
    const fd = new FormData();
    Object.entries(cx.createFields).forEach(([k, v]) => fd.append(k, v));
    fd.append("csrf_token", csrfToken());
    try {
      const resp = await fetch(cx.createUrl, { method: "POST", body: fd });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.url) throw new Error(data.error || "could not create note");
      window.location.href = data.url;  // open the new note to start writing
    } catch (err) {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-plus-lg"></i> New note';
      alert("Could not create note: " + err.message);
    }
  }

  async function openPanel(btn) {
    const reopen = openerBtn === btn;
    closePanel();
    if (reopen) return;  // second click on same button = toggle closed

    const cx = ctxFor(btn);
    if (!cx) return;
    openerBtn = btn;
    panel = el("div", "event-notes-panel");
    panel.appendChild(el("div", "enp-empty", "Loading…"));
    document.body.appendChild(panel);
    position(panel, btn);
    document.addEventListener("keydown", onKeydown, true);

    let notes = [];
    try {
      const resp = await fetch(cx.listUrl);
      const data = await resp.json();
      notes = data.notes || [];
    } catch (e) { /* show the create option anyway */ }
    if (panel && openerBtn === btn) render(cx, notes);
  }

  // Delegated: button click opens/toggles; any outside click closes.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest && e.target.closest(".event-notes-btn");
    if (btn) {
      e.preventDefault();
      e.stopPropagation();
      openPanel(btn);
      return;
    }
    if (panel && !e.target.closest(".event-notes-panel")) closePanel();
  }, true);

  window.addEventListener("resize", closePanel);
})();
