/* Inline images in rendered markdown (notes, comments, …) are constrained in
 * size via CSS so they never break out of their column. Clicking one opens it
 * full-size in a lightweight overlay. Delegated from document, so it also works
 * for markdown that gets swapped in via AJAX (e.g. the inline note editor).
 */
(function () {
  let overlay = null;

  function ensureOverlay() {
    if (overlay) return overlay;
    overlay = document.createElement("div");
    overlay.className = "md-lightbox";
    overlay.innerHTML = '<img alt="">';
    overlay.addEventListener("click", close);
    document.body.appendChild(overlay);
    return overlay;
  }

  function close() {
    if (overlay) overlay.classList.remove("is-open");
  }

  document.addEventListener("click", (ev) => {
    const img = ev.target.closest(".md-body img");
    if (!img) return;
    ev.preventDefault();
    const o = ensureOverlay();
    o.querySelector("img").src = img.currentSrc || img.src;
    o.classList.add("is-open");
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") close();
  });
})();
