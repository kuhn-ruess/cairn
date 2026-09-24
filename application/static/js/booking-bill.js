/* Billing a meeting from the bookings list, in place.
 *
 * Every action on a row — file it under a customer, log its time, take an entry
 * back — posts its own form and gets that ONE row's markup back, which replaces
 * the row. No navigation, so the Back button doesn't walk through every
 * appointment you touched, and the panel you were working in stays open (the
 * server re-renders it open).
 *
 * Delegated on document: the rows are replaced, so nothing may be bound to
 * them. The forms are ordinary forms with ordinary actions — without
 * JavaScript each one posts, flashes and redirects back to the list.
 */
(function () {
  function tokenOf(form) {
    const el = form.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  async function send(form) {
    const row = form.closest("[data-booking-row]");
    if (!row) return;
    const btns = form.querySelectorAll("button");
    btns.forEach((b) => { b.disabled = true; });
    try {
      const resp = await fetch(form.action, {
        method: "POST",
        headers: { "X-Requested-With": "fetch", "X-CSRFToken": tokenOf(form) },
        body: new FormData(form),
      });
      if (!resp.ok) throw new Error(resp.status);
      const html = await resp.text();
      // A <ul>, because the row IS an <li>: parsed inside anything else the
      // browser simply discards the tag.
      const tmp = document.createElement("ul");
      tmp.innerHTML = html.trim();
      const fresh = tmp.querySelector("[data-booking-row]");
      if (fresh) row.replaceWith(fresh);
      else row.outerHTML = html;
    } catch (err) {
      // A failed action must not leave a dead row: let the plain form path
      // take over, which is the one that can show the server's answer.
      btns.forEach((b) => { b.disabled = false; });
      form.removeAttribute("data-booking-form");
      form.submit();
    }
  }

  document.addEventListener("submit", (e) => {
    const form = e.target.closest("form[data-booking-form]");
    if (!form) return;
    e.preventDefault();
    send(form);
  });

  // Choosing a customer is the whole gesture — nobody wants to press Set after
  // it. The button stays for the no-JavaScript path.
  document.addEventListener("change", (e) => {
    const sel = e.target.closest("select[data-booking-customer]");
    if (!sel) return;
    const form = sel.closest("form[data-booking-form]");
    if (form) send(form);
  });
})();
