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

  /* An empty datetime-local field is the slowest way there is to say "next
   * Tuesday": every segment has to be typed before the arrow keys or the
   * picker become useful. So focusing an empty one fills in a default — today
   * at 09:00, tomorrow's if that hour has passed, never a moment already gone.
   *
   * The value is TENTATIVE: leave the field without touching it and it is
   * empty again, so tabbing through the inspector never invents a due date or
   * defers a task by way of a follow-up nobody asked for. Blur settles it, and
   * so does a submit (Enter inside the field never blurs it) — that listener
   * captures, because the inspector's own submit handler reads the FormData.
   */
  const MARK = "quickDateTentative";

  function defaultValue() {
    const d = new Date();
    d.setHours(9, 0, 0, 0);
    if (d.getTime() <= Date.now()) d.setDate(d.getDate() + 1);
    return toLocalISO(d);
  }

  function fill(ev) {
    const el = ev.target;
    if (!el.matches || !el.matches('input[type="datetime-local"]')) return;
    if (el.value || el.disabled || el.readOnly) return;
    el.value = defaultValue();
    el.dataset[MARK] = el.value;
    markEmpty(el);
  }

  function settle(el) {
    if (!el.dataset || !(MARK in el.dataset)) return;
    if (el.value === el.dataset[MARK]) el.value = "";
    delete el.dataset[MARK];
    markEmpty(el);
  }

  /* Safari draws an EMPTY datetime-local field with today's date and the
   * current time in its segments, in the same colour as a real value. So
   * after the clear button (×) the plan date looked like it had only jumped
   * to today, and after a reload a task without a plan date looked planned
   * for today. The value was empty all along, and so was what got saved.
   *
   * An empty field in the inspector carries .dt-empty, and app.css draws its
   * segments transparent while it is not focused. Kept in step on input and
   * change; code that sets one of these fields by hand dispatches "change"
   * (the × above does). Only the inspector's fields: a field elsewhere that is
   * filled by a script without an event would otherwise stay invisible. */
  const EMPTY_SCOPE = 'form[data-inspector-form] input[type="datetime-local"]';

  function markEmpty(el) {
    if (el.matches && el.matches(EMPTY_SCOPE)) el.classList.toggle("dt-empty", !el.value);
  }

  function markAll() {
    document.querySelectorAll(EMPTY_SCOPE).forEach(markEmpty);
  }

  document.addEventListener("input", (ev) => markEmpty(ev.target));
  document.addEventListener("change", (ev) => markEmpty(ev.target));
  document.addEventListener("inspector:loaded", markAll);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", markAll);
  else markAll();

  document.addEventListener("focusin", fill);
  document.addEventListener("focusout", (ev) => settle(ev.target));
  document.addEventListener("submit", (ev) => {
    const form = ev.target;
    if (!form.querySelectorAll) return;
    form.querySelectorAll('input[type="datetime-local"]').forEach(settle);
  }, true);
})();
