/* Task time tracking — log / delete entries via fetch, swapping the
 * re-rendered partial in place. Same shape as checklist.js: document-level
 * delegation (works in the inline detail inspector AND the off-canvas one),
 * plain buttons carrying their endpoint in data-action, page CSRF token.
 *
 * The duration input lives inside the inspector's single <form>, so Enter
 * must be intercepted or it would submit/save the whole task.
 */
(function () {
  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  function blockFor(el) {
    return el.closest("#time, .time-block");
  }

  // Default the date picker to today whenever the partial appears — covers
  // the off-canvas inspector whose body is injected after page load.
  function ensureDefaultDate(scope) {
    (scope || document).querySelectorAll(".time-add-date").forEach((inp) => {
      if (!inp.value) inp.value = new Date().toISOString().slice(0, 10);
    });
  }
  ensureDefaultDate();
  new MutationObserver(() => ensureDefaultDate())
    .observe(document.body, { childList: true, subtree: true });

  async function logTime(add, block) {
    const dur = add.querySelector(".time-add-duration");
    if (!dur || !dur.value.trim()) { if (dur) dur.focus(); return; }
    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    fd.append("duration", dur.value.trim());
    const date = add.querySelector(".time-add-date");
    const note = add.querySelector(".time-add-note");
    if (date && date.value) fd.append("spent_on", date.value);
    if (note && note.value) fd.append("note", note.value.trim());
    const resp = await fetch(add.dataset.action, {
      method: "POST", headers: { "X-Partial": "1" }, body: fd,
    });
    if (!resp.ok) throw new Error(resp.status);
    block.innerHTML = await resp.text();
    ensureDefaultDate(block);
    const next = block.querySelector(".time-add-duration");
    if (next) next.focus();
  }

  async function del(action, block) {
    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    const resp = await fetch(action, {
      method: "POST", headers: { "X-Partial": "1" }, body: fd,
    });
    if (!resp.ok) throw new Error(resp.status);
    block.innerHTML = await resp.text();
    ensureDefaultDate(block);
  }

  document.addEventListener("click", (ev) => {
    const addBtn = ev.target.closest(".time-add-btn");
    if (addBtn) {
      ev.preventDefault();
      const add = addBtn.closest(".time-add");
      logTime(add, blockFor(addBtn)).catch(() => {});
      return;
    }
    const rm = ev.target.closest(".time-delete");
    if (rm) {
      ev.preventDefault();
      del(rm.dataset.action, blockFor(rm)).catch(() => {});
    }
  });

  // Enter in any of the add inputs logs the entry instead of submitting the
  // surrounding inspector form.
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter") return;
    const input = ev.target.closest(
      ".time-add-duration, .time-add-date, .time-add-note"
    );
    if (!input) return;
    ev.preventDefault();
    const add = input.closest(".time-add");
    logTime(add, blockFor(input)).catch(() => {});
  });
})();
