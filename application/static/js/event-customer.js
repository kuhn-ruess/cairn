/* The customer chip on a calendar event (day / plan / list views).
 *
 * ``.event-cust-btn`` (tasks/_event_note_macros.html) carries the event's uid
 * and title. Clicking it opens a small panel: who the event is filed under or
 * suggested as, every customer to pick from (type to filter), "No customer",
 * "Back to the title's suggestion" once it was filed by hand, and that
 * customer's open tasks, each opening in the inspector.
 *
 *   GET  /tasks/event-customer?uid=…&title=…   POST /tasks/event-customer
 *
 * Filing is per SERIES, so after a save every chip of the same series on the
 * page is redrawn in place (and its "Log time" button learns the customer) —
 * no reload, the plan stays where it was scrolled.
 *
 * Loaded globally and delegated on document, like event-notes.js.
 */
(function () {
  let panel = null;
  let openerBtn = null;

  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function closePanel() {
    if (panel) { panel.remove(); panel = null; }
    openerBtn = null;
    document.removeEventListener("keydown", onKeydown, true);
  }

  function onKeydown(e) {
    if (e.key === "Escape") closePanel();
  }

  function position(p, btn) {
    const r = btn.getBoundingClientRect();
    let left = Math.min(r.left, window.innerWidth - p.offsetWidth - 8);
    left = Math.max(8, left);
    let top = r.bottom + 4;
    if (top + p.offsetHeight > window.innerHeight - 8) {
      top = Math.max(8, r.top - p.offsetHeight - 4);
    }
    p.style.left = left + "px";
    p.style.top = top + "px";
  }

  // The same series as ``services/event_customers.series_uid``.
  function seriesOf(uid) {
    return (uid || "").replace(/#.*$/, "").replace(/_\d{8}(T\d{6}Z?)?$/, "");
  }

  // Redraw one chip from what the server says now (``current`` or null).
  function paint(btn, cur) {
    btn.classList.toggle("has-customer", !!cur);
    btn.classList.toggle("is-suggested", !!(cur && cur.suggested));
    btn.textContent = "";
    if (cur) {
      // The dot only where rows are coloured by customer (data-dot).
      if (btn.dataset.dot) {
        const dot = el("span", "event-cust-dot");
        dot.style.setProperty("--dot", cur.color);
        btn.appendChild(dot);
      } else {
        btn.appendChild(el("i", "bi bi-building"));
      }
      btn.appendChild(el("span", "event-cust-name", cur.name + (cur.suggested ? "?" : "")));
      btn.title = cur.suggested
        ? `Suggested from the title: ${cur.name} — click to confirm or change`
        : `Customer: ${cur.name}`;
    } else {
      btn.appendChild(el("i", "bi bi-building"));
      btn.title = "Assign a customer";
    }
    const host = btn.closest(".cal-event, .cal-pill-event, .plan-card-event");
    const log = host && host.querySelector(".event-log-btn");
    if (log) log.dataset.logCustomer = cur ? cur.id : "";
  }

  function repaintSeries(uid, cur) {
    const sid = seriesOf(uid);
    document.querySelectorAll(".event-cust-btn").forEach((b) => {
      if (seriesOf(b.dataset.eventUid) === sid) paint(b, cur);
    });
  }

  async function save(btn, customer) {
    const fd = new FormData();
    fd.append("uid", btn.dataset.eventUid);
    fd.append("title", btn.dataset.eventTitle || "");
    fd.append("customer", customer);
    fd.append("csrf_token", csrfToken());
    try {
      const resp = await fetch("/tasks/event-customer", {
        method: "POST", body: fd, credentials: "same-origin",
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.ok) throw new Error(data.error || resp.status);
      repaintSeries(btn.dataset.eventUid, data.current);
      closePanel();
    } catch (err) {
      alert("Could not save the customer: " + err.message);
    }
  }

  function render(btn, data) {
    panel.textContent = "";
    panel.appendChild(el("div", "enp-head", btn.dataset.eventTitle || "Event"));

    const cur = data.current;
    const state = el("div", "enp-empty");
    state.textContent = !cur ? "No customer."
      : cur.suggested ? `Suggested from the title: ${cur.name}`
      : `Customer: ${cur.name}`;
    panel.appendChild(state);

    const search = el("input", "form-control form-control-sm ecp-search");
    search.type = "search";
    search.placeholder = "Find a customer…";
    panel.appendChild(search);

    const list = el("div", "ecp-list");
    const rows = (data.customers || []).map((c) => {
      const b = el("button", "enp-item");
      b.type = "button";
      const isCur = cur && cur.id === c.id && !cur.suggested;
      b.appendChild(el("i", "bi " + (isCur ? "bi-check-lg" : "bi-building")));
      b.appendChild(el("span", null, c.label));
      b.addEventListener("click", () => save(btn, c.id));
      list.appendChild(b);
      return { b, key: c.label.toLowerCase() };
    });
    panel.appendChild(list);
    search.addEventListener("input", () => {
      const q = search.value.trim().toLowerCase();
      rows.forEach((r) => { r.b.hidden = !!q && !r.key.includes(q); });
    });
    search.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const first = rows.find((r) => !r.b.hidden);
      if (first) first.b.click();
    });

    const none = el("button", "enp-item");
    none.type = "button";
    none.appendChild(el("i", "bi bi-dash-circle"));
    none.appendChild(el("span", null, "No customer"));
    none.addEventListener("click", () => save(btn, "none"));
    panel.appendChild(none);
    if (data.filed) {
      const reset = el("button", "enp-item");
      reset.type = "button";
      reset.appendChild(el("i", "bi bi-arrow-counterclockwise"));
      reset.appendChild(el("span", null, "Back to the title's suggestion"));
      reset.addEventListener("click", () => save(btn, ""));
      panel.appendChild(reset);
    }

    if (cur) {
      const head = el("div", "enp-head ecp-tasks-head",
        `Open tasks · ${cur.name} (${data.tasks_total || 0})`);
      panel.appendChild(head);
      if (!(data.tasks || []).length) {
        panel.appendChild(el("div", "enp-empty", "No open tasks for this customer."));
      }
      (data.tasks || []).forEach((t) => {
        const a = el("a", "enp-item");
        a.href = t.url;
        a.dataset.inspectorUrl = t.edit_url;
        a.dataset.inspectorTitle = t.title;
        a.appendChild(el("i", "bi " + (t.next ? "bi-arrow-right-circle" : "bi-circle")));
        a.appendChild(el("span", null, t.title));
        a.addEventListener("click", () => setTimeout(closePanel, 0));
        panel.appendChild(a);
      });
    }

    position(panel, btn);
    search.focus();
  }

  async function openPanel(btn) {
    const reopen = openerBtn === btn;
    closePanel();
    if (reopen) return;
    openerBtn = btn;
    panel = el("div", "event-notes-panel event-cust-panel");
    panel.appendChild(el("div", "enp-empty", "Loading…"));
    document.body.appendChild(panel);
    position(panel, btn);
    document.addEventListener("keydown", onKeydown, true);
    const qs = new URLSearchParams({
      uid: btn.dataset.eventUid, title: btn.dataset.eventTitle || "",
    });
    try {
      const resp = await fetch("/tasks/event-customer?" + qs, { credentials: "same-origin" });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || resp.status);
      if (panel && openerBtn === btn) render(btn, data);
    } catch (err) {
      if (panel && openerBtn === btn) {
        panel.textContent = "";
        panel.appendChild(el("div", "enp-empty", "Could not load the customers."));
      }
    }
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest && e.target.closest(".event-cust-btn");
    if (btn) {
      e.preventDefault();
      e.stopPropagation();
      openPanel(btn);
      return;
    }
    if (panel && !e.target.closest(".event-cust-panel")) closePanel();
  }, true);

  window.addEventListener("resize", closePanel);
})();
