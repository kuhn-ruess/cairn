/* Favorite star toggle.
 *
 * Any element with [data-fav-token] is a star button. Clicking it POSTs to
 * /favorites/toggle and flips the bi-star / bi-star-fill icon in place.
 * Document-level delegated so it survives <main> swaps and works on rows that
 * are injected later. The sidebar Favorites section only updates on next load.
 */
(function () {
  function csrf() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  function paint(btn, favorited) {
    btn.setAttribute("aria-pressed", favorited ? "true" : "false");
    btn.classList.toggle("is-fav", favorited);
    const icon = btn.querySelector("i");
    if (icon) {
      icon.classList.toggle("bi-star-fill", favorited);
      icon.classList.toggle("bi-star", !favorited);
    }
    btn.title = favorited ? "Remove from favorites" : "Add to favorites";
  }

  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-fav-token]");
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    const token = btn.dataset.favToken;
    if (!token || btn.dataset.busy === "1") return;
    btn.dataset.busy = "1";
    try {
      const fd = new FormData();
      fd.append("csrf_token", csrf());
      fd.append("token", token);
      const resp = await fetch("/favorites/toggle", {
        method: "POST", body: fd, credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (!resp.ok) return;
      const data = await resp.json();
      paint(btn, !!data.favorited);
      // In the sidebar Favorites list, unstarring removes the row immediately.
      if (!data.favorited) {
        const row = btn.closest("[data-fav-remove-on-unset]");
        if (row) row.remove();
      }
      // Sidebar list reflects the change on the next navigation/refresh.
      document.dispatchEvent(new CustomEvent("cairn:favorites-changed",
        { detail: { token, favorited: !!data.favorited } }));
    } catch (e) {
      /* network — leave the star as-is */
    } finally {
      btn.dataset.busy = "0";
    }
  });
})();
