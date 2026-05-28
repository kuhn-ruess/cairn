/* Intercept the per-section "show / hide blocked" toggle so it updates
 * just that section's block instead of reloading the whole page.
 *
 * Strategy: POST the form, then GET the current page, parse out the
 * matching .section-block[data-section-id] and swap it in. Other
 * sections + scroll position stay put.
 */
(function () {
  function swapSection(sectionId) {
    return fetch(window.location.href, { credentials: "same-origin" })
      .then((r) => r.text())
      .then((html) => {
        const doc = new DOMParser().parseFromString(html, "text/html");
        const fresh = doc.querySelector(
          `.section-block[data-section-id="${sectionId}"]`
        );
        const current = document.querySelector(
          `.section-block[data-section-id="${sectionId}"]`
        );
        if (fresh && current) {
          current.replaceWith(fresh);
        }
      });
  }

  document.addEventListener("submit", (ev) => {
    const form = ev.target.closest("form[data-section-toggle]");
    if (!form) return;
    ev.preventDefault();
    const block = form.closest(".section-block[data-section-id]");
    const sectionId = block && block.dataset.sectionId;
    const fd = new FormData(form);
    fetch(form.action, {
      method: "POST",
      body: fd,
      credentials: "same-origin",
    })
      .then((r) => {
        if (!r.ok) throw new Error("toggle failed");
        if (sectionId) return swapSection(sectionId);
      })
      .catch(() => {
        // Fall back to full reload on error so the user isn't stuck.
        window.location.reload();
      });
  });
})();
