/* Pending attachments for the "create then attach" flow.
 *
 * On a NOT-yet-saved task (quick-add modal, new-task inspector) there's no item
 * id to upload to, so files/images are held client-side as chips. After the
 * task is created, flush(container, taskId) uploads each one to the real item
 * (images compressed via CairnNoteImage, reusing the note-image/note-file
 * endpoints) with append_to_note=1, so the references land in the saved note.
 *
 * Container markup (templates provide it):
 *   <div data-pending-attachments>
 *     <div class="pending-attachments-list"></div>
 *     <label class="btn ..."><input type="file" multiple class="d-none pending-attachments-input"></label>
 *   </div>
 *
 * Exposed as window.PendingAttachments. Document-level delegated so it survives
 * <main>/inspector DOM swaps.
 */
(function () {
  const store = new Map(); // container -> [{file, id}]
  let seq = 0;

  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }
  function isImage(file) {
    return file.type && file.type.startsWith("image/");
  }
  function listEl(container) {
    let l = container.querySelector(".pending-attachments-list");
    if (!l) {
      l = document.createElement("div");
      l.className = "pending-attachments-list";
      container.prepend(l);
    }
    return l;
  }
  function entries(container) {
    return store.get(container) || [];
  }

  function render(container) {
    const l = listEl(container);
    const items = entries(container);
    l.innerHTML = items.map((e) => {
      const icon = isImage(e.file) ? "bi-image" : "bi-paperclip";
      const name = (e.file.name || "image").replace(/[<>&]/g, "");
      return `<span class="badge text-bg-secondary pending-chip me-1 mb-1" data-id="${e.id}">`
        + `<i class="bi ${icon}"></i> ${name} `
        + `<button type="button" class="btn-close btn-close-white pending-chip-remove" `
        + `style="font-size:.6em;vertical-align:middle" aria-label="Remove"></button></span>`;
    }).join("");
  }

  function add(container, file) {
    if (!container || !file) return;
    if (!store.has(container)) store.set(container, []);
    store.get(container).push({ file, id: ++seq });
    render(container);
  }
  function remove(container, id) {
    const items = entries(container).filter((e) => String(e.id) !== String(id));
    store.set(container, items);
    render(container);
  }
  function clear(container) {
    store.set(container, []);
    render(container);
  }
  function count(container) {
    return entries(container).length;
  }

  async function uploadOne(taskId, file) {
    let blob = file, filename = file.name || "attachment";
    let endpoint = "note-file";
    if (isImage(file)) {
      endpoint = "note-image";
      filename = "pasted.webp";
      if (window.CairnNoteImage && window.CairnNoteImage.compress) {
        try { blob = await window.CairnNoteImage.compress(file); } catch (e) { blob = file; }
      }
    }
    const fd = new FormData();
    fd.append("file", blob, filename);
    fd.append("csrf_token", csrfToken());
    fd.append("append_to_note", "1");
    const resp = await fetch(`/tasks/${taskId}/${endpoint}`, {
      method: "POST", body: fd, credentials: "same-origin",
    });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      throw new Error(d.error || ("upload failed: " + resp.status));
    }
    return resp.json().catch(() => ({}));
  }

  // Upload every held file to the now-saved task, in order (the server appends
  // each reference to the note and re-saves, so serial keeps order + avoids
  // racing on the same document).
  async function flush(container, taskId) {
    const items = entries(container);
    for (const e of items) {
      try { await uploadOne(taskId, e.file); }
      catch (err) { console.error("attachment upload failed", err); }
    }
    clear(container);
  }

  // ---- delegated UI wiring ----
  document.addEventListener("change", (ev) => {
    const input = ev.target.closest && ev.target.closest(".pending-attachments-input");
    if (!input) return;
    const container = input.closest("[data-pending-attachments]");
    if (!container) return;
    for (const f of input.files) add(container, f);
    input.value = ""; // allow re-picking the same file
  });
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest && ev.target.closest(".pending-chip-remove");
    if (!btn) return;
    const chip = btn.closest(".pending-chip");
    const container = btn.closest("[data-pending-attachments]");
    if (chip && container) remove(container, chip.dataset.id);
  });

  window.PendingAttachments = { add, remove, clear, count, flush };
})();
