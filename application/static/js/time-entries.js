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
    const report = el.closest("#tt-report");
    if (report) return { kind: "report", block: report, args: report.dataset.args };
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
    if (ctx.kind === "report") {
      fd.append("report_args", ctx.args || "");
      // Which team the report is showing rides in the filters; the endpoint
      // reads it from the body, so a swap can't fall back to the default team.
      const team = new URLSearchParams(ctx.args || "").get("team");
      if (team) fd.append("team", team);
    }
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

  // querySelectorAll only ever looks *below* the node it is called on, so a
  // scope that IS a capture box has to be added by hand — the clock toggle
  // hands us its own .time-add, and re-rendering nothing is how the toggle
  // came to do nothing at all.
  function addBoxes(scope) {
    const root = scope || document;
    const out = Array.prototype.slice.call(root.querySelectorAll(".time-add"));
    if (root.nodeType === 1 && root.matches(".time-add")) out.unshift(root);
    return out;
  }

  // ---- custom fields -------------------------------------------------------
  // Each <option> carries its project's field definitions, so switching the
  // project re-asks for the right things without a round trip. Values are
  // submitted as cf_<key>, which is what the server reads them back as.
  function renderCustomFields(scope) {
    addBoxes(scope).forEach((add) => {
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
          // A dropdown is as wide as its choices need; a free-text field is
          // where you actually type, so it takes the room that's left.
          el.style.flex = "0 1 auto";
          el.style.minWidth = "8rem";
          el.style.maxWidth = "14rem";
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
          el.style.flex = "1 1 10rem";
          el.style.minWidth = "7rem";
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
  // The *project* decides which one the form asks for (set on the billing
  // project, since it is a property of how that customer is billed, not of
  // whoever happens to be logging). The toggle stays as a one-off override —
  // for a forgotten entry you only remember the length of — and it lasts until
  // the project changes, at which point that project's way wins again.
  function projectMode(add) {
    const sel = add.querySelector(".time-add-project");
    const opt = sel && sel.selectedOptions[0];
    return (opt && opt.dataset.mode) === "clock" ? "clock" : "duration";
  }

  function applyClockMode(scope) {
    addBoxes(scope).forEach((add) => {
      const dur = add.querySelector(".time-dur") || add.querySelector(".time-add-duration");
      const clock = add.querySelector(".time-add-clock");
      if (!dur || !clock) return;
      const mode = projectMode(add);
      // An override only survives while the project it was made for is still
      // selected.
      if (add.dataset.overrideFor && add.dataset.overrideFor !== currentProject(add)) {
        delete add.dataset.override;
        delete add.dataset.overrideFor;
      }
      const on = add.dataset.override
        ? add.dataset.override === "clock" : mode === "clock";
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

  // Fill a clock field with the time right now — the common case when you
  // start or finish something and reach for the form.
  function stampNow(input) {
    if (!input) return;
    const d = new Date();
    input.value = `${String(d.getHours()).padStart(2, "0")}:` +
                  `${String(d.getMinutes()).padStart(2, "0")}`;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // ---- duration buttons ----------------------------------------------------
  // Mirrors services.timetracking.parse_duration, so ±15 can pick up whatever
  // is already in the field no matter which of the accepted spellings it is.
  // The field stays free text — the buttons only write into it.
  function parseDuration(raw) {
    const s = (raw || "").trim().toLowerCase().replace(/\s+/g, "");
    if (!s) return null;
    let m = s.match(/^(\d+):([0-5]?\d)$/);
    if (m) return parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
    m = s.match(/^(\d+(?:[.,]\d+)?)h$/);
    if (m) return Math.round(parseFloat(m[1].replace(",", ".")) * 60);
    m = s.match(/^(?:(\d+)h)?(?:(\d+)m?)?$/);
    if (m && (m[1] || m[2])) {
      return parseInt(m[1] || "0", 10) * 60 + parseInt(m[2] || "0", 10);
    }
    return null;
  }

  function formatDuration(minutes) {
    if (minutes <= 0) return "";
    const h = Math.floor(minutes / 60), m = minutes % 60;
    if (!h) return m + "m";
    return m ? h + "h" + m + "m" : h + "h";
  }

  function setDuration(field, minutes) {
    if (!field) return;
    field.value = formatDuration(minutes);
    field.dispatchEvent(new Event("input", { bubbles: true }));
  }

  // ---- picking a time with the mouse ---------------------------------------
  // A native <input type="time"> is a keyboard field: hh, tab, mm. Booking a
  // day happens on the quarter-hour grid, so the caret next to each clock
  // field offers exactly that grid. The end list is labelled with what each
  // choice makes the entry long — the number you are really deciding about.
  const TIME_STEP = 15;

  function toMinutes(hhmm) {
    const m = /^(\d{1,2}):([0-5]\d)$/.exec((hhmm || "").trim());
    return m ? parseInt(m[1], 10) * 60 + parseInt(m[2], 10) : null;
  }

  function toClock(minutes) {
    const m = ((minutes % 1440) + 1440) % 1440;
    return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  }

  function clockField(box, which) {
    return box.querySelector(which === "end" ? ".time-add-end, .tt-edit-end"
                                             : ".time-add-start, .tt-edit-start");
  }

  function timeChoices(which, startMin) {
    const out = [];
    // An end can only follow its start, and no shift runs past midnight here —
    // an overnight entry is still booked as a duration.
    if (which === "end" && startMin !== null) {
      for (let t = startMin + TIME_STEP; t < 1440; t += TIME_STEP) {
        out.push({ value: toClock(t), label: toClock(t), hint: formatDuration(t - startMin) });
      }
      return out;
    }
    for (let t = 0; t < 1440; t += TIME_STEP) {
      out.push({ value: toClock(t), label: toClock(t), hint: "" });
    }
    return out;
  }

  function openTimeMenu(btn) {
    const box = btn.closest(".time-add") || btn.closest(".tt-edit");
    if (!box) return;
    const which = btn.dataset.target === "end" ? "end" : "start";
    const field = clockField(box, which);
    if (!field) return;
    const startMin = which === "end" ? toMinutes((clockField(box, "start") || {}).value) : null;

    const menu = document.createElement("div");
    menu.className = "dropdown-menu show shadow tt-time-menu";
    menu.style.cssText = "position:absolute; z-index:1080; max-height:16rem; overflow:auto;";
    // The menu hangs off <body> so the panel's scroll box can't clip it, which
    // makes a click in it an *outside* click for whatever it was opened from.
    // This says otherwise — without it the inspector closes underneath the
    // menu and takes the half-filled form with it.
    menu.dataset.keepsInspectorOpen = "";
    // Open where the answer probably is: on what the field already says,
    // otherwise on the clock.
    const now = new Date();
    const focusOn = toMinutes(field.value);
    const near = focusOn !== null ? focusOn
      : (which === "end" && startMin !== null ? startMin + 60
                                              : now.getHours() * 60 + now.getMinutes());
    let nearest = null, nearestGap = Infinity;
    timeChoices(which, startMin).forEach((c) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "dropdown-item d-flex justify-content-between gap-3 small font-monospace";
      item.innerHTML = `<span>${c.label}</span>` +
        (c.hint ? `<span class="text-muted">${c.hint}</span>` : "");
      if (c.value === field.value) item.classList.add("active");
      item.addEventListener("click", () => {
        menu.remove();
        field.value = c.value;
        field.dispatchEvent(new Event("change", { bubbles: true }));
      });
      menu.appendChild(item);
      const gap = Math.abs(toMinutes(c.value) - near);
      if (gap < nearestGap) { nearestGap = gap; nearest = item; }
    });
    if (!menu.childElementCount) return;

    const rect = btn.getBoundingClientRect();
    menu.style.top = `${window.scrollY + rect.bottom + 4}px`;
    menu.style.left = `${window.scrollX + rect.left - 60}px`;
    menu.style.minWidth = "9rem";
    document.body.appendChild(menu);
    if (nearest) menu.scrollTop = Math.max(0, nearest.offsetTop - menu.clientHeight / 2);
    setTimeout(() => document.addEventListener("click", function once(ev) {
      if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener("click", once); }
    }), 0);
  }

  function currentProject(add) {
    const sel = add.querySelector(".time-add-project");
    return sel ? sel.value : "";
  }

  function ensureDefaultDate(scope) {
    (scope || document).querySelectorAll('.time-add-date[type="date"]').forEach((inp) => {
      if (!inp.value) inp.value = new Date().toISOString().slice(0, 10);
    });
  }

  function arm(scope) {
    ensureDefaultDate(scope);
    startTicking(scope);
    renderCustomFields(scope);
    applyClockMode(scope);
  }
  arm();

  // The off-canvas inspector injects its body after load and <main> is swapped
  // wholesale, so newly inserted capture markup has to be armed.
  //
  // This MUST only react to *our* markup appearing. Two of the four steps write
  // to the DOM themselves — the clock rewrites its own text once a second, and
  // the custom-field box is rebuilt — so a callback that re-armed on any
  // mutation re-triggered itself forever and locked the tab up. Matching on
  // added elements keeps that from happening: the ticking clock only ever adds
  // a text node, and the inputs we generate match nothing here.
  const ARM_SELECTOR = ".time-add, .tt-timer-elapsed, .time-block, #time, #tt-day, #tt-chip";
  let queued = false;
  new MutationObserver((records) => {
    if (queued) return;
    const relevant = records.some((r) =>
      Array.prototype.some.call(r.addedNodes, (n) =>
        n.nodeType === 1 &&
        ((n.matches && n.matches(ARM_SELECTOR)) ||
         (n.querySelector && n.querySelector(ARM_SELECTOR)))));
    if (!relevant) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; arm(); });
  }).observe(document.body, { childList: true, subtree: true });

  document.addEventListener("change", (ev) => {
    if (ev.target.closest(".time-add-project")) {
      renderCustomFields();
      applyClockMode();
    }
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
    menu.dataset.keepsInspectorOpen = "";
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

  // ---- re-filing an entry --------------------------------------------------
  // The report's editor can move an entry onto another billing project. The
  // options live in one <template> on the page, so they are cloned in on first
  // open rather than repeated in every row. An entry sitting on an archived or
  // deleted project is not in that list — its own project is prepended, so
  // saving can never move it somewhere nobody picked.
  function fillProjectSelect(form) {
    const sel = form.querySelector(".tt-edit-project");
    const tpl = document.getElementById("tt-edit-projects");
    if (!sel || !tpl || sel.options.length) return;
    sel.appendChild(tpl.content.cloneNode(true));
    const pid = sel.dataset.project || "";
    sel.value = pid;
    if (sel.value !== pid) {
      const own = document.createElement("option");
      own.value = pid;
      own.textContent = sel.dataset.label || "(current project)";
      sel.insertBefore(own, sel.firstChild);
      sel.value = pid;
    }
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

    const chip = t.closest(".tt-dur-chip");
    if (chip) {
      ev.preventDefault();
      const add = chip.closest(".time-add");
      setDuration(add && add.querySelector(".time-add-duration"),
                  parseInt(chip.dataset.minutes, 10) || 0);
      // The length is settled; the note is the only thing left to say.
      const note = add && add.querySelector(".time-add-note");
      if (note) note.focus();
      return;
    }

    const step = t.closest(".tt-dur-step");
    if (step) {
      ev.preventDefault();
      const add = step.closest(".time-add");
      const field = add && add.querySelector(".time-add-duration");
      if (!field) return;
      const by = parseInt(step.dataset.step, 10) || 0;
      const have = parseDuration(field.value) || 0;
      // Land on the quarter hour rather than beside it: 47m steps up to 1h,
      // not to 1h2m.
      const grid = by > 0 ? Math.floor(have / 15) * 15 : Math.ceil(have / 15) * 15;
      setDuration(field, Math.max(0, grid + by));
      return;
    }

    const pick = t.closest(".tt-time-pick");
    if (pick) {
      ev.preventDefault();
      document.querySelectorAll(".tt-time-menu").forEach((m) => m.remove());
      openTimeMenu(pick);
      return;
    }

    const now = t.closest(".tt-now");
    if (now) {
      ev.preventDefault();
      const add = now.closest(".time-add") || now.closest(".tt-edit");
      const target = now.dataset.target === "end" ? ".time-add-end, .tt-edit-end"
                                                  : ".time-add-start, .tt-edit-start";
      stampNow(add && add.querySelector(target));
      return;
    }

    const clockToggle = t.closest(".tt-clock-toggle");
    if (clockToggle) {
      ev.preventDefault();
      const add = clockToggle.closest(".time-add");
      if (!add) return;
      const clock = add.querySelector(".time-add-clock");
      const wasClock = clock && !clock.classList.contains("d-none");
      add.dataset.override = wasClock ? "duration" : "clock";
      add.dataset.overrideFor = currentProject(add);
      applyClockMode(add);
      const focus = add.querySelector(wasClock ? ".time-add-duration" : ".time-add-start");
      if (focus) focus.focus();
      return;
    }

    const editToggle = t.closest(".tt-edit-toggle");
    if (editToggle) {
      ev.preventDefault();
      // The day sheet keeps the edit form inside the entry; the report puts it
      // in the next table row.
      const row = editToggle.closest(".list-group-item, .tt-report-row");
      const form = row && (row.querySelector(".tt-edit")
                           || (row.nextElementSibling
                               && row.nextElementSibling.classList.contains("tt-edit")
                               ? row.nextElementSibling : null));
      if (form) {
        form.classList.toggle("d-none");
        if (!form.classList.contains("d-none")) fillProjectSelect(form);
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
      const projEl = form.querySelector(".tt-edit-project");
      const payload = {
        project_id: projEl ? projEl.value : "",
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

    const restore = t.closest(".time-restore");
    if (restore) {
      ev.preventDefault();
      post(restore.dataset.action, {}, contextFor(restore)).catch(() => {});
      return;
    }

    const rm = t.closest(".time-delete");
    if (rm) {
      ev.preventDefault();
      // Booked time is worth a question — it vanishes from the sheet with one
      // stray click otherwise, and the row says nothing about how much.
      const what = rm.dataset.confirm || "Remove this time entry?";
      if (!confirm(what)) return;
      post(rm.dataset.action, {}, contextFor(rm)).catch(() => {});
    }
  });

  // The bulk field editor: show the control that belongs to the chosen field.
  // A dropdown field must be offered as a dropdown, so there is one control per
  // field and only the active one is enabled — a disabled control is not
  // submitted, which is what keeps exactly one value in the request.
  document.addEventListener("change", (ev) => {
    const picker = ev.target.closest(".tt-field-key");
    if (!picker) return;
    const key = (picker.value || "").split("|")[0];
    const scope = picker.closest("form") || document;
    scope.querySelectorAll(".tt-field-value").forEach((el) => {
      const mine = el.dataset.field === key;
      el.disabled = !mine;
      el.hidden = !mine;
    });
  });

  // "Mark selected billed" needs something to select — one tick for the lot.
  document.addEventListener("change", (ev) => {
    const all = ev.target.closest("#tt-select-all");
    if (!all) return;
    const scope = all.closest("form") || document;
    scope.querySelectorAll(".tt-pick").forEach((cb) => { cb.checked = all.checked; });
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
