/* A row of chips at the top of the task inspector, one per section it shows,
 * so a field is one click away instead of a scroll through a dozen blocks.
 * A click opens the section if it was folded and brings it into view.
 *
 * Built from the sections the inspector actually rendered (the user's own
 * order, hidden ones absent, empty ones skipped), so it can never point at
 * something that isn't there. Rebuilt on `inspector:loaded`, the app's
 * "the inspector's markup is new" signal, so it follows every reload of the
 * panel, the offcanvas and the inline one alike.
 */
(function () {
  const CHIPS = {
    status_project: ["bi-folder2", "Project & tags"],
    dates: ["bi-calendar-event", "Dates"],
    priority_estimate: ["bi-flag", "Priority"],
    assigned_to: ["bi-person", "Assignee"],
    repeat: ["bi-arrow-repeat", "Repeat"],
    review: ["bi-eye", "Follow-up"],
    location: ["bi-geo-alt", "Location"],
    checklist: ["bi-check2-square", "Checklist"],
    time: ["bi-stopwatch", "Time"],
    related: ["bi-link-45deg", "Related"],
    note: ["bi-journal-text", "Note"],
    subtasks: ["bi-diagram-3", "Subtasks"],
    comments: ["bi-chat-left-text", "Comments"],
  };

  function build(form) {
    const old = form.querySelector(":scope > .insp-jump");
    if (old) old.remove();
    const sections = Array.from(form.querySelectorAll(":scope > [data-section-key]"));
    if (sections.length < 3) return;
    const nav = document.createElement("nav");
    nav.className = "insp-jump";
    nav.setAttribute("aria-label", "Jump to an inspector section");
    sections.forEach((sec) => {
      const key = sec.dataset.sectionKey;
      const def = CHIPS[key];
      const b = document.createElement("button");
      b.type = "button";
      b.className = "insp-jump-chip";
      b.dataset.jumpTo = key;
      const label = def ? def[1] : (sec.querySelector("h2, .form-label")?.textContent || key).trim();
      b.innerHTML = '<i class="bi ' + (def ? def[0] : "bi-dot") + '"></i><span></span>';
      b.querySelector("span").textContent = label;
      b.title = "Go to " + label;
      nav.appendChild(b);
    });
    const anchor = form.querySelector(':scope > [data-section="title"]');
    if (anchor) anchor.after(nav); else form.prepend(nav);
  }

  function buildAll() {
    document.querySelectorAll("form[data-inspector-form]").forEach(build);
  }

  document.addEventListener("click", (ev) => {
    const chip = ev.target.closest("[data-jump-to]");
    if (!chip) return;
    const form = chip.closest("form[data-inspector-form]");
    const sec = form && form.querySelector(
      ':scope > [data-section-key="' + chip.dataset.jumpTo + '"]');
    if (!sec) return;
    const body = sec.querySelector(".section-collapsible");
    if (body && body.classList.contains("d-none")) {
      const toggle = sec.querySelector("[data-collapse-key]");
      if (toggle) toggle.click();
    }
    // Land below the sticky chip row, not under it.
    const nav = form.querySelector(":scope > .insp-jump");
    sec.style.scrollMarginTop = ((nav ? nav.offsetHeight : 0) + 8) + "px";
    sec.scrollIntoView({ behavior: "smooth", block: "start" });
    sec.classList.remove("insp-jump-hit");
    void sec.offsetWidth;
    sec.classList.add("insp-jump-hit");
    const field = sec.querySelector("input:not([type=hidden]), select, textarea");
    if (field && field.offsetParent) setTimeout(() => field.focus({ preventScroll: true }), 300);
  });

  document.addEventListener("inspector:loaded", buildAll);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", buildAll);
  else buildAll();
})();
