/* Quick-set helpers for datetime-local inputs.
 *
 * Markup: any button with [data-quick-date="<input-id>"] and one of
 *   data-set="today" / "tomorrow" / "evening" / "in-week" / "next-monday" /
 *   "clear"  — ABSOLUTE shortcuts, always relative to *now*.
 *   data-set="add-day" / "add-week" / "add-month"
 *                        — CUMULATIVE: add onto the value already in the
 *                          field (or today, if it's empty), keeping its
 *                          time-of-day. So clicking "+1w" repeatedly keeps
 *                          pushing the date a week further out.
 */
(function () {
  function pad(n) { return n < 10 ? "0" + n : "" + n; }

  function toLocalISO(d) {
    return d.getFullYear() + "-" +
      pad(d.getMonth() + 1) + "-" +
      pad(d.getDate()) + "T" +
      pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  // Parse a datetime-local value ("YYYY-MM-DDTHH:MM") into a Date, or null.
  function parseLocal(v) {
    const m = (v || "").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
    if (!m) return null;
    return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  }

  function compute(kind, current) {
    // Cumulative shortcuts add onto the field's current value so they stack;
    // when the field is empty they start from today at 09:00.
    if (kind === "add-day" || kind === "add-week" || kind === "add-month") {
      const base = parseLocal(current);
      const d = base || (function () {
        const t = new Date(); t.setHours(9, 0, 0, 0); return t;
      })();
      if (kind === "add-day") d.setDate(d.getDate() + 1);
      else if (kind === "add-week") d.setDate(d.getDate() + 7);
      else d.setMonth(d.getMonth() + 1);
      return toLocalISO(d);
    }
    // Absolute shortcuts — always relative to *now*. Basing off the input's
    // existing value made "Today" only change the time and leave the old day.
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
    input.value = compute(kind, input.value);
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  document.addEventListener("click", handle);
})();
