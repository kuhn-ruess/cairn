/* Image-paste support for note textareas.
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

  // Pull the item id out of the surrounding form's action (…/tasks/<id>/…).
  // Returns null for the create form (/tasks/new) — there's no item yet.
  function taskIdFor(textarea) {
    const form = textarea.closest("form");
    const action = (form && form.getAttribute("action")) || "";
    const m = action.match(/\/tasks\/([0-9a-fA-F]{12,})(?:\/|$)/);
    return m ? m[1] : null;
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

  async function uploadToItem(taskId, blob) {
    const fd = new FormData();
    fd.append("file", blob, "pasted.webp");
    fd.append("csrf_token", csrfToken());
    const resp = await fetch(`/tasks/${taskId}/note-image`, { method: "POST", body: fd });
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
      const taskId = taskIdFor(textarea);
      if (taskId) {
        const data = await uploadToItem(taskId, await canvasToBlob(canvas));
        replacePlaceholder(textarea, placeholder, data.markdown);
        if (data.warning) toast(data.warning, "warning");
      } else {
        // Unsaved task: no item to attach to yet — embed compressed inline.
        replacePlaceholder(textarea, placeholder, `![pasted image](${canvasToDataUrl(canvas)})`);
      }
    } catch (err) {
      replacePlaceholder(textarea, placeholder, `<!-- image paste failed: ${err.message} -->`);
      toast("Image paste failed: " + err.message, "danger");
    }
  }

  function onPaste(e) {
    const textarea = e.target;
    if (!textarea.matches('textarea[name="note"]')) return;
    const items = (e.clipboardData && e.clipboardData.items) || [];
    for (const item of items) {
      if (item.type && item.type.startsWith("image/")) {
        e.preventDefault();
        const blob = item.getAsFile();
        if (blob) handleImage(textarea, blob);
        return;
      }
    }
  }

  document.addEventListener("paste", onPaste, true);
})();
