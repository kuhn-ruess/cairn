/* The Billing-project picker follows the Customer picker above it.
 *
 * The scope is decided on the server (services.customers.billing_choices) and
 * the form renders it already narrowed, so the picker is right the moment the
 * inspector opens. This file is only about the same sitting: naming the
 * customer and binding a project in one pass would otherwise mean saving twice
 * before the list narrowed. It asks the server for the list rather than hiding
 * options it was given — one source of truth for "what may be bound", so the
 * pair the user ends up with is always a pair the save accepts.
 *
 * The project currently selected is sent along and comes back whatever happens
 * (marked as another customer's): a binding carries logged hours and is
 * inherited by a whole subtree, so it is never dropped behind the user's back.
 *
 * Delegated on document — the inspector loads its form by fetch.
 */
(function () {
  const PLACEHOLDER = "— none —";

  function partner(cust) {
    const form = cust.closest("form");
    return form ? form.querySelector("select[data-billing-scope]") : null;
  }

  /* Kept in step with the same three sentences in
   * templates/projects/_inspector.html — the server renders them on open, this
   * renders them after a change, and they have to say the same thing. */
  function note(sel, data, name) {
    const scope = sel.closest(".inspector-section") || sel.parentNode;
    const box = scope ? scope.querySelector("[data-billing-scope-note]") : null;
    if (!box) return;
    if (data.empty) {
      box.innerHTML = "";
      box.append(name + " has no billing project yet — ");
      const a = document.createElement("a");
      a.href = "/time/customers/" + encodeURIComponent(sel.dataset.billingCustomer || "");
      a.textContent = "create one";
      box.append(a, ", or clear the customer above to bill this somewhere else.");
    } else if (name) {
      box.textContent = "Only " + name + "'s projects are offered, because a "
        + "billing project belongs to exactly one customer. Clear the customer "
        + "above to see every project again.";
    } else {
      box.textContent = "Time logged on tasks in here bills to this project — "
        + "and picking one sets the customer above, because a billing project "
        + "has exactly one.";
    }
    if ((data.out_of_scope || []).indexOf(sel.value) >= 0) {
      const warn = document.createElement("span");
      warn.className = "text-warning d-block";
      warn.textContent = "The project bound here bills to another customer. It "
        + "is kept rather than dropped — it carries logged hours — and a "
        + "binding names the customer, so clear it to file this under someone "
        + "else.";
      box.appendChild(warn);
    }
  }

  function repaint(sel, data) {
    const was = sel.value;
    sel.innerHTML = "";
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = PLACEHOLDER;
    sel.appendChild(blank);
    (data.projects || []).forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.label;
      sel.appendChild(opt);
    });
    // The selection survives when the server sent it back; it always does when
    // something was bound, which is what ``bound`` asks for.
    sel.value = Array.from(sel.options).some((o) => o.value === was) ? was : "";
  }

  const timers = new WeakMap();

  function reload(cust) {
    const sel = partner(cust);
    if (!sel) return;
    const cid = cust.value || "";
    const label = cust.options[cust.selectedIndex];
    const name = cid && label ? label.textContent.trim() : "";
    sel.dataset.billingCustomer = cid;
    clearTimeout(timers.get(sel));
    timers.set(sel, setTimeout(() => {
      const url = "/time/projects/for-customer?customer=" + encodeURIComponent(cid)
        + "&bound=" + encodeURIComponent(sel.value || "");
      fetch(url, { headers: { "X-Requested-With": "fetch" } })
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          // A failed ask leaves the list the server rendered — a picker that
          // empties itself because one request lost is worse than a wide one.
          if (!data) return;
          repaint(sel, data);
          note(sel, data, name);
        })
        .catch(() => {});
    }, 60));
  }

  document.addEventListener("change", (ev) => {
    const cust = ev.target.closest("select[data-billing-scope-customer]");
    if (cust) reload(cust);
  });
})();
