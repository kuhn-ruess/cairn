/* Drag-to-nest for the tag list.
 *
 * Drop a tag on another tag → new parent.
 * Drop on the dedicated "Top-level" zone at the bottom → un-nest.
 *
 * Uses HTML5 native drag/drop since the tag list is alphabetical
 * (no reorder semantics needed). Reloads the page on success so
 * the new indentation reflects immediately.
 */
(function () {
  const root = document.querySelector(".tag-tree");
  if (!root) return;

  const csrfEl = document.querySelector('input[name="csrf_token"]');
  const csrf = () => (csrfEl ? csrfEl.value : "");
  let dragName = null;
  let lastTarget = null;

  function clearHover() {
    root.querySelectorAll(".tag-drop-hover").forEach((el) =>
      el.classList.remove("tag-drop-hover"));
    lastTarget = null;
  }

  root.addEventListener("dragstart", (ev) => {
    const li = ev.target.closest(".tag-row");
    if (!li || !li.dataset.tagName) return;
    dragName = li.dataset.tagName;
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", dragName);
    li.classList.add("tag-row-dragging");
  });

  root.addEventListener("dragend", (ev) => {
    const li = ev.target.closest(".tag-row");
    if (li) li.classList.remove("tag-row-dragging");
    dragName = null;
    clearHover();
  });

  root.addEventListener("dragover", (ev) => {
    if (!dragName) return;
    const target = ev.target.closest(".tag-row[data-tag-name], .tag-drop-toplevel");
    if (!target) return;
    if (target.dataset && target.dataset.tagName === dragName) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "move";
    if (lastTarget !== target) {
      clearHover();
      target.classList.add("tag-drop-hover");
      lastTarget = target;
    }
  });

  root.addEventListener("dragleave", (ev) => {
    // Only clear when leaving the list entirely.
    if (!ev.relatedTarget || !root.contains(ev.relatedTarget)) {
      clearHover();
    }
  });

  root.addEventListener("drop", async (ev) => {
    if (!dragName) return;
    const target = ev.target.closest(".tag-row[data-tag-name], .tag-drop-toplevel");
    if (!target) return;
    if (target.dataset && target.dataset.tagName === dragName) return;
    ev.preventDefault();
    clearHover();
    const newParent = target.dataset.toplevel ? "" :
      (target.dataset.tagName || "");
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    fd.append("parent", newParent);
    try {
      const resp = await fetch(
        `/tags/${encodeURIComponent(dragName)}/move`,
        { method: "POST", body: fd }
      );
      if (!resp.ok) {
        const j = await resp.json().catch(() => ({}));
        alert(j.error || `Move failed (${resp.status}).`);
        return;
      }
      window.location.reload();
    } catch (err) {
      alert("Move failed (network).");
    }
  });
})();
