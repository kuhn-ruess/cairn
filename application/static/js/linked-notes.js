/* Expand + inline-edit linked notes on the project detail page.
 *
 * Each linked note (#linked-notes .ln-item) expands to show its rendered
 * Markdown and — when the user may edit it — flips into a textarea that POSTs
 * to notes.edit and swaps the rendered body in place from the JSON response.
 * Handlers are delegated on document so they survive the in-place <main> swap
 * (see the bktodo:refresh-needed pattern).
 */
(function () {
  function csrf() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  function toggle(item) {
    const body = item.querySelector(".ln-body");
    const icon = item.querySelector(".ln-toggle i");
    if (!body) return;
    const open = body.classList.toggle("d-none") === false;
    if (icon) icon.className = open ? "bi bi-chevron-down" : "bi bi-chevron-right";
  }

  function startEdit(item) {
    const body = item.querySelector(".ln-body");
    if (body) body.classList.remove("d-none");  // ensure expanded
    const icon = item.querySelector(".ln-toggle i");
    if (icon) icon.className = "bi bi-chevron-down";
    item.querySelector(".ln-view")?.classList.add("d-none");
    const wrap = item.querySelector(".ln-edit-wrap");
    if (wrap) {
      wrap.classList.remove("d-none");
      const ta = wrap.querySelector(".ln-editor");
      if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
    }
  }

  function cancelEdit(item) {
    const ta = item.querySelector(".ln-editor");
    if (ta) ta.value = ta.defaultValue;  // back to last-saved text
    item.querySelector(".ln-edit-wrap")?.classList.add("d-none");
    item.querySelector(".ln-view")?.classList.remove("d-none");
  }

  function saveEdit(item) {
    const ta = item.querySelector(".ln-editor");
    if (!ta) return;
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    fd.append("note", ta.value);
    fetch(item.dataset.noteUrl, {
      method: "POST", body: fd, credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest", "X-Partial": "1" },
    })
      .then((r) => { if (!r.ok) throw new Error("save failed"); return r.json(); })
      .then((data) => {
        const view = item.querySelector(".ln-view");
        if (view) view.innerHTML = data.html
          || '<span class="text-muted fst-italic">Empty note.</span>';
        ta.defaultValue = data.note != null ? data.note : ta.value;
        item.querySelector(".ln-edit-wrap")?.classList.add("d-none");
        view?.classList.remove("d-none");
      })
      .catch(() => window.alert("Saving the note failed."));
  }

  document.addEventListener("click", (ev) => {
    const item = ev.target.closest("#linked-notes .ln-item");
    if (!item) return;
    if (ev.target.closest(".ln-toggle")) { ev.preventDefault(); toggle(item); return; }
    if (ev.target.closest(".ln-edit"))   { ev.preventDefault(); startEdit(item); return; }
    if (ev.target.closest(".ln-cancel")) { ev.preventDefault(); cancelEdit(item); return; }
    if (ev.target.closest(".ln-save"))   { ev.preventDefault(); saveEdit(item); }
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" || !(ev.metaKey || ev.ctrlKey)) return;
    const ta = ev.target.closest && ev.target.closest("#linked-notes .ln-editor");
    if (!ta) return;
    ev.preventDefault();
    saveEdit(ta.closest(".ln-item"));
  });
})();
