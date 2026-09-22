/* Take a label off an item from the list it is showing in — the × on a chip
 * rendered by ``tasks/_tag_macros.html``.
 *
 * Delegated on ``document`` on purpose: every list in this app re-renders by
 * swapping ``<main>``'s markup (``bktodo:refresh-needed`` / ``refreshMain``),
 * which would throw away anything wired to the rows themselves. See the
 * "main-swap kills inline JS" note — view JS delegates or it stops working
 * after the first drag.
 *
 * The chip is only taken out of the page once the server has confirmed it, so
 * a failed call leaves the list telling the truth rather than a label that is
 * gone here and still there on reload. The same task can be on the page more
 * than once (a section plus a rolled-up list, Today beside the plan), so every
 * chip for that item+label goes, not just the one clicked.
 */
(function () {
  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  function flashError(msg) {
    let bar = document.getElementById("move-error-flash");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "move-error-flash";
      bar.className =
        "alert alert-danger position-fixed top-0 start-50 translate-middle-x mt-3 shadow";
      bar.style.zIndex = 2000;
      document.body.appendChild(bar);
    }
    bar.textContent = msg;
    setTimeout(() => { if (bar) bar.remove(); }, 3000);
  }

  // Every chip on the page for this item + label, wherever the item is drawn.
  function chipsFor(itemId, tagName) {
    return Array.from(
      document.querySelectorAll(
        `.tag-chip-remove[data-tag-item-id="${CSS.escape(itemId)}"]` +
        `[data-tag-name="${CSS.escape(tagName)}"]`,
      ),
    ).map((btn) => btn.closest(".tag-chip")).filter(Boolean);
  }

  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".tag-chip-remove");
    if (!btn) return;
    // The chip sits inside a row that is itself a link target and, in the
    // kanban, a drag handle — this click is neither.
    ev.preventDefault();
    ev.stopPropagation();
    if (btn.dataset.busy === "1") return;
    btn.dataset.busy = "1";

    const itemId = btn.dataset.tagItemId;
    const tagName = btn.dataset.tagName || "";
    const chips = chipsFor(itemId, tagName);

    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    fd.append("tag", tagName);
    try {
      const resp = await fetch(btn.dataset.tagRemoveUrl, {
        method: "POST",
        body: fd,
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      });
      if (!resp.ok) {
        btn.dataset.busy = "";
        flashError(`Could not remove the label (${resp.status}).`);
        return;
      }
      chips.forEach((chip) => chip.remove());
    } catch (err) {
      btn.dataset.busy = "";
      flashError("Could not remove the label — network error.");
    }
  });
})();
