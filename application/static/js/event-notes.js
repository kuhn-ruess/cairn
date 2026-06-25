/* Notes for a calendar event, from the day / plan / list views.
 *
 * Each calendar event renders a ``.event-notes-btn`` carrying the event's
 * snapshot in data-* attributes (see tasks/_event_note_macros.html). Clicking
 * it opens a small panel that lists notes already linked to the event and
 * offers "New note" — which creates a note pre-linked to the event and opens
 * it. Linkage + lookup live on the notes blueprint (/notes/for-event,
 * /notes/new-for-event), keyed by the event uid.
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

  function snapshotOf(btn) {
    const d = btn.dataset;
    return {
      uid: d.eventUid || "",
      calendar_pk: d.calendarPk || "",
      title: d.eventTitle || "",
      start: d.eventStart || "",
      all_day: d.eventAllday || "",
      url: d.eventUrl || "",
    };
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
    // Below the button, right-aligned to it, clamped into the viewport.
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

  function render(snap, notes) {
    panel.textContent = "";
    const head = el("div", "enp-head", snap.title || "Event");
    panel.appendChild(head);

    if (notes.length) {
      notes.forEach((n) => {
        const a = el("a", "enp-item");
        a.href = n.url;
        const ic = el("i", "bi bi-journal-text");
        const sp = el("span", null, n.title);
        a.appendChild(ic);
        a.appendChild(sp);
        panel.appendChild(a);
      });
    } else {
      panel.appendChild(el("div", "enp-empty", "No notes linked yet."));
    }

    const add = el("button", "enp-new");
    add.type = "button";
    add.innerHTML = '<i class="bi bi-plus-lg"></i> New note';
    add.addEventListener("click", () => createNote(snap, add));
    panel.appendChild(add);

    if (openerBtn) position(panel, openerBtn);
  }

  async function createNote(snap, btn) {
    btn.disabled = true;
    btn.innerHTML = '<i class="bi bi-hourglass-split"></i> Creating…';
    const fd = new FormData();
    Object.entries(snap).forEach(([k, v]) => fd.append(k, v));
    fd.append("csrf_token", csrfToken());
    try {
      const resp = await fetch("/notes/new-for-event", { method: "POST", body: fd });
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

    const snap = snapshotOf(btn);
    if (!snap.uid) return;
    openerBtn = btn;
    panel = el("div", "event-notes-panel");
    panel.appendChild(el("div", "enp-empty", "Loading…"));
    document.body.appendChild(panel);
    position(panel, btn);
    document.addEventListener("keydown", onKeydown, true);

    let notes = [];
    try {
      const resp = await fetch("/notes/for-event?uid=" + encodeURIComponent(snap.uid));
      const data = await resp.json();
      notes = data.notes || [];
    } catch (e) { /* show the create option anyway */ }
    if (panel && openerBtn === btn) render(snap, notes);
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
