/* Quick-add modal — Todoist-style.
 *
 * - Opens on the sidebar "New task" button, on `q` keypress, or any
 *   element with [data-quickadd-trigger].
 * - Pre-fills project from the current URL (when on a project page).
 * - Live preview parses tokens client-side; the server does the
 *   authoritative parse on submit.
 * - "Add another after this" keeps the modal open so the user can
 *   rapid-fire enter several tasks in a row.
 */
(function () {
  const modalEl = document.getElementById("quickAddModal");
  if (!modalEl) return;
  const modal = bootstrap.Modal.getOrCreateInstance(modalEl, { keyboard: true });
  const form = modalEl.querySelector("#quickAddForm");
  const input = modalEl.querySelector("#quickAddText");
  const noteEl = modalEl.querySelector("#quickAddNote");
  const pending = modalEl.querySelector("[data-pending-attachments]");
  const previewEl = modalEl.querySelector("#quickAddPreview");
  const projectInput = modalEl.querySelector("#quickAddProject");
  const projectHint = modalEl.querySelector("#quickAddProjectHint");
  const projectCombo = modalEl.querySelector("#quickAddProjectSearch");
  const projectList = modalEl.querySelector("#quickAddProjectList");
  const continueCb = modalEl.querySelector("#quickAddContinue");

  // ----- Detect the current project -----
  function detectCurrentProject() {
    // On a project page the pk is in the path…
    const m = window.location.pathname.match(/^\/projects\/([0-9a-f]{24})/i);
    if (m) return m[1];
    // …on every other view (perspectives, tags, today, …) the selected
    // project comes through the sidebar filter as ?project=<pk>, so New task
    // still lands in the project the user is looking at.
    const qp = new URLSearchParams(window.location.search).get("project");
    if (qp && /^[0-9a-f]{24}$/i.test(qp)) return qp;
    return "";
  }
  function projectLabel() {
    const o = projectInput.options[projectInput.selectedIndex];
    return o ? o.textContent.trim() : "";
  }
  function updateProjectHint() {
    // The field itself says where the task goes; the one thing left to say
    // beside it is that a #project token typed into the title wins over it.
    if (document.activeElement !== projectCombo) projectCombo.value = projectLabel();
    const tokenMatch = input.value.match(/(?<!\S)#([\w\-äöüÄÖÜß]+)\b/);
    projectHint.innerHTML = tokenMatch
      ? `<i class="bi bi-hash"></i> <code>#${escape(tokenMatch[1])}</code> overrides this`
      : "";
  }
  projectInput.addEventListener("change", updateProjectHint);

  // ----- The project field: one input that shows the project and searches -----
  // It reads as the current project; focusing it selects that text, so typing
  // replaces it with a search and the list below filters live. Unlike the
  // @/+/# list in the title, the first match IS highlighted: this list opens
  // because a project was asked for, so Enter can mean nothing else. Enter
  // (or a click) takes it and hands the caret back to the title, where the
  // next Enter adds the task; Tab takes it and moves on; Escape closes the
  // list and puts the current project back (the next Escape closes the
  // dialog). Leaving the field without a pick keeps the project it had.
  let comboItems = [];   // <option>s shown
  let comboIndex = -1;

  function comboOpen() { return !projectList.classList.contains("d-none"); }
  function closeCombo() {
    projectList.classList.add("d-none");
    projectList.innerHTML = "";
    projectCombo.setAttribute("aria-expanded", "false");
    comboItems = []; comboIndex = -1;
  }
  function renderCombo() {
    if (!comboItems.length) {
      projectList.innerHTML = '<div class="qa-ac-item text-muted">No project matches</div>';
    } else {
      projectList.innerHTML = comboItems.map((o, i) =>
        `<button type="button" tabindex="-1" role="option" class="qa-ac-item${i === comboIndex ? " active" : ""}" `
        + `data-i="${i}"><i class="bi ${o.value ? "bi-folder" : "bi-inbox"}"></i> `
        + `${escape(o.textContent.trim())}</button>`).join("");
      const active = projectList.querySelector(".qa-ac-item.active");
      if (active) active.scrollIntoView({ block: "nearest" });
    }
    projectList.classList.remove("d-none");
    projectCombo.setAttribute("aria-expanded", "true");
  }
  function filterCombo(q) {
    const needle = q.trim().toLowerCase();
    const opts = Array.from(projectInput.options);
    comboItems = needle ? opts.filter(o => o.textContent.toLowerCase().includes(needle)) : opts;
    comboIndex = needle ? (comboItems.length ? 0 : -1)
      : comboItems.findIndex(o => o.value === projectInput.value);
    renderCombo();
  }
  function takeCombo(i) {
    const o = comboItems[i];
    closeCombo();
    if (o) pickProject(o.value);
    projectCombo.value = projectLabel();
  }

  projectCombo.addEventListener("focus", () => {
    projectCombo.select();
    filterCombo("");
  });
  projectCombo.addEventListener("input", () => filterCombo(projectCombo.value));
  projectCombo.addEventListener("keydown", (ev) => {
    if (ev.isComposing) return;
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      if (!comboOpen()) { filterCombo(""); return; }
      if (!comboItems.length) return;
      const n = comboItems.length;
      comboIndex = ev.key === "ArrowDown"
        ? (comboIndex + 1) % n
        : (comboIndex <= 0 ? n : comboIndex) - 1;
      renderCombo();
    } else if (ev.key === "Enter") {
      // ⌘/Ctrl+Enter adds the task (form handler), with the project it has.
      if (ev.metaKey || ev.ctrlKey) { closeCombo(); return; }
      ev.preventDefault();
      if (comboOpen() && comboIndex >= 0) takeCombo(comboIndex);
      else { closeCombo(); projectCombo.value = projectLabel(); }
      input.focus();
    } else if (ev.key === "Tab") {
      if (comboOpen() && comboIndex >= 0) takeCombo(comboIndex);
    } else if (ev.key === "Escape") {
      if (!comboOpen()) return;
      ev.preventDefault(); ev.stopPropagation();
      closeCombo();
      projectCombo.value = projectLabel();
      projectCombo.select();
    }
  });
  projectList.addEventListener("mousedown", (ev) => {
    const btn = ev.target.closest("button.qa-ac-item");
    if (!btn) return;
    ev.preventDefault();
    takeCombo(parseInt(btn.dataset.i, 10));
    input.focus();
  });
  projectCombo.addEventListener("blur", () => {
    setTimeout(() => {
      if (document.activeElement === projectCombo) return;
      closeCombo();
      projectCombo.value = projectLabel();
    }, 120);
  });

  // ----- Light client-side preview parser (mirrors server tokens) -----
  const PREVIEW_PATTERNS = [
    { re: /(?<!\S)p([1-4])(?!\S)/i, label: m => `P${m[1]}`, cls: "qa-prio" },
    { re: /(?<!\S)\+"([^"\n]+)"/g, label: m => `+${m[1]}`, cls: "qa-tag" },
    // A colon INSIDE the name belongs to the tag (`+AI:local`) — same rule as
    // `quick_parse.TAG_RE`, which is the parse that counts.
    { re: /(?<!\S)\+([\w\-äöüÄÖÜß]+(?::[\w\-äöüÄÖÜß]+)*)\b/g, label: m => `+${m[1]}`, cls: "qa-tag" },
    { re: /(?<!\S)#([\w\-äöüÄÖÜß]+)\b/g, label: m => `#${m[1]}`, cls: "qa-project" },
    { re: /(?<!\S)@"([^"\n]+)"/g, label: m => `@${m[1]}`, cls: "qa-assignee" },
    { re: /(?<!\S)@([\w.\-äöüÄÖÜß]+)\b/g, label: m => `@${m[1]}`, cls: "qa-assignee" },
    { re: /(?<!\S)!(?!\S)/, label: () => "flagged", cls: "qa-flag" },
    { re: /\b(today|tomorrow|yesterday|heute|morgen|gestern)\b/i,
      label: m => m[1].toLowerCase(), cls: "qa-date" },
    { re: /\bin\s+(\d+)\s+(?:day|days|tag|tage)\b/i,
      label: m => `in ${m[1]} days`, cls: "qa-date" },
    { re: /\b(next\s+week|naechste\s+woche)\b/i,
      label: () => "next week", cls: "qa-date" },
    { re: /\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun|montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\b/i,
      label: m => m[1].toLowerCase(), cls: "qa-date" },
    { re: /\b(\d{4}-\d{2}-\d{2})\b/, label: m => m[1], cls: "qa-date" },
    { re: /\b(\d{1,2}\.\d{1,2}\.\d{4})\b/, label: m => m[1], cls: "qa-date" },
  ];

  function preview() {
    const text = input.value;
    if (!text) { previewEl.classList.add("d-none"); return; }
    const chips = [];
    for (const { re, label, cls } of PREVIEW_PATTERNS) {
      if (re.global) {
        for (const m of text.matchAll(re)) {
          chips.push(`<span class="qa-chip ${cls}">${escape(label(m))}</span>`);
        }
      } else {
        const m = text.match(re);
        if (m) chips.push(`<span class="qa-chip ${cls}">${escape(label(m))}</span>`);
      }
    }
    if (chips.length) {
      previewEl.classList.remove("d-none");
      previewEl.innerHTML = chips.join(" ");
    } else {
      previewEl.classList.add("d-none");
    }
  }
  function escape(s) {
    return String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }
  // Local wall-clock "now" as a datetime-local value. The server attaches the
  // user's tz to naive values, so browser-local time keeps "today" honest.
  function nowLocalDatetime() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
      + `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  const planEl = modalEl.querySelector("#quickAddPlan");
  // Re-apply the "plan for today" prefill (used by the Today view's New task
  // button) — called on open and after each "Add another" clear.
  function applyPlanToday() {
    if (modalEl._planToday && planEl) planEl.value = nowLocalDatetime();
  }
  input.addEventListener("input", () => { preview(); updateProjectHint(); });

  // ----- Autocomplete for the @person and +tag tokens -----
  // `token-complete.js` (shared with the project sketch): `@` opens the same
  // teammate list as an @-mention in a comment, because `@` means a person
  // everywhere, and `+` lists tags, because typing one blind is how they
  // silently ended up unassigned (the parser only takes a tag that exists, and
  // a name with spaces has to be written quoted, `+"Review AI Work"`, which the
  // list writes for you). Nothing is highlighted until the user arrows into
  // the list: Enter and Tab already mean "create the task" and "leave the
  // field" in this input. Quick Add adds one kind of its own: `#` searches the
  // project field below, straight away. The list is that field's own options
  // (no round trip), and picking one sets the field and drops the `#query`
  // from the title: a `#name` token only reaches projects whose name is one
  // word, the picker reaches them all.
  window.CairnTokenComplete.attach(input, {
    place: box => (previewEl.parentNode || modalEl).insertBefore(box, previewEl),
    onApply: () => { preview(); updateProjectHint(); },
    sources: {
      project: {
        re: /(?:^|\s)(#"[^"\n]*|#[\w\-äöüÄÖÜß]*)$/,
        local: q => {
          const needle = q.trim().toLowerCase();
          return Array.from(projectInput.options)
            .filter(o => !needle || o.textContent.toLowerCase().includes(needle))
            .slice(0, 20)
            .map(o => ({
              token: "",
              pick: () => pickProject(o.value),
              html: `<i class="bi bi-folder"></i> ${escape(o.textContent.trim())}`,
            }));
        },
      },
    },
  });

  function pickProject(value) {
    projectInput.value = value;
    projectInput.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // ----- Submit -----
  // The title field finishes the dialog on plain Enter, because it is a
  // single-line input in a form that has a submit button — the browser does
  // that, not us. Nothing BELOW it does: the note is a textarea where Enter is
  // a newline (and has to stay one), and a `datetime-local` or a `<select>`
  // keep Enter for themselves. So once the caret had left the first field the
  // only way to add the task was the mouse — the button sits 40 tab stops past
  // the note, behind three date pickers with seven stops each.
  //
  // **⌘/Ctrl+Enter adds the task from anywhere in the dialog.** Same key as the
  // inspector's task form (`inspector.js`) and the note editors, so there is
  // one "save this from the keyboard" chord in the app rather than three.
  let submitting = false;
  form.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" || ev.isComposing) return;
    if (!(ev.metaKey || ev.ctrlKey)) return;
    ev.preventDefault();
    if (typeof form.requestSubmit === "function") form.requestSubmit();
    else form.dispatchEvent(new Event("submit", { cancelable: true }));
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const text = input.value.trim();
    // An empty title is not an error worth a banner, but it must put the caret
    // back where the text goes — otherwise ⌘+Enter from the note field looks
    // like nothing happened at all.
    if (!text) { input.focus(); return; }
    if (submitting) return;
    submitting = true;
    const submitBtn = form.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    try {
      const fd = new FormData(form);
      const resp = await fetch("/tasks/quick-add", {
        method: "POST",
        body: fd,
        credentials: "same-origin",
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        showError(data.error || `Server error: ${resp.status}`);
        return;
      }
      // Upload any held attachments to the freshly-created task (they append
      // themselves to its note server-side), before any reload.
      if (pending && window.PendingAttachments &&
          window.PendingAttachments.count(pending) && data.task && data.task.id) {
        await window.PendingAttachments.flush(pending, data.task.id);
      }
      showSuccess(data.task, data.warnings || {});
      if (continueCb.checked) {
        input.value = "";
        if (noteEl) noteEl.value = "";
        modalEl.querySelectorAll('input[type="datetime-local"]').forEach((i) => { i.value = ""; });
        applyPlanToday();
        if (pending && window.PendingAttachments) window.PendingAttachments.clear(pending);
        previewEl.classList.add("d-none");
        input.focus();
        // Refresh the main content in the background so the new task
        // shows up under the inspector / list.
        document.dispatchEvent(new Event("bktodo:refresh-needed"));
      } else {
        modal.hide();
        window.location.reload();
      }
    } catch (err) {
      showError(`Network error: ${err.message}`);
    } finally {
      submitting = false;
      submitBtn.disabled = false;
    }
  });

  function showSuccess(task, warnings) {
    const banner = document.createElement("div");
    banner.className =
      "alert alert-success position-fixed top-0 start-50 translate-middle-x mt-3 shadow";
    banner.style.zIndex = 2100;
    let extra = "";
    if (warnings.unresolved_tags && warnings.unresolved_tags.length) {
      extra += ` (unresolved tags: ${warnings.unresolved_tags.join(", ")})`;
    }
    if (warnings.unresolved_project) {
      extra += ` (project "${warnings.unresolved_project}" not found)`;
    }
    if (warnings.unresolved_assignee) {
      extra += ` (person "${warnings.unresolved_assignee}" not found)`;
    }
    banner.textContent = `Added: ${task.title}${extra}`;
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

  // ----- Triggers -----
  modalEl.addEventListener("show.bs.modal", (ev) => {
    // The trigger may carry an explicit ``data-quickadd-project`` attribute
    // (e.g., the "+" next to a project row in the list). That wins over
    // URL-based detection.
    const trigger = ev.relatedTarget;
    let wanted = "";
    if (trigger && trigger.dataset && "quickaddProject" in trigger.dataset) {
      wanted = trigger.dataset.quickaddProject || "";
    } else {
      wanted = detectCurrentProject();
    }
    // Only select it if it's actually an option (e.g. a folder context has
    // no matching project option — fall back to Inbox).
    projectInput.value = wanted;
    if (projectInput.value !== wanted) projectInput.value = "";
    closeCombo();
    input.value = "";
    updateProjectHint();
    if (noteEl) noteEl.value = "";
    modalEl.querySelectorAll('input[type="datetime-local"]').forEach((i) => { i.value = ""; });
    // Today view's "New task" carries data-quickadd-plan="today" → pre-plan the
    // task for today so it stays on Today while still landing in the Inbox.
    modalEl._planToday = !!(trigger && trigger.dataset
                            && trigger.dataset.quickaddPlan === "today");
    applyPlanToday();
    if (pending && window.PendingAttachments) window.PendingAttachments.clear(pending);
    previewEl.classList.add("d-none");
  });

  // Focus belongs in ``shown``, not ``show``: Bootstrap's focus trap calls
  // ``.focus()`` on the modal element itself at the end of the fade, so
  // anything focused earlier (a timeout inside ``show.bs.modal``) loses the
  // caret again and the user types into nothing. ``shown.bs.modal`` fires
  // right after that, which is the first moment the input can keep it.
  modalEl.addEventListener("shown.bs.modal", () => input.focus());

  // `q` shortcut from anywhere (not in inputs).
  document.addEventListener("keydown", (ev) => {
    if (ev.target.matches("input, textarea, select, [contenteditable]")) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey || ev.shiftKey) return;
    if (ev.key && ev.key.toLowerCase() === "q") {
      ev.preventDefault();
      modal.show();
    }
  });
})();
