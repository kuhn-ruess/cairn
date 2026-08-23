/* Capture — "the text I just marked becomes tasks / a note".
   Drives the two dialogs in layout/_capture_modals.html and posts them to
   /capture/*. Callers (scratchpad.js, the notes detail page) hand over the
   marked text and get told what was created:

     CairnCapture.tasks(text, { onDone })   // → task dialog
     CairnCapture.note(text, { onDone })    // → note dialog

   ``onDone`` runs only after the server confirmed, so a caller that removes
   the converted text can do so knowing it landed somewhere. */
(function () {
  const PREVIEW_LINES = 12;

  function csrf() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  function toast(html, kind) {
    const el = document.createElement("div");
    el.className = `alert alert-${kind} position-fixed top-0 start-50 translate-middle-x mt-3 shadow`;
    el.style.zIndex = 2100;
    el.innerHTML = html;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 6000);
  }

  // The lines that will become tasks — the same "non-empty line" rule the
  // server parses with, so the count in the dialog is the count you get.
  function taskLines(text) {
    return (text || "").split("\n").filter((l) => l.trim());
  }

  function preview(el, text) {
    if (!el) return;
    const lines = (text || "").split("\n");
    el.textContent = lines.slice(0, PREVIEW_LINES).join("\n") +
      (lines.length > PREVIEW_LINES ? `\n… (+${lines.length - PREVIEW_LINES} more)` : "");
  }

  async function post(url, payload) {
    const resp = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify(payload),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || `Failed (${resp.status})`);
    return data;
  }

  // One dialog, one pending conversion: opening a second one replaces the
  // first, so the buttons always act on the text last handed over.
  let pending = null;

  function modal(id) {
    const el = document.getElementById(id);
    if (!el || typeof bootstrap === "undefined") return null;
    return bootstrap.Modal.getOrCreateInstance(el);
  }

  function run(btn, work) {
    if (!btn || !pending) return;
    btn.disabled = true;
    work()
      .then(() => { if (pending.onDone) pending.onDone(); })
      .catch((err) => toast(err.message, "danger"))
      .finally(() => { btn.disabled = false; pending = null; });
  }

  document.addEventListener("click", (ev) => {
    const tasksBtn = ev.target.closest("#captureTasksGo");
    if (tasksBtn) {
      const sel = document.getElementById("captureTasksProject");
      run(tasksBtn, async () => {
        const data = await post("/capture/tasks", {
          text: pending.text, project_id: sel ? sel.value : "",
        });
        const s = data.stats || {};
        const t = data.target || {};
        modal("captureTasksModal") && modal("captureTasksModal").hide();
        toast(`Created <strong>${s.tasks_created || 0}</strong> task(s)` +
              (s.tasks_updated ? `, updated <strong>${s.tasks_updated}</strong>` : "") +
              ` in <a href="${t.url}" class="alert-link">${t.label}</a>.`, "success");
      });
      return;
    }

    const noteBtn = ev.target.closest("#captureNoteGo");
    if (noteBtn) {
      const title = document.getElementById("captureNoteTitle");
      const folder = document.getElementById("captureNoteFolder");
      run(noteBtn, async () => {
        const data = await post("/capture/note", {
          text: pending.text,
          title: title ? title.value.trim() : "",
          folder_id: folder ? folder.value : "",
        });
        modal("captureNoteModal") && modal("captureNoteModal").hide();
        toast(`Note <a href="${data.url}" class="alert-link">${data.title}</a> created.`,
              "success");
      });
    }
  });

  window.CairnCapture = {
    /** Ask for a target project, then turn ``text`` into tasks. */
    tasks(text, opts) {
      opts = opts || {};
      if (!(text || "").trim()) { toast("Nothing marked to convert.", "warning"); return; }
      pending = { text, onDone: opts.onDone };
      const count = document.getElementById("captureTasksCount");
      if (count) count.textContent = taskLines(text).length;
      preview(document.getElementById("captureTasksPreview"), text);
      const m = modal("captureTasksModal");
      if (m) m.show();
    },

    /** Ask for a title + folder, then turn ``text`` into a note. */
    note(text, opts) {
      opts = opts || {};
      if (!(text || "").trim()) { toast("Nothing to convert.", "warning"); return; }
      pending = { text, onDone: opts.onDone };
      const title = document.getElementById("captureNoteTitle");
      if (title) title.value = opts.title || "";
      preview(document.getElementById("captureNotePreview"), text);
      const m = modal("captureNoteModal");
      if (m) m.show();
    },
  };
})();
