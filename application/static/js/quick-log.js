/* "Log time" dialog — the time-tracking twin of the quick-add (quick-add.js).
 *
 * - Opens on `t` from anywhere (not in inputs), or the sidebar "Log time".
 * - Its body is fetched from /time/quick each time it opens, with the page's
 *   context: the task in the open inspector, the task / note / project page,
 *   the sidebar's ?project=, a billing project's or a customer's page. The
 *   server turns that into the preselected billing project and says which
 *   context decided it; the picker overrides it like anywhere else.
 * - The controls are the day sheet's capture bar (.time-add), so custom
 *   fields, the clock toggle and the duration chips are time-entries.js's.
 *   This file owns only opening, submitting and the keyboard:
 *   Enter books from any text field, ⌘/Ctrl+Enter from anywhere, Esc cancels.
 */
(function () {
  const modalEl = document.getElementById("quickLogModal");
  if (!modalEl) return;
  const modal = bootstrap.Modal.getOrCreateInstance(modalEl, { keyboard: true });
  const form = modalEl.querySelector("#quickLogForm");
  const body = modalEl.querySelector("[data-quick-log-body]");
  const continueCb = modalEl.querySelector("#quickLogContinue");
  const HEX = "([0-9a-f]{24})";

  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  // What the page is about, most specific first. Only ids travel — the server
  // decides what they mean for billing (and ignores what the user can't see).
  function detectContext() {
    const insp = document.querySelector("#inspector-panel.show [data-detail-url]");
    const im = insp && insp.dataset.detailUrl.match(new RegExp("/tasks/" + HEX, "i"));
    if (im) return { item: im[1] };
    const path = window.location.pathname;
    let m = path.match(new RegExp("^/time/projects/" + HEX, "i"));
    if (m) return { tp: m[1] };
    m = path.match(new RegExp("^/time/customers/" + HEX, "i"));
    if (m) return { customer: m[1] };
    m = path.match(new RegExp("^/(?:tasks|notes|projects)/" + HEX, "i"));
    if (m) return { item: m[1] };
    const qp = new URLSearchParams(window.location.search).get("project");
    if (qp && new RegExp("^" + HEX + "$", "i").test(qp)) return { item: qp };
    return {};
  }

  let loaded = Promise.resolve();
  // What a calendar event's "Log time" button hands over (event_log_btn):
  // title, day, start, end, minutes, customer. Taken by the next load and dropped, so a
  // later `t` opens the dialog empty again.
  let prefill = null;

  // Both the duration and the clock pair are filled: the project decides
  // which of the two is shown and sent, and the picker may change it.
  function applyPrefill(p) {
    const set = (sel, v) => {
      const el = body.querySelector(sel);
      if (el && v) el.value = v;
    };
    set(".time-add-note", p.logNote);
    set(".time-add-date", p.logDate);
    set(".time-add-start", p.logStart);
    set(".time-add-end", p.logEnd);
    const min = parseInt(p.logMinutes, 10);
    if (min > 0) {
      const h = Math.floor(min / 60), m = min % 60;
      set(".time-add-duration", h ? (m ? `${h}h${m}m` : `${h}h`) : `${m}m`);
    }
  }

  function load() {
    const taken = prefill;
    prefill = null;
    body.innerHTML = '<p class="text-muted small mb-0">Loading…</p>';
    // An event is not about the task that happens to be open beside it; its
    // own customer (the chip on the event) is the context, if it has one.
    const ctx = taken ? (taken.logCustomer ? { customer: taken.logCustomer } : {})
                      : detectContext();
    const url = modalEl.dataset.formUrl + "?" + new URLSearchParams(ctx);
    loaded = fetch(url, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.text() : Promise.reject(r.status)))
      .then((html) => {
        body.innerHTML = html;
        if (taken) applyPrefill(taken);
        // The chips and clock buttons repeat what the fields take typed, so
        // they stay out of the tab order — note → duration is one Tab, the
        // same reasoning as the quick-add's date chips.
        body.querySelectorAll(".tt-dur-step, .tt-dur-chip, .tt-now, .tt-time-pick")
          .forEach((b) => { b.tabIndex = -1; });
      })
      .catch(() => {
        body.innerHTML = '<p class="text-danger small mb-0">Could not load the form.</p>';
      });
    return loaded;
  }

  function focusFirst() {
    const note = body.querySelector(".time-add-note");
    if (note) note.focus();
  }

  modalEl.addEventListener("show.bs.modal", load);

  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest && ev.target.closest(".event-log-btn");
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    prefill = { ...btn.dataset };
    modal.show();
  });
  // Focus in ``shown`` for the same reason as quick-add.js: Bootstrap's focus
  // trap takes the caret back at the end of the fade.
  modalEl.addEventListener("shown.bs.modal", () => loaded.then(focusFirst));

  function field(sel) {
    const el = body.querySelector(sel);
    return el ? el.value.trim() : "";
  }

  function collect() {
    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    const clock = body.querySelector(".time-add-clock");
    const byClock = clock && !clock.classList.contains("d-none");
    if (byClock) {
      // 930 or 9 as typed is read as 09:30 / 09:00 (time-entries.js);
      // what can't be read goes as typed and the server says so.
      const clockVal = (sel) => {
        const raw = field(sel);
        return (window.ttNormClock && window.ttNormClock(raw)) || raw;
      };
      fd.append("start_time", clockVal(".time-add-start"));
      fd.append("end_time", clockVal(".time-add-end"));
    } else {
      fd.append("duration", field(".time-add-duration"));
    }
    fd.append("note", field(".time-add-note"));
    fd.append("project_id", field(".time-add-project"));
    fd.append("spent_on", field(".time-add-date"));
    const billable = body.querySelector(".time-add-billable");
    fd.append("billable", billable && !billable.checked ? "0" : "1");
    body.querySelectorAll(".tt-cf").forEach((el) => {
      fd.append("cf_" + el.dataset.key, el.value.trim());
    });
    const link = body.querySelector('input[name="link_item"]');
    if (link && link.checked) {
      fd.append("link_item", "1");
      fd.append("item_id", field('input[name="item_id"]'));
    }
    return { fd, byClock };
  }

  // The link to the open item follows the project: picking another project
  // than the one the item bills to means this time is about something else.
  body.addEventListener("change", (ev) => {
    if (!ev.target.closest(".time-add-project")) return;
    const link = body.querySelector('input[name="link_item"]');
    if (link && link.dataset.project) {
      link.checked = ev.target.value === link.dataset.project;
    }
  });

  let submitting = false;
  async function submit() {
    if (submitting || !body.querySelector(".time-add")) return;
    const { fd, byClock } = collect();
    // Nothing to book yet — put the caret where the time goes, like the day
    // sheet does, instead of asking the server to say so.
    if (!byClock && !fd.get("duration")) {
      const d = body.querySelector(".time-add-duration");
      if (d) d.focus();
      return;
    }
    submitting = true;
    const btn = form.querySelector('button[type="submit"]');
    btn.disabled = true;
    try {
      let resp = await fetch(form.action, {
        method: "POST", body: fd, credentials: "same-origin",
      });
      let data = await resp.json().catch(() => ({}));
      // Overlaps another entry for the same customer that day: nothing was
      // saved yet. Confirmed, the same booking goes again and is kept.
      if (data.confirm_overlap) {
        if (!window.confirm(`${data.confirm_overlap}\n\nSave anyway?`)) return;
        fd.set("overlap_ok", "1");
        resp = await fetch(form.action, {
          method: "POST", body: fd, credentials: "same-origin",
        });
        data = await resp.json().catch(() => ({}));
      }
      if (!resp.ok || !data.ok) {
        showError(data.error || `Server error: ${resp.status}`);
        return;
      }
      showSuccess(data);
      // Anything on the page that shows time (the day sheet, a task's time
      // section) is re-rendered in place.
      document.dispatchEvent(new Event("bktodo:refresh-needed"));
      if (continueCb.checked) {
        // Same project, same day — the next entry is usually the next thing
        // done for the same customer. Only what describes this one is cleared.
        body.querySelectorAll(".time-add-note, .time-add-duration, .time-add-start, .time-add-end")
          .forEach((el) => { el.value = ""; });
        focusFirst();
      } else {
        modal.hide();
      }
    } catch (err) {
      showError(`Network error: ${err.message}`);
    } finally {
      submitting = false;
      btn.disabled = false;
    }
  }

  function showSuccess(data) {
    const banner = document.createElement("div");
    banner.className =
      "alert alert-success position-fixed top-0 start-50 translate-middle-x mt-3 shadow";
    banner.style.zIndex = 2100;
    const h = Math.floor(data.minutes / 60), m = data.minutes % 60;
    const dur = h ? (m ? `${h}h${m}m` : `${h}h`) : `${m}m`;
    banner.textContent = `Logged ${dur} → ${data.project}`;
    document.body.appendChild(banner);
    setTimeout(() => banner.remove(), 2500);
  }

  function showError(msg) {
    let err = modalEl.querySelector(".quick-add-error");
    if (!err) {
      err = document.createElement("div");
      err.className = "alert alert-danger quick-add-error mb-0 mt-2";
      modalEl.querySelector(".modal-body").appendChild(err);
    }
    err.textContent = msg;
    setTimeout(() => err.remove(), 5000);
  }

  form.addEventListener("submit", (ev) => { ev.preventDefault(); submit(); });

  // Enter books from every text field — the capture bar's rule — and
  // ⌘/Ctrl+Enter from anywhere, the app's one "save from the keyboard" chord.
  // Stopped here so time-entries.js's own Enter handler (which posts a
  // .time-add to the day sheet) never sees it. The picker's search box keeps
  // Enter: there it takes the highlighted project.
  modalEl.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" || ev.isComposing) return;
    const t = ev.target;
    if (t.closest(".tt-pick-search")) return;
    const chord = ev.metaKey || ev.ctrlKey;
    if (!chord && (t.tagName === "BUTTON" || t.tagName === "A")) return;
    ev.preventDefault();
    ev.stopPropagation();
    submit();
  });

  // `t` from anywhere (not in inputs). shortcuts.js runs first and claims the
  // `t` of `g t` (Today) with preventDefault, which is what the check skips.
  document.addEventListener("keydown", (ev) => {
    if (ev.defaultPrevented) return;
    if (ev.target.matches("input, textarea, select, [contenteditable]")) return;
    if (ev.target.isContentEditable) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey || ev.shiftKey) return;
    if (document.querySelector(".modal.show")) return;
    if (ev.key && ev.key.toLowerCase() === "t") {
      ev.preventDefault();
      modal.show();
    }
  });
})();
