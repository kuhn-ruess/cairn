/* Navbar notification bell.
 *
 * The list is rendered server-side; this just clears the unread state. When
 * the dropdown opens (or "Mark all read" is clicked) we POST to mark every
 * notification read, drop the badge, and un-highlight the rows. No reload —
 * the next page load renders them already-read.
 */
(function () {
  const bell = document.getElementById("notifBell");
  if (!bell) return;

  const toggle = bell.querySelector('[data-bs-toggle="dropdown"]');
  const badge = document.getElementById("notifBadge");
  const markAllBtn = document.getElementById("notifMarkAll");
  const url = bell.dataset.markReadUrl;

  function markRead() {
    if (!badge || badge.hidden) return; // nothing unread
    const fd = new FormData();
    const csrf = bell.querySelector('input[name="csrf_token"]')
      || document.querySelector('input[name="csrf_token"]');
    if (csrf) fd.append("csrf_token", csrf.value);
    fetch(url, { method: "POST", body: fd, headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(() => {
        badge.hidden = true;
        badge.textContent = "0";
        bell.querySelectorAll(".notif-item").forEach((el) => {
          el.classList.remove("bg-body-tertiary");
          const icon = el.querySelector("i");
          if (icon) { icon.classList.remove("text-primary"); icon.classList.add("text-muted"); }
        });
      })
      .catch(() => {});
  }

  if (toggle) toggle.addEventListener("shown.bs.dropdown", markRead);
  if (markAllBtn) markAllBtn.addEventListener("click", markRead);
})();
