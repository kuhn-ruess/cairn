/* Image-paste + link-drop support for note textareas.
 *
 * Dragging a link (e.g. an Apple Mail message → a ``message://`` URL) into a
 * note inserts a Markdown link "[subject](url)" rather than bare text — see the
 * drag-and-drop section at the bottom.
 *
 * Listens on textarea[name="note"]; when the clipboard carries an image we
 * compress it in-browser (resize + re-encode to WebP/JPEG) and store it inline
 * on the item via /tasks/<id>/note-image. The note then carries only a short
 * reference URL — never a base64 blob — and nothing touches the filesystem.
 *
 * For an as-yet-unsaved task (no id, e.g. the new-task inspector) there's no
 * item to attach to, so we fall back to embedding a compressed data: URI.
 */
(function () {
  const MAX_DIM = 1600;
  const QUALITY = 0.72;

  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  // Resolve the note-image upload URL for a textarea, or null when there's no
  // saved item to attach to yet (create form → fall back to an inline data URI).
  // Notes live under #note-detail[data-note-id]; tasks/projects carry the id in
  // their surrounding form's action (…/tasks/<id>/…).
  function uploadUrlFor(textarea, endpoint) {
    endpoint = endpoint || "note-image";
    const noteWrap = textarea.closest("[data-note-id]");
    if (noteWrap && noteWrap.dataset.noteId) {
      return `/notes/${noteWrap.dataset.noteId}/${endpoint}`;
    }
    const form = textarea.closest("form");
    const action = (form && form.getAttribute("action")) || "";
    const m = action.match(/\/tasks\/([0-9a-fA-F]{12,})(?:\/|$)/);
    return m ? `/tasks/${m[1]}/${endpoint}` : null;
  }

  // Refresh every storage meter on the page from an upload's {used_mb, pct,
  // level} payload, so the "x / 16 MB" bar moves without a reload.
  function updateStorageMeters(storage) {
    if (!storage) return;
    document.querySelectorAll(".js-storage-meter").forEach((meter) => {
      const bar = meter.querySelector(".js-storage-bar");
      const text = meter.querySelector(".js-storage-text");
      if (bar) {
        bar.style.width = storage.pct + "%";
        bar.className = bar.className.replace(/\bbg-\w+\b/, "bg-" + storage.level);
      }
      if (text) text.textContent = `${storage.used_mb.toFixed(1)} / ${storage.max_mb} MB`;
    });
  }

  function loadImage(blob) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(blob);
      const img = new Image();
      img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("could not read image")); };
      img.src = url;
    });
  }

  function scaledCanvas(img) {
    let { width, height } = img;
    const scale = Math.min(1, MAX_DIM / Math.max(width, height));
    width = Math.max(1, Math.round(width * scale));
    height = Math.max(1, Math.round(height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.getContext("2d").drawImage(img, 0, 0, width, height);
    return canvas;
  }

  function canvasToBlob(canvas) {
    return new Promise((resolve) => {
      canvas.toBlob((b) => {
        if (b && b.type === "image/webp") return resolve(b);
        // Browser can't encode WebP — fall back to JPEG for real compression.
        canvas.toBlob((j) => resolve(j), "image/jpeg", QUALITY);
      }, "image/webp", QUALITY);
    });
  }

  function canvasToDataUrl(canvas) {
    let url = canvas.toDataURL("image/webp", QUALITY);
    if (!url.startsWith("data:image/webp")) url = canvas.toDataURL("image/jpeg", QUALITY);
    return url;
  }

  async function uploadToItem(uploadUrl, blob) {
    const fd = new FormData();
    fd.append("file", blob, "pasted.webp");
    fd.append("csrf_token", csrfToken());
    const resp = await fetch(uploadUrl, { method: "POST", body: fd });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || ("upload failed: " + resp.status));
    return data; // { markdown, warning? }
  }

  function toast(msg, kind) {
    const el = document.createElement("div");
    el.className = `alert alert-${kind} position-fixed top-0 start-50 translate-middle-x mt-3 shadow`;
    el.style.zIndex = 2100;
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 4500);
  }

  function insertAtCursor(textarea, snippet) {
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    textarea.value = textarea.value.substring(0, start) + snippet + textarea.value.substring(end);
    const pos = start + snippet.length;
    textarea.selectionStart = textarea.selectionEnd = pos;
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function replacePlaceholder(textarea, placeholder, replacement) {
    textarea.value = textarea.value.replace(placeholder, replacement);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  }

  async function handleImage(textarea, blob) {
    const placeholder = `![compressing…](pasted-${Date.now()})`;
    insertAtCursor(textarea, placeholder);
    try {
      const img = await loadImage(blob);
      const canvas = scaledCanvas(img);
      const uploadUrl = uploadUrlFor(textarea);
      if (uploadUrl) {
        const data = await uploadToItem(uploadUrl, await canvasToBlob(canvas));
        replacePlaceholder(textarea, placeholder, data.markdown);
        updateStorageMeters(data.storage);
        if (data.warning) toast(data.warning, "warning");
      } else {
        // Unsaved item: nothing to attach to yet — embed compressed inline.
        replacePlaceholder(textarea, placeholder, `![pasted image](${canvasToDataUrl(canvas)})`);
      }
    } catch (err) {
      replacePlaceholder(textarea, placeholder, `<!-- image paste failed: ${err.message} -->`);
      toast("Image paste failed: " + err.message, "danger");
    }
  }

  // Upload an arbitrary file (Numbers/Word/PDF/…) to the item and insert a
  // Markdown download link. Behaves like image paste otherwise; needs a saved
  // item (there's nowhere to attach to on an as-yet-unsaved create form).
  async function uploadFile(uploadUrl, file) {
    const fd = new FormData();
    fd.append("file", file, file.name || "attachment");
    fd.append("csrf_token", csrfToken());
    const resp = await fetch(uploadUrl, { method: "POST", body: fd });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || ("upload failed: " + resp.status));
    return data; // { markdown, storage, warning? }
  }

  async function handleFile(textarea, file) {
    const uploadUrl = uploadUrlFor(textarea, "note-file");
    if (!uploadUrl) { toast("Save the item first, then attach files.", "warning"); return; }
    const safe = (file.name || "file").replace(/[[\]]/g, "");
    const placeholder = `[uploading ${safe}…](uploading-${Date.now()})`;
    insertAtCursor(textarea, placeholder);
    try {
      const data = await uploadFile(uploadUrl, file);
      replacePlaceholder(textarea, placeholder, data.markdown);
      updateStorageMeters(data.storage);
      if (data.warning) toast(data.warning, "warning");
    } catch (err) {
      replacePlaceholder(textarea, placeholder, `<!-- attachment failed: ${err.message} -->`);
      toast("Attachment failed: " + err.message, "danger");
    }
  }

  function onPaste(e) {
    const textarea = e.target;
    if (!textarea.matches('textarea[name="note"]')) return;
    const items = (e.clipboardData && e.clipboardData.items) || [];
    for (const item of items) {
      if (item.kind !== "file") continue;
      e.preventDefault();
      const blob = item.getAsFile();
      if (!blob) return;
      if (item.type && item.type.startsWith("image/")) handleImage(textarea, blob);
      else handleFile(textarea, blob);
      return;
    }
  }

  document.addEventListener("paste", onPaste, true);

  // ----- Drag-and-drop a link into a note -----------------------------------
  // Dragging an Apple Mail message (or any link) into a note textarea inserts
  // a Markdown link instead of the bare subject text. Mail provides a
  // ``message://<id>`` URL in the drag; we keep that as the target and use the
  // dragged subject as the label, so the note carries a clickable reference
  // that reopens the original mail. Works for the task-detail / inspector
  // notes (``textarea[name="note"]``) and the project note (``.pn-editor``).

  function noteTextareaFor(target) {
    if (!target || !target.closest) return null;
    return target.closest('textarea[name="note"], textarea.pn-editor');
  }

  function firstLink(str) {
    return (str || "")
      .split(/\r?\n/)
      .map((s) => s.trim())
      .find((s) => /^(message:|mailto:|https?:)/i.test(s)) || "";
  }

  // Build "[label](url)" from a drop's DataTransfer, or null when there's no
  // URL to link (so plain-text drops fall through to the native behaviour).
  function linkSnippetFromDrop(dt) {
    const plain = dt.getData("text/plain") || "";
    const url =
      firstLink(dt.getData("text/uri-list") || "") ||
      firstLink(dt.getData("URL") || "") ||
      firstLink(plain);
    if (!url) return null;
    // Label: a Firefox-style "url\ntitle" flavour, else a plain-text line that
    // isn't the URL itself (Mail drops the subject there), else a fallback.
    let label = "";
    const mozParts = (dt.getData("text/x-moz-url") || "").split(/\r?\n/);
    if (mozParts.length >= 2) label = mozParts[1].trim();
    if (!label) {
      label = plain
        .split(/\r?\n/)
        .map((s) => s.trim())
        .find((s) => s && s !== url && !/^(message:|mailto:|https?:)/i.test(s)) || "";
    }
    if (!label) {
      if (/^message:/i.test(url)) label = "Email";
      else {
        try { label = new URL(url).hostname.replace(/^www\./, ""); }
        catch (e) { label = "Link"; }
      }
    }
    // Keep the label from breaking the Markdown link syntax.
    label = label.replace(/\s+/g, " ").replace(/[[\]]/g, "").trim() || "Link";
    return `[${label}](${url})`;
  }

  // A drop only fires on the textarea if the drag was "accepted" in dragover.
  // Only claim drags that carry a URL flavour, so dragging plain text into a
  // note still behaves natively.
  // File drops only attach on the real note textarea (where image paste also
  // works) — the project ``.pn-editor`` only gets the link-drop behaviour, to
  // match where inline images are supported today.
  function fileDropTarget(target) {
    return (target && target.closest) ? target.closest('textarea[name="note"]') : null;
  }

  function onNoteDragOver(e) {
    const types = (e.dataTransfer && e.dataTransfer.types) || [];
    const ta = fileDropTarget(e.target);
    if (ta && Array.from(types).includes("Files") && uploadUrlFor(ta, "note-file")) {
      e.preventDefault();
      return;
    }
    if (!noteTextareaFor(e.target)) return;
    const hasUrl = Array.from(types).some(
      (t) => t === "text/uri-list" || t === "URL" || t === "text/x-moz-url"
    );
    if (hasUrl) e.preventDefault();
  }

  function onNoteDrop(e) {
    const dt = e.dataTransfer;
    if (!dt) return;
    const ta = fileDropTarget(e.target);
    if (ta && dt.files && dt.files.length && uploadUrlFor(ta, "note-file")) {
      e.preventDefault();
      e.stopPropagation();
      ta.focus();
      for (const file of dt.files) {
        if (file.type && file.type.startsWith("image/")) handleImage(ta, file);
        else handleFile(ta, file);
      }
      return;
    }
    const textarea = noteTextareaFor(e.target);
    if (!textarea) return;
    const snippet = linkSnippetFromDrop(dt);
    if (!snippet) return; // not a link drop — let the browser handle it
    e.preventDefault();
    e.stopPropagation();
    textarea.focus();
    insertAtCursor(textarea, snippet);
  }

  document.addEventListener("dragover", onNoteDragOver, true);
  document.addEventListener("drop", onNoteDrop, true);

  // Expose the compression + Mail-link helpers. The EasyMDE note editor swaps
  // the raw textarea for CodeMirror, so the paste/drop listeners above never
  // fire there — its own hooks call into these to get the identical pipeline.
  window.CairnNoteImage = {
    async compress(blob) {
      const img = await loadImage(blob);
      return canvasToBlob(scaledCanvas(img));
    },
    linkSnippetFromDrop: linkSnippetFromDrop,
    updateStorageMeters: updateStorageMeters,
  };
})();
