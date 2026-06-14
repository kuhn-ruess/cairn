/* Quick-set helpers for datetime-local inputs.
 *
 * Markup: any button with [data-quick-date="<input-id>"] and one of
 *   data-set="today" / "tomorrow" / "in-week" / "next-monday" /
 *   "evening" / "clear" — clicking it stuffs the matching ISO string
 *   into the named input.
 */
(function () {
  function pad(n) { return n < 10 ? "0" + n : "" + n; }

  function toLocalISO(d) {
    return d.getFullYear() + "-" +
      pad(d.getMonth() + 1) + "-" +
      pad(d.getDate()) + "T" +
      pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function compute(kind) {
    // Always relative to *now* — the shortcuts mean "today", "tomorrow",
    // "+1 week" from today, etc. Basing off the input's existing value made
    // "Today" only change the time and leave the old day in place.
    const d = new Date();
    switch (kind) {
      case "today":
        d.setHours(17, 0, 0, 0);
        return toLocalISO(d);
      case "tomorrow":
        d.setDate(d.getDate() + 1);
        d.setHours(9, 0, 0, 0);
        return toLocalISO(d);
      case "evening":
        d.setHours(19, 0, 0, 0);
        return toLocalISO(d);
      case "in-week":
        d.setDate(d.getDate() + 7);
        d.setHours(9, 0, 0, 0);
        return toLocalISO(d);
      case "next-monday": {
        const days = (8 - d.getDay()) % 7 || 7;
        d.setDate(d.getDate() + days);
        d.setHours(9, 0, 0, 0);
        return toLocalISO(d);
      }
      default:
        return "";
    }
  }

  function handle(e) {
    const btn = e.target.closest("[data-quick-date]");
    if (!btn) return;
    e.preventDefault();
    const inputId = btn.dataset.quickDate;
    const input = document.getElementById(inputId);
    if (!input) return;
    const kind = btn.dataset.set || "today";
    input.value = compute(kind);
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  document.addEventListener("click", handle);
})();
