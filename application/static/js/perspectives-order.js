/* Drag-to-reorder for the Settings → Sidebar perspectives list.
 *
 * Lives in a static file (not inline) so it runs whether the page is a full
 * load OR swapped into <main> by the app's background refresh — an inline
 * <script> wouldn't execute on innerHTML swap, which left reordering dead and
 * (worse) submitting the form with no order. The order itself rides in
 * per-row `visible[]` hidden inputs, so saving works even if this never runs.
 */
(function () {
  function init() {
    const list = document.getElementById("visibleList");
    if (!list || list.dataset.sortableReady || typeof Sortable === "undefined") {
      return;
    }
    list.dataset.sortableReady = "1";
    new Sortable(list, {
      handle: ".handle",
      animation: 150,
      ghostClass: "perspective-ghost",
    });
  }

  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
  // Re-init after the app swaps <main> (a fresh #visibleList has no ready flag).
  document.addEventListener("bktodo:refresh-needed", function () {
    setTimeout(init, 0);
  });
})();
