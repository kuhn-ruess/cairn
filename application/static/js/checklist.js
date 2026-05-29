/* Task checklist — add / toggle / delete via fetch, swapping the
 * re-rendered partial in place. Document-level delegation so it works
 * everywhere the checklist renders: the inline inspector on the task
 * detail page AND the off-canvas inspector (whose body is injected
 * after page load). No navigation happens, so the add input keeps focus
 * for rapid entry and the browser Back button stays clean.
 */
(function () {
  function containerFor(el) {
    // The swap target is the nearest checklist wrapper (id="checklist").
    return el.closest("#checklist, .checklist-block");
  }

  async function submitForm(form, { refocus }) {
    const container = containerFor(form);
    if (!container) return;
    const resp = await fetch(form.action, {
      method: "POST",
      headers: { "X-Partial": "1" },
      body: new FormData(form),
    });
    if (!resp.ok) throw new Error(resp.status);
    container.innerHTML = await resp.text();
    if (refocus) {
      const input = container.querySelector(".checklist-add-input");
      if (input) input.focus();
    }
  }

  // Add + delete: intercept the form submit.
  document.addEventListener("submit", (ev) => {
    const form = ev.target.closest(
      "form[data-checklist-add], form[data-checklist-delete]"
    );
    if (!form) return;
    ev.preventDefault();
    submitForm(form, { refocus: form.hasAttribute("data-checklist-add") })
      .catch(() => form.submit());
  });

  // Toggle: the checkbox-style button submits its own little form.
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".checklist-toggle");
    if (!btn) return;
    const form = btn.closest("form[data-checklist-toggle]");
    if (!form) return;
    ev.preventDefault();
    submitForm(form, { refocus: false }).catch(() => form.submit());
  });
})();
