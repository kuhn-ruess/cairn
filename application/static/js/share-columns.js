/* Reordering the customer sheet's columns on a billing project's page.
 *
 * The order is simply the order of the rows: each carries a hidden
 * `share_col_key`, and same-named fields are submitted in document order — so
 * moving a row *is* the edit, with no positions to keep in sync and nothing to
 * go stale if this script never runs (the rows then submit as rendered).
 *
 * Static file rather than inline, like the app's other reorder lists: an inline
 * <script> would not execute when <main> is replaced by the background refresh.
 */
(function () {
  function init() {
    const list = document.getElementById("shareColumns");
    if (!list || list.dataset.ready) return;
    list.dataset.ready = "1";
    if (typeof Sortable !== "undefined") {
      new Sortable(list, {
        handle: ".handle",
        animation: 150,
        ghostClass: "sc-ghost",
      });
    }
  }

  // The arrows work with or without Sortable — a keyboard, a touch screen and
  // a mouse all reach them, which dragging alone cannot claim.
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".sc-up, .sc-down");
    if (!btn) return;
    const row = btn.closest(".sc-row");
    const sibling = btn.classList.contains("sc-up")
      ? row.previousElementSibling
      : row.nextElementSibling;
    if (!row || !sibling) return;
    ev.preventDefault();
    if (btn.classList.contains("sc-up")) sibling.before(row);
    else sibling.after(row);
    btn.focus();
  });

  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
  document.addEventListener("bktodo:refresh-needed", () => setTimeout(init, 0));
})();
