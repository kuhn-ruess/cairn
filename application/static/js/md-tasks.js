/* Tickable "- [ ]" boxes in a rendered Markdown body.

   Any body that carries ``data-md-toggle-url`` gets its checkboxes wired to
   that endpoint: the box's ``data-md-task`` is its ordinal in the source, and
   the server flips that line (services/md_tasks.py). Used by the note field on
   tasks and projects; the standalone note page has its own handler because it
   must also keep the open editor's baseline in step.

   Delegated on document: <main> is swapped wholesale by bktodo:refresh-needed,
   and the task note body is re-rendered after every inline edit. */
(function () {
  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  document.addEventListener("change", async (ev) => {
    const box = ev.target.closest("input[data-md-task]");
    if (!box) return;
    const body = box.closest("[data-md-toggle-url]");
    if (!body) return;

    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    fd.append("index", box.dataset.mdTask);
    box.disabled = true;
    try {
      const resp = await fetch(body.dataset.mdToggleUrl, {
        method: "POST", body: fd, credentials: "same-origin",
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.error || "toggle failed");
      box.checked = !!data.checked;
    } catch (err) {
      // Put the box back where it was — a checkbox that silently forgets is
      // the one bug this must not have.
      box.checked = !box.checked;
      window.alert("Couldn't update the checkbox.");
    } finally {
      box.disabled = false;
    }
  });
})();
