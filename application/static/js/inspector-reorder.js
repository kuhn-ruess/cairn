/* Drag the inspector's sections into the order you want, where they are.
 *
 * The same setting the layout page writes (User.inspector_layout), from the
 * place the sections actually sit — arranging them shouldn't mean going
 * somewhere else and imagining the result.
 *
 * The grip is added here rather than in the template: it does nothing without
 * this script, so it shouldn't exist without it. Sections are marked with
 * data-section-key; the pinned title has none and therefore can't move, and
 * onMove refuses drops outside the run of sections so nothing can land above
 * the title or below the footer.
 */
(function () {
  const ENDPOINT = "/settings/inspector-layout/order";
  const instances = new WeakMap();

  function csrfToken(root) {
    const el = (root || document).querySelector('input[name="csrf_token"]')
      || document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  function addGrip(section) {
    if (section.querySelector(":scope > .inspector-grip")) return;
    const grip = document.createElement("span");
    grip.className = "inspector-grip";
    grip.title = "Drag to reorder this section";
    grip.innerHTML = '<i class="bi bi-grip-vertical"></i>';
    section.appendChild(grip);
  }

  async function persist(form) {
    const fd = new FormData();
    fd.append("csrf_token", csrfToken(form));
    form.querySelectorAll("[data-section-key]").forEach((el) => {
      fd.append("order[]", el.dataset.sectionKey);
    });
    try {
      await fetch(ENDPOINT, {
        method: "POST", body: fd, credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
    } catch (e) {
      /* The sections are already where the user dropped them; the next load
         will show the old order, which is a better failure than a jump. */
    }
  }

  function init() {
    if (typeof Sortable === "undefined") return;
    document.querySelectorAll("form.inspector-form").forEach((form) => {
      const sections = form.querySelectorAll("[data-section-key]");
      if (sections.length < 2) return;
      if (instances.has(form)) return;
      sections.forEach(addGrip);
      instances.set(form, new Sortable(form, {
        draggable: "[data-section-key]",
        handle: ".inspector-grip",
        animation: 150,
        ghostClass: "inspector-section-ghost",
        // Everything else in the form — the title, the footer — is not a
        // section, and a section may not be dropped across it.
        onMove: (evt) => !!(evt.related && evt.related.dataset
                            && evt.related.dataset.sectionKey),
        onEnd: () => persist(form),
      }));
    });
  }

  document.addEventListener("DOMContentLoaded", init);
  // The inspector is fetched and swapped in; its sections are then new nodes.
  document.addEventListener("inspector:loaded", init);
})();
