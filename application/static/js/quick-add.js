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
  function updateProjectHint() {
    // A #project token typed into the text overrides the dropdown.
    const tokenMatch = input.value.match(/(?<!\S)#([\w\-äöüÄÖÜß]+)\b/);
    if (tokenMatch) {
      projectHint.innerHTML =
        `<i class="bi bi-hash"></i> The <code>#${escape(tokenMatch[1])}</code> token overrides the project above.`;
      projectHint.className = "form-text mt-1 text-info";
      return;
    }
    const label = projectInput.options[projectInput.selectedIndex].textContent.trim();
    projectHint.innerHTML =
      `<i class="bi bi-folder"></i> Goes into <strong>${escape(label)}</strong>. Type <code>#project</code> to override.`;
    projectHint.className = "form-text mt-1 text-muted";
  }
  projectInput.addEventListener("change", updateProjectHint);
  // Enter in the picker's search box takes the highlighted project (↓/↑ walk
  // it, searchable-select.js) and hands the caret back to the title, so the
  // next Enter adds the task.
  modalEl.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" || ev.metaKey || ev.ctrlKey || ev.isComposing) return;
    if (!ev.target.matches || !ev.target.matches(".searchable-select-input")) return;
    if (!projectInput.parentNode.contains(ev.target)) return;
    input.focus();
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
  // A small dropdown appears while typing either token at the caret; picking
  // an entry replaces the partial token with the whole one. `@` opens the same
  // teammate list as an @-mention in a comment, because `@` means a person
  // everywhere. Tags share the box because typing one blind is how they
  // silently ended up unassigned: the parser only takes a tag that exists, and
  // a name with spaces has to be written quoted (`+"Review AI Work"`) — the
  // list writes that for you.
  // (`qa-assignee-ac` is also the comment @-mention box's class.)
  //
  // **Nothing in the list is highlighted until the user arrows into it.** This
  // box completes a token inside a sentence the user is writing, and the two
  // keys that would take the highlighted entry — Enter and Tab — already mean
  // "create the task" and "leave the field" in this input. A pre-selected first
  // row makes Enter hand over a tag or a person that was never picked, and it
  // does it silently, because `+cair` is also perfectly good text on its own.
  // A command bar is the other case and keeps its pre-selection: quickjump
  // (`quickjump.js`), the `/` menu and the `[[ ]]` completion open BECAUSE a
  // list was asked for, and there Enter can mean nothing else. So: `acIndex`
  // stays -1 until ↓/↑ moves it, and while it is -1 the dropdown handles no
  // key the field wanted for itself.
  const acBox = document.createElement("div");
  acBox.className = "qa-assignee-ac d-none";
  (previewEl.parentNode || modalEl).insertBefore(acBox, previewEl);
  let acItems = [];     // [{ token, html }]
  let acIndex = -1;
  let acToken = null;   // { start, end, query, kind }
  let acSeq = 0;

  const AC_SOURCES = {
    user: {
      // Either half of a quoted display name, or a bare word.
      re: /(?:^|\s)(@"[^"\n]*|@[\w.\-äöüÄÖÜß]*)$/,
      url: q => `/tasks/assignees?q=${encodeURIComponent(q)}`,
      items: data => (data.users || []).map(u => ({
        token: "@" + u.username,
        html: '<i class="bi bi-person"></i> '
          + (u.display_name && u.display_name !== u.username
             ? `${escape(u.username)} <span class="text-muted">${escape(u.display_name)}</span>`
             : escape(u.username))
          + (u.is_me ? ' <span class="text-muted">(me)</span>' : ""),
      })),
    },
    tag: {
      // Either half of a quoted name, or a bare word — colons included, so the
      // list keeps answering while `+AI:local` is being typed.
      re: /(?:^|\s)(\+"[^"\n]*|\+[\w\-äöüÄÖÜß:]*)$/,
      url: q => `/tasks/tag-names?q=${encodeURIComponent(q)}`,
      items: data => (data.tags || []).map(t => ({
        token: t.token,
        // The tag's own colour, but only if it really is one — this goes in
        // through innerHTML, so anything else is dropped rather than trusted.
        html: `<i class="bi bi-tag"${/^#[0-9a-f]{3,8}$/i.test(t.color || "")
                 ? ` style="color: ${t.color}"` : ""}></i> ${escape(t.name)}`,
      })),
    },
    project: {
      // `#` is a search in the project picker below, straight away. The list
      // is that picker's own options (no round trip), and picking one sets the
      // picker and drops the `#query` from the title: a `#name` token only
      // reaches projects whose name is one word, the picker reaches them all.
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
  };

  function pickProject(value) {
    // An old filter in the picker's search box would hide the option picked.
    const projectSearch = projectInput.parentNode.querySelector(".searchable-select-input");
    if (projectSearch && projectSearch.value) {
      projectSearch.value = "";
      projectSearch.dispatchEvent(new Event("input"));
    }
    projectInput.value = value;
    projectInput.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function tokenAtCaret() {
    const pos = input.selectionStart || 0;
    const before = input.value.slice(0, pos);
    for (const [kind, src] of Object.entries(AC_SOURCES)) {
      const m = before.match(src.re);
      if (!m) continue;
      const raw = m[1];
      const query = /^[@+#]"/.test(raw) ? raw.slice(2) : raw.slice(1);
      return { start: pos - raw.length, end: pos, query, kind };
    }
    return null;
  }

  function hideAc() {
    acBox.classList.add("d-none");
    acBox.innerHTML = "";
    acItems = []; acIndex = -1; acToken = null;
  }

  function renderAc() {
    if (!acItems.length) { hideAc(); return; }
    acBox.innerHTML = acItems.map((it, i) =>
      `<button type="button" class="qa-ac-item${i === acIndex ? " active" : ""}" `
      + `data-i="${i}">${it.html}</button>`).join("");
    acBox.classList.remove("d-none");
  }

  async function refreshAc() {
    const tok = tokenAtCaret();
    if (!tok) { hideAc(); return; }
    acToken = tok;
    const src = AC_SOURCES[tok.kind];
    const seq = ++acSeq;
    if (src.local) {
      acItems = src.local(tok.query);
      acIndex = -1;
      renderAc();
      return;
    }
    try {
      const resp = await fetch(src.url(tok.query), { credentials: "same-origin" });
      if (!resp.ok || seq !== acSeq) return;
      acItems = src.items(await resp.json());
      acIndex = -1;
      renderAc();
    } catch (e) { hideAc(); }
  }

  function applyAc(i) {
    const item = acItems[i];
    if (!item || !acToken) return;
    const v = input.value;
    if (item.pick) item.pick();
    const replacement = item.token ? item.token + " " : "";
    input.value = v.slice(0, acToken.start) + replacement + v.slice(acToken.end);
    const caret = acToken.start + replacement.length;
    input.setSelectionRange(caret, caret);
    hideAc();
    preview();
    updateProjectHint();
    input.focus();
  }

  input.addEventListener("input", refreshAc);
  input.addEventListener("keydown", (ev) => {
    if (acBox.classList.contains("d-none") || !acItems.length) return;
    if (ev.key === "ArrowDown") {
      // From "nothing highlighted" (-1) this lands on the first entry.
      ev.preventDefault(); acIndex = (acIndex + 1) % acItems.length; renderAc();
    } else if (ev.key === "ArrowUp") {
      // …and this one on the last, rather than one short of it.
      ev.preventDefault(); acIndex = (acIndex <= 0 ? acItems.length : acIndex) - 1; renderAc();
    } else if (ev.key === "Enter" || ev.key === "Tab") {
      // ⌘/Ctrl+Enter is "add the task" and is never a completion, even with an
      // entry highlighted — it is the one key that has to work from every field.
      if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) { hideAc(); return; }
      // Only once an entry is actually highlighted. Otherwise Enter submits the
      // task and Tab moves on, which is what the user pressed them for.
      if (acIndex < 0) return;
      ev.preventDefault(); ev.stopPropagation(); applyAc(acIndex);
    } else if (ev.key === "Escape") {
      // Closes the list, not the dialog: the next Escape does that.
      ev.preventDefault(); ev.stopPropagation(); hideAc();
    }
  });
  acBox.addEventListener("mousedown", (ev) => {
    const btn = ev.target.closest(".qa-ac-item");
    if (!btn) return;
    ev.preventDefault();
    applyAc(parseInt(btn.dataset.i, 10));
  });
  input.addEventListener("blur", () => setTimeout(hideAc, 120));

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
    // The picker's search box (searchable-select.js) keeps its filter between
    // openings; an empty one shows every option again, so the preselection
    // below can find its project and the list starts complete.
    const projectSearch = projectInput.parentNode.querySelector(".searchable-select-input");
    if (projectSearch && projectSearch.value) {
      projectSearch.value = "";
      projectSearch.dispatchEvent(new Event("input"));
    }
    // Only select it if it's actually an option (e.g. a folder context has
    // no matching project option — fall back to Inbox).
    projectInput.value = wanted;
    if (projectInput.value !== wanted) projectInput.value = "";
    updateProjectHint();
    input.value = "";
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
