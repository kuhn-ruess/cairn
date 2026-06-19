/* Inline project-note create/edit — skip the inspector.
 *
 * On the project detail page the note lives in #project-note. Clicking the
 * pencil (or the "Add note" button when empty) flips the card into a textarea;
 * Save POSTs to projects.update_note and repaints <main> in place. All handlers
 * are delegated on document so they survive the in-place sidebar/main refresh.
 */
(function () {
  function csrf() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  function startEdit(box) {
    box.classList.add("pn-editing");
    const ta = box.querySelector(".pn-editor");
    if (ta) {
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);
    }
  }

  function cancelEdit(box) {
    const ta = box.querySelector(".pn-editor");
    if (ta) ta.value = ta.defaultValue;   // back to the last-saved text
    box.classList.remove("pn-editing");
  }

  function saveNote(box) {
    const ta = box.querySelector(".pn-editor");
    if (!ta) return;
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    fd.append("note", ta.value);
    fetch(box.dataset.noteUrl, {
      method: "POST",
      body: fd,
      headers: { "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin",
    })
      .then((r) => { if (!r.ok) throw new Error("save failed"); return r.json(); })
      .then(() => {
        // Repaint main from the server so the card re-renders (rendered
        // markdown, or the "Add note" affordance when cleared).
        document.dispatchEvent(new Event("bktodo:refresh-needed"));
      })
      .catch(() => { window.alert("Saving the note failed."); });
  }

  document.addEventListener("click", (ev) => {
    const box = ev.target.closest("#project-note");
    if (!box) return;
    if (ev.target.closest(".pn-edit-btn")) { ev.preventDefault(); startEdit(box); return; }
    if (ev.target.closest(".pn-cancel"))  { ev.preventDefault(); cancelEdit(box); return; }
    if (ev.target.closest(".pn-save"))    { ev.preventDefault(); saveNote(box); }
  });

  // Ctrl/⌘+Enter inside the editor saves.
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" || !(ev.metaKey || ev.ctrlKey)) return;
    const ta = ev.target.closest && ev.target.closest("#project-note .pn-editor");
    if (!ta) return;
    ev.preventDefault();
    saveNote(ta.closest("#project-note"));
  });
})();
