/* Sub-task widget (tasks/_subtasks.html). Delegated on document so it works
 * wherever the partial renders — the detail page, the inline inspector, and
 * the offcanvas inspector — and survives the background <main> swap.
 * The parent stays a task (the /subtasks endpoint never converts it). */
(function () {
  "use strict";
  function csrf() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }
  async function refresh(wrap) {
    if (!wrap || !wrap.dataset.refreshUrl) return;
    try {
      const r = await fetch(wrap.dataset.refreshUrl, { headers: { "X-Partial": "1" }, credentials: "same-origin" });
      if (r.ok) wrap.outerHTML = await r.text();
    } catch (e) { /* leave as-is */ }
  }
  async function addSubtask(input) {
    const box = input.closest(".subtask-add");
    const wrap = input.closest(".task-subtasks");
    const title = input.value.trim();
    if (!title || !box) return;
    input.disabled = true;
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    fd.append("title", title);
    try {
      const r = await fetch(box.dataset.action, {
        method: "POST", body: fd, credentials: "same-origin", headers: { "X-Partial": "1" },
      });
      if (r.ok) {
        const html = await r.text();
        wrap.outerHTML = html;
        // re-focus the fresh input so you can keep typing subtasks
        const fresh = document.querySelector('.task-subtasks[data-refresh-url="' + wrap.dataset.refreshUrl + '"] .subtask-add-input');
        if (fresh) fresh.focus();
      } else { input.disabled = false; }
    } catch (e) { input.disabled = false; }
  }

  // add on Enter
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter") return;
    const input = ev.target.closest && ev.target.closest(".subtask-add-input");
    if (!input) return;
    ev.preventDefault();
    addSubtask(input);
  });
  // complete / reopen a subtask
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest && ev.target.closest(".subtask-toggle");
    if (!btn) return;
    ev.preventDefault();
    const wrap = btn.closest(".task-subtasks");
    const fd = new FormData();
    fd.append("csrf_token", csrf());
    try {
      const r = await fetch(btn.dataset.action, {
        method: "POST", body: fd, credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (r.ok) refresh(wrap);
    } catch (e) { /* ignore */ }
  });
})();
