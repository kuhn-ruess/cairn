/* Habit goals — +1 / undo check-ins without a page reload.
 *
 * Every handler is delegated on `document` (not the card element) so it keeps
 * working after the app's background <main> refresh swaps DOM in/out, and so a
 * card replaced by an AJAX response is instantly live again. The server returns
 * the re-rendered card partial; we swap it in place, keeping browser history
 * and scroll position clean (see the AJAX-over-POST convention).
 */
(function () {
  "use strict";

  function csrf() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  async function checkin(btn) {
    const url = btn.dataset.url;
    const tid = btn.dataset.tid;
    if (!url || !tid) return;

    const card = btn.closest("[data-habit-card]");
    // Disable both buttons in this target row while the request is in flight.
    const row = btn.closest(".habit-target");
    if (row) row.querySelectorAll("button").forEach((b) => (b.disabled = true));

    const fd = new FormData();
    fd.append("csrf_token", csrf());
    fd.append("tid", tid);

    try {
      const resp = await fetch(url, {
        method: "POST",
        body: fd,
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrf(), "X-Requested-With": "XMLHttpRequest" },
      });
      if (!resp.ok) throw new Error("checkin failed");
      const html = await resp.text();
      if (card) {
        const tmp = document.createElement("div");
        tmp.innerHTML = html.trim();
        const fresh = tmp.firstElementChild;
        if (fresh) card.replaceWith(fresh);
      }
    } catch (e) {
      if (row) row.querySelectorAll("button").forEach((b) => (b.disabled = false));
    }
  }

  document.addEventListener("click", function (ev) {
    const btn = ev.target.closest(".habit-plus, .habit-undo");
    if (!btn) return;
    ev.preventDefault();
    checkin(btn);
  });
})();
