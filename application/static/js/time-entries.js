/* Time tracking — stopwatch + logging, everywhere.
 *
 * One delegated handler drives three places that share the same markup:
 *   • the task inspector block (#time / .time-block)
 *   • the day sheet (#tt-day)
 *   • the navbar stopwatch chip (#tt-chip)
 *
 * Every endpoint answers with {chip, block}: the chip HTML always, the block
 * only for the caller's own view (X-Partial: task | day). So a stop from the
 * navbar updates the navbar, and a stop from the day sheet updates both — no
 * reload, no history entry (see the AJAX-over-POST-redirect convention).
 *
 * Delegation lives on document because <main> is swapped wholesale by
 * bktodo:refresh-needed, which would kill any handler bound to a node inside.
 */
(function () {
  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  // Which block the clicked control lives in — decides the partial we ask for.
  function contextFor(el) {
    const day = el.closest("#tt-day");
    if (day) return { kind: "day", block: day, day: day.dataset.day };
    const task = el.closest("#time, .time-block");
    if (task) return { kind: "task", block: task };
    return { kind: "", block: null };
  }

  function applyPayload(data, ctx) {
    const chip = document.getElementById("tt-chip");
    if (chip && typeof data.chip === "string") {
      chip.innerHTML = data.chip;
      startTicking(chip);
    }
    if (ctx.block && typeof data.block === "string") {
      ctx.block.innerHTML = data.block;
      ensureDefaultDate(ctx.block);
      startTicking(ctx.block);
    }
    if (typeof data.timer_item === "string") syncRowButtons(data.timer_item);
  }

  // Task rows carry a play button that becomes a stop button while the timer
  // runs on that row — kept in sync here so starting from anywhere updates the
  // list without a reload.
  function syncRowButtons(timerItem) {
    document.querySelectorAll(
      ".task-row .tt-timer-start, .task-row .tt-timer-stop"
    ).forEach((btn) => {
      const running = !!timerItem && btn.dataset.taskId === timerItem;
      btn.classList.toggle("tt-timer-stop", running);
      btn.classList.toggle("tt-timer-start", !running);
      btn.classList.toggle("task-row-hover-action", !running);
      btn.dataset.action = running ? (window.TT || {}).stop : (window.TT || {}).start;
      btn.title = running ? "Stop the timer and log the time"
                          : "Start tracking time on this task";
      btn.innerHTML = running
        ? '<i class="bi bi-stop-circle-fill text-danger"></i>'
        : '<i class="bi bi-play-circle text-muted"></i>';
    });
  }

  async function post(action, fields, ctx) {
    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    Object.entries(fields || {}).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") fd.append(k, v);
    });
    if (ctx.kind === "day" && ctx.day) fd.append("d", ctx.day);
    const resp = await fetch(action, {
      method: "POST",
      headers: { "X-Partial": ctx.kind || "chip" },
      body: fd,
    });
    if (!resp.ok) throw new Error(resp.status);
    const data = await resp.json();
    applyPayload(data, ctx);
    return data;
  }

  // ---- the ticking clock ---------------------------------------------------
  // Every .tt-timer-elapsed carries the seconds the *server* measured; we only
  // add the time passed in this tab since it was rendered, so a skewed browser
  // clock can never make the timer disagree with what gets logged.
  function startTicking(scope) {
    (scope || document).querySelectorAll(".tt-timer-elapsed").forEach((el) => {
      const base = parseInt(el.dataset.elapsed || "0", 10);
      const t0 = Date.now();
      const paint = () => {
        if (!el.isConnected) return;
        const s = base + Math.floor((Date.now() - t0) / 1000);
        const h = Math.floor(s / 3600);
        const m = Math.floor((s % 3600) / 60);
        el.textContent = `${h}:${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
      };
      paint();
      if (el._ttTick) clearInterval(el._ttTick);
      el._ttTick = setInterval(paint, 1000);
    });
  }

  // ---- custom fields -------------------------------------------------------
  // Each <option> carries its project's field definitions, so switching the
  // project re-asks for the right things without a round trip. Values are
  // submitted as cf_<key>, which is what the server reads them back as.
  function renderCustomFields(scope) {
    (scope || document).querySelectorAll(".time-add").forEach((add) => {
      const box = add.querySelector(".tt-custom-fields");
      const sel = add.querySelector(".time-add-project");
      if (!box) return;
      let fields = [];
      const opt = sel && sel.selectedOptions[0];
      if (opt && opt.dataset.fields) {
        try { fields = JSON.parse(opt.dataset.fields); } catch (e) { fields = []; }
      }
      // Keep what the user already typed when the list is rebuilt for the
      // same project (a re-render after logging must not eat a half-filled
      // field), but drop it when the project — and so the fields — changed.
      const previous = {};
      box.querySelectorAll(".tt-cf").forEach((el) => { previous[el.dataset.key] = el.value; });
      const sameProject = box.dataset.project === (sel ? sel.value : "");
      box.dataset.project = sel ? sel.value : "";
      box.innerHTML = "";
      fields.forEach((f) => {
        const value = sameProject && previous[f.key] !== undefined
          ? previous[f.key] : (f.default || "");
        let el;
        if (f.type === "select") {
          el = document.createElement("select");
          el.className = "form-select form-select-sm tt-cf";
          el.style.maxWidth = "11rem";
          const blank = document.createElement("option");
          blank.value = "";
          blank.textContent = f.label + " —";
          el.appendChild(blank);
          (f.options || []).forEach((o) => {
            const op = document.createElement("option");
            op.value = o;
            op.textContent = o;
            el.appendChild(op);
          });
          el.value = value;
        } else {
          el = document.createElement("input");
          el.type = "text";
          el.className = "form-control form-control-sm tt-cf";
          el.style.maxWidth = "11rem";
          el.placeholder = f.label;
          el.value = value;
        }
        el.dataset.key = f.key;
        el.title = f.label;
        box.appendChild(el);
      });
    });
  }

  // ---- duration vs. start/end ----------------------------------------------
  // Which one you book with is a habit, not a per-entry decision, so the choice
  // is remembered across pages and reloads.
  const CLOCK_KEY = "cairn-tt-clock";
  function applyClockMode(scope) {
    const on = localStorage.getItem(CLOCK_KEY) === "1";
    (scope || document).querySelectorAll(".time-add").forEach((add) => {
      const dur = add.querySelector(".time-add-duration");
      const clock = add.querySelector(".time-add-clock");
      if (!dur || !clock) return;
      dur.classList.toggle("d-none", on);
      clock.classList.toggle("d-none", !on);
      clock.classList.toggle("d-flex", on);
      const btn = add.querySelector(".tt-clock-toggle");
      if (btn) {
        btn.classList.toggle("btn-outline-secondary", !on);
        btn.classList.toggle("btn-secondary", on);
        btn.title = on ? "Book a duration instead"
                       : "Book start and end time instead of a duration";
      }
    });
  }

  function ensureDefaultDate(scope) {
    (scope || document).querySelectorAll('.time-add-date[type="date"]').forEach((inp) => {
      if (!inp.value) inp.value = new Date().toISOString().slice(0, 10);
    });
  }

  ensureDefaultDate();
  startTicking();
  renderCustomFields();
  applyClockMode();
  // The off-canvas inspector injects its body after load, and <main> swaps
  // wholesale — re-arm everything on any DOM change.
  new MutationObserver(() => {
    ensureDefaultDate();
    startTicking();
    renderCustomFields();
    applyClockMode();
  }).observe(document.body, { childList: true, subtree: true });

  document.addEventListener("change", (ev) => {
    if (ev.target.closest(".time-add-project")) renderCustomFields();
  });

  // ---- manual logging ------------------------------------------------------
  function addFields(add) {
    const val = (sel) => {
      const el = add.querySelector(sel);
      return el ? el.value.trim() : "";
    };
    const billable = add.querySelector(".time-add-billable");
    const clock = add.querySelector(".time-add-clock");
    const byClock = clock && !clock.classList.contains("d-none");
    const fields = {
      duration: byClock ? "" : val(".time-add-duration"),
      start_time: byClock ? val(".time-add-start") : "",
      end_time: byClock ? val(".time-add-end") : "",
      spent_on: val(".time-add-date"),
      note: val(".time-add-note"),
      project_id: val(".time-add-project"),
      billable: billable && !billable.checked ? "0" : "1",
    };
    add.querySelectorAll(".tt-cf").forEach((el) => {
      fields["cf_" + el.dataset.key] = el.value.trim();
    });
    return fields;
  }

  async function logTime(add, ctx) {
    const fields = addFields(add);
    if (!fields.duration && !(fields.start_time && fields.end_time)) {
      // Nothing to book yet — put the cursor in whichever input is showing.
      const clock = add.querySelector(".time-add-clock");
      const byClock = clock && !clock.classList.contains("d-none");
      const first = add.querySelector(byClock ? ".time-add-start" : ".time-add-duration");
      if (first) first.focus();
      return;
    }
    await post(add.dataset.action, fields, ctx);
    const next = (ctx.block || document).querySelector(".time-add-duration");
    if (next) next.focus();
  }

  // ---- the stopwatch -------------------------------------------------------
  // Starting needs a billing project. When none can be derived the server
  // answers with the list instead of an error, and we ask right here.
  async function startTimer(btn, ctx) {
    const add = btn.closest(".time-add");
    const fields = {
      task_id: btn.dataset.taskId || "",
      project_id: btn.dataset.projectId ||
        (add ? (add.querySelector(".time-add-project") || {}).value : "") || "",
      note: btn.dataset.note !== undefined ? btn.dataset.note
        : (add ? (add.querySelector(".time-add-note") || {}).value : "") || "",
    };
    if (add) {
      add.querySelectorAll(".tt-cf").forEach((el) => {
        fields["cf_" + el.dataset.key] = el.value.trim();
      });
    }
    const data = await post(btn.dataset.action, fields, ctx);
    if (data && data.needs_project) askForProject(btn, ctx, data);
  }

  function askForProject(btn, ctx, data) {
    if (!data.projects || !data.projects.length) {
      alert("Create a customer and a billing project first (Time tracking → Customers).");
      return;
    }
    const menu = document.createElement("div");
    menu.className = "dropdown-menu show shadow tt-project-menu";
    menu.style.cssText = "position:absolute; z-index:1080; max-height:16rem; overflow:auto;";
    data.projects.forEach((p) => {
      const a = document.createElement("button");
      a.type = "button";
      a.className = "dropdown-item small";
      a.textContent = `${p.customer} › ${p.name}`;
      a.addEventListener("click", () => {
        menu.remove();
        post(btn.dataset.action, { task_id: data.task_id, project_id: p.id }, ctx)
          .catch(() => {});
      });
      menu.appendChild(a);
    });
    const rect = btn.getBoundingClientRect();
    menu.style.top = `${window.scrollY + rect.bottom + 4}px`;
    menu.style.left = `${window.scrollX + Math.max(8, rect.left - 120)}px`;
    document.body.appendChild(menu);
    setTimeout(() => document.addEventListener("click", function once(ev) {
      if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener("click", once); }
    }), 0);
  }

  // ---- clicks --------------------------------------------------------------
  document.addEventListener("click", (ev) => {
    const t = ev.target;

    const addBtn = t.closest(".time-add-btn");
    if (addBtn) {
      ev.preventDefault();
      const add = addBtn.closest(".time-add");
      logTime(add, contextFor(addBtn)).catch(() => {});
      return;
    }

    const start = t.closest(".tt-timer-start");
    if (start) {
      ev.preventDefault();
      startTimer(start, contextFor(start)).catch(() => {});
      return;
    }

    const stop = t.closest(".tt-timer-stop");
    if (stop) {
      ev.preventDefault();
      post(stop.dataset.action || (window.TT || {}).stop, {}, contextFor(stop)).catch(() => {});
      return;
    }

    const discard = t.closest(".tt-timer-discard");
    if (discard) {
      ev.preventDefault();
      if (!confirm("Discard the running timer without logging it?")) return;
      post(discard.dataset.action || (window.TT || {}).discard, {}, contextFor(discard)).catch(() => {});
      return;
    }

    const clockToggle = t.closest(".tt-clock-toggle");
    if (clockToggle) {
      ev.preventDefault();
      const on = localStorage.getItem(CLOCK_KEY) === "1";
      localStorage.setItem(CLOCK_KEY, on ? "0" : "1");
      applyClockMode();
      const add = clockToggle.closest(".time-add");
      const focus = add && add.querySelector(on ? ".time-add-duration" : ".time-add-start");
      if (focus) focus.focus();
      return;
    }

    const editToggle = t.closest(".tt-edit-toggle");
    if (editToggle) {
      ev.preventDefault();
      const row = editToggle.closest(".list-group-item");
      const form = row && row.querySelector(".tt-edit");
      if (form) {
        form.classList.toggle("d-none");
        const dur = form.querySelector(".tt-edit-duration");
        if (!form.classList.contains("d-none") && dur) dur.focus();
      }
      return;
    }

    const save = t.closest(".tt-edit-save");
    if (save) {
      ev.preventDefault();
      const form = save.closest(".tt-edit");
      const billable = form.querySelector(".tt-edit-billable");
      const startEl = form.querySelector(".tt-edit-start");
      const endEl = form.querySelector(".tt-edit-end");
      const payload = {
        duration: form.querySelector(".tt-edit-duration").value.trim(),
        start_time: startEl ? startEl.value : "",
        end_time: endEl ? endEl.value : "",
        spent_on: form.querySelector(".tt-edit-date").value,
        note: form.querySelector(".tt-edit-note").value.trim() || " ",
        billable: billable && !billable.checked ? "0" : "1",
      };
      form.querySelectorAll(".tt-edit-cf").forEach((el) => {
        payload["cf_" + el.dataset.key] = el.value.trim();
      });
      post(form.dataset.action, payload, contextFor(save)).catch(() => {});
      return;
    }

    const rm = t.closest(".time-delete");
    if (rm) {
      ev.preventDefault();
      post(rm.dataset.action, {}, contextFor(rm)).catch(() => {});
    }
  });

  // Enter in any capture input logs the entry instead of submitting the
  // surrounding inspector form.
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter") return;
    const input = ev.target.closest(
      ".time-add-duration, .time-add-date, .time-add-note, .time-add-project, " +
      ".time-add-start, .time-add-end, .tt-cf"
    );
    if (input) {
      ev.preventDefault();
      const add = input.closest(".time-add");
      logTime(add, contextFor(input)).catch(() => {});
      return;
    }
    const edit = ev.target.closest(
      ".tt-edit-duration, .tt-edit-note, .tt-edit-date, .tt-edit-start, " +
      ".tt-edit-end, .tt-edit-cf");
    if (edit) {
      ev.preventDefault();
      const save = edit.closest(".tt-edit").querySelector(".tt-edit-save");
      if (save) save.click();
    }
  });
})();
