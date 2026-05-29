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
  const previewEl = modalEl.querySelector("#quickAddPreview");
  const projectInput = modalEl.querySelector("#quickAddProject");
  const projectHint = modalEl.querySelector("#quickAddProjectHint");
  const continueCb = modalEl.querySelector("#quickAddContinue");

  // ----- Detect the current project -----
  function detectCurrentProject() {
    const m = window.location.pathname.match(/^\/projects\/([0-9a-f]{24})/i);
    return m ? m[1] : "";
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

  // ----- Light client-side preview parser (mirrors server tokens) -----
  const PREVIEW_PATTERNS = [
    { re: /(?<!\S)p([1-4])(?!\S)/i, label: m => `P${m[1]}`, cls: "qa-prio" },
    { re: /(?<!\S)@([\w\-äöüÄÖÜß]+)\b/g, label: m => `@${m[1]}`, cls: "qa-tag" },
    { re: /(?<!\S)#([\w\-äöüÄÖÜß]+)\b/g, label: m => `#${m[1]}`, cls: "qa-project" },
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
  input.addEventListener("input", () => { preview(); updateProjectHint(); });

  // ----- Submit -----
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const text = input.value.trim();
    if (!text) return;
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
      showSuccess(data.task, data.warnings || {});
      if (continueCb.checked) {
        input.value = "";
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
    updateProjectHint();
    input.value = "";
    previewEl.classList.add("d-none");
    setTimeout(() => input.focus(), 50);
  });

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
