/* The OTHER note editor: a note kept as a plain Markdown document.
 *
 * static/js/outliner.js edits a body block by block, because that body is an
 * outline. A note whose ``note_format`` is "markdown" is not one — it is a
 * document somebody wrote in a Markdown editor, and the only thing that keeps
 * it intact is that nothing here ever reshapes it. So this file is deliberately
 * small: it shows the rendered body, opens ONE textarea over the whole thing,
 * and posts that text back unchanged.
 *
 * The save contract is the outliner's, unchanged — the whole body, throttled,
 * against ``base_stamp``, with the same 409 conflict check and the same
 * pagehide beacon. The server route does not know which of the two editors
 * wrote to it, and must not have to.
 *
 * The textarea is ``[name=note]`` inside ``[data-note-id]``, which is all
 * note-paste.js needs for image paste and file drop.
 */
(function () {
  const root = document.getElementById("note-document");
  if (!root) return;

  const noteId = root.dataset.noteId;
  const saveUrl = `/notes/${noteId}/edit`;
  const canEdit = root.dataset.canEdit === "1";
  const view = root.querySelector(".nd-view");
  const src = root.querySelector(".nd-src");
  const titleInput = document.getElementById("note-title");
  const status = document.getElementById("note-save-status");
  const conflictBox = document.getElementById("note-conflict");

  let stamp = root.dataset.stamp || "";
  let savedNote = src.value;
  let savedTitle = titleInput ? titleInput.value : "";
  let editing = false;
  let saveTimer = null, queue = Promise.resolve(), conflicted = false;
  let busy = 0;

  const csrf = () => (document.querySelector('input[name="csrf_token"]') || {}).value || "";

  function setStatus(text, cls) {
    if (!status) return;
    status.textContent = text || "";
    status.className = "small ms-auto " + (cls || "text-muted");
  }

  function dirty() {
    return src.value !== savedNote
      || (titleInput && titleInput.value !== savedTitle);
  }

  // ---- open / close --------------------------------------------------------

  function grow() {
    src.style.height = "auto";
    src.style.height = src.scrollHeight + "px";
  }

  /** Where in the SOURCE the reader just clicked.
   *
   *  A document can be long, and dropping the caret at the end of it because
   *  the rendered view and the text are two different things would make
   *  click-to-edit useless. The rendered element carries the words, so the
   *  words are what locates the line — imperfect where the same sentence
   *  appears twice, and right the rest of the time. */
  function offsetOf(target) {
    const el = target && target.closest
      ? target.closest(".nd-view > *, .nd-view li")
      : null;
    if (!el || el === view) return src.value.length;
    const words = (el.textContent || "").trim().slice(0, 60);
    if (!words) return src.value.length;
    const hit = src.value.indexOf(words.split("\n")[0]);
    return hit < 0 ? src.value.length : hit;
  }

  function open(at) {
    if (!canEdit || editing) return;
    editing = true;
    view.hidden = true;
    src.hidden = false;
    grow();
    src.focus();
    const pos = typeof at === "number" ? at : src.value.length;
    src.selectionStart = src.selectionEnd = pos;
  }

  async function close() {
    if (!editing) return;
    editing = false;
    await save({ render: true });
    src.hidden = true;
    view.hidden = false;
  }

  if (canEdit) {
    view.addEventListener("click", (e) => {
      // A link, a checkbox or an image lightbox is the reader using the page,
      // not asking to write on it.
      if (e.target.closest("a, input, button, .md-lightbox")) return;
      open(offsetOf(e.target));
    });
    src.addEventListener("input", () => {
      grow();
      setStatus("Unsaved changes", "text-warning");
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => save({ render: false }), 1200);
    });
    src.addEventListener("blur", () => close());
    src.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); src.blur(); }
    });
    if (titleInput && !titleInput.readOnly) {
      titleInput.addEventListener("input", () => {
        clearTimeout(saveTimer);
        saveTimer = setTimeout(() => save({ render: false }), 1200);
      });
      titleInput.addEventListener("blur", () => save({ render: false }));
    }
    const toggle = document.getElementById("note-doc-source");
    if (toggle) toggle.addEventListener("click", () => (editing ? close() : open()));
  }

  // ---- save ----------------------------------------------------------------

  /** Chained, so two saves can never be in flight at once: the second would
   *  carry the stamp the first is about to move and invent a conflict. */
  function save(opts) {
    clearTimeout(saveTimer);
    queue = queue.catch(() => {}).then(() => doSave(opts || {}));
    return queue;
  }

  async function doSave(opts, retried) {
    if (!canEdit || conflicted) return true;
    const render = opts.render ? "doc" : "";
    if (!dirty() && !render) { setStatus(""); return true; }
    setStatus("Saving…");
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    if (titleInput) fd.append("title", titleInput.value);
    fd.append("note", src.value);
    fd.append("base_stamp", stamp);
    if (render) fd.append("render", render);
    busy += 1;
    let resp, data;
    try {
      resp = await fetch(saveUrl, {
        method: "POST", body: fd,
        headers: { "X-Requested-With": "XMLHttpRequest", "X-Partial": "1" },
      });
      data = await resp.json().catch(() => null);
    } finally {
      busy -= 1;
    }
    if (resp.status === 409) {
      // Our own save landing late moves the stamp without the text moving —
      // see outliner.js, which carries the same retry for the same reason.
      if (data && data.note === savedNote && !retried) {
        stamp = data.stamp;
        return doSave(opts, true);
      }
      conflicted = true;
      if (conflictBox) conflictBox.classList.remove("d-none");
      setStatus("Not saved — conflict", "text-danger");
      return false;
    }
    if (!data || !data.ok) {
      setStatus("Save failed — your text is still here", "text-danger");
      return false;
    }
    if (titleInput && typeof data.title === "string") {
      if (document.activeElement !== titleInput && titleInput.value !== data.title) {
        titleInput.value = data.title;
        const row = document.querySelector(".nn-row.active .nn-name");
        if (row) row.textContent = data.title;
      }
      savedTitle = data.title;
    }
    savedNote = data.note;
    stamp = data.stamp;
    const html = data.rendered && data.rendered.doc;
    if (html !== undefined) view.innerHTML = html;
    const words = document.getElementById("note-words");
    if (words) words.textContent = data.words;
    const updated = document.getElementById("note-updated");
    if (updated) updated.textContent = "just now";
    if (data.title) document.title = data.title + " · Notes · Cairn";
    setStatus("Saved");
    setTimeout(() => { if (!dirty()) setStatus(""); }, 2000);
    return true;
  }

  // Last-chance save when the tab goes away; fetch does not survive unload.
  window.addEventListener("pagehide", () => {
    if (!canEdit || conflicted || busy || !dirty()) return;
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    if (titleInput) fd.append("title", titleInput.value);
    fd.append("note", src.value);
    fd.append("base_stamp", stamp);
    navigator.sendBeacon(saveUrl, fd);
  });

  // The page around the body (tag edits, link actions) talks to whichever
  // editor is on it through this one name.
  window.CairnNoteBody = {
    save: () => save({ render: !editing }),
    getStamp: () => stamp,
    setStamp: (s) => { if (s) stamp = s; },
    text: () => src.value,
  };
})();
