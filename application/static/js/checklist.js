/* Task checklist — add / toggle / delete via fetch, swapping the
 * re-rendered partial in place. Document-level delegation so it works
 * everywhere the checklist renders: the inline inspector on the task
 * detail page AND the off-canvas inspector (whose body is injected
 * after page load).
 *
 * Actions are plain buttons (not nested <form>s — the inspector wraps
 * everything in one form and HTML forbids nesting). Each carries its
 * endpoint in data-action; we POST with the page CSRF token.
 */
(function () {
  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  function containerFor(el) {
    return el.closest("#checklist, .checklist-block");
  }

  async function run(action, container, { refocus } = {}) {
    if (!action || !container) return;
    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    const addInput = container.querySelector(".checklist-add-input");
    if (addInput && action.endsWith("/checklist")) {
      const text = addInput.value.trim();
      if (!text) { addInput.focus(); return; }
      fd.append("text", text);
    }
    const resp = await fetch(action, {
      method: "POST",
      headers: { "X-Partial": "1" },
      body: fd,
    });
    if (!resp.ok) throw new Error(resp.status);
    container.innerHTML = await resp.text();
    if (refocus) {
      const input = container.querySelector(".checklist-add-input");
      if (input) input.focus();
    }
  }

  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest(
      ".checklist-toggle, .checklist-delete, .checklist-add-btn"
    );
    if (!btn) return;
    ev.preventDefault();
    const container = containerFor(btn);
    let action = btn.dataset.action;
    let refocus = false;
    if (btn.classList.contains("checklist-add-btn")) {
      const add = btn.closest(".checklist-add");
      action = add && add.dataset.action;
      refocus = true;
    }
    run(action, container, { refocus }).catch(() => {});
  });

  // Enter in the add input submits without reloading.
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter") return;
    const input = ev.target.closest(".checklist-add-input");
    if (!input) return;
    ev.preventDefault();
    const add = input.closest(".checklist-add");
    run(add && add.dataset.action, containerFor(input), { refocus: true })
      .catch(() => {});
  });
})();
