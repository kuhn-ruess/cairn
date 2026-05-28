/* Image-paste support for note textareas.
 *
 * Listens on textarea[name="note"]; when the clipboard carries an
 * image, uploads it to /uploads/note-image and inserts the returned
 * markdown link at the cursor.
 */
(function () {
  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  async function uploadImage(blob) {
    const fd = new FormData();
    fd.append("file", blob, blob.name || "pasted.png");
    fd.append("csrf_token", csrfToken());
    const resp = await fetch("/uploads/note-image", {
      method: "POST",
      body: fd,
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || ("upload failed: " + resp.status));
    }
    return resp.json();
  }

  function insertAtCursor(textarea, snippet) {
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const before = textarea.value.substring(0, start);
    const after = textarea.value.substring(end);
    textarea.value = before + snippet + after;
    const pos = start + snippet.length;
    textarea.selectionStart = textarea.selectionEnd = pos;
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function onPaste(e) {
    const textarea = e.target;
    if (!textarea.matches('textarea[name="note"]')) return;
    const items = (e.clipboardData && e.clipboardData.items) || [];
    for (const item of items) {
      if (item.type && item.type.startsWith("image/")) {
        e.preventDefault();
        const blob = item.getAsFile();
        if (!blob) return;
        const placeholder = `![uploading…](pasted-${Date.now()})`;
        insertAtCursor(textarea, placeholder);
        uploadImage(blob)
          .then((data) => {
            textarea.value = textarea.value.replace(placeholder, data.markdown);
            textarea.dispatchEvent(new Event("input", { bubbles: true }));
          })
          .catch((err) => {
            textarea.value = textarea.value.replace(
              placeholder,
              `<!-- upload failed: ${err.message} -->`
            );
          });
        return;
      }
    }
  }

  document.addEventListener("paste", onPaste, true);
})();
