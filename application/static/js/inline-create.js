/* Inline create + inline naming — skip the inspector for quick adds.
 *
 * Two entry points:
 *   1. Project detail page: press "s" to add a section. A naming input
 *      appears at the bottom of the project; type a name + Enter to create.
 *   2. Projects page "+" menu: [data-inline-create="project|folder"] drops
 *      a naming row into the table instead of opening the inspector.
 *
 * Both reuse the existing JSON endpoints (projects.add_section /
 * projects.quick_create) and repaint in place.
 */
(function () {
  function csrfToken() {
    const el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  function flashError(msg) {
    let bar = document.getElementById("move-error-flash");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "move-error-flash";
      bar.className =
        "alert alert-danger position-fixed top-0 start-50 translate-middle-x mt-3 shadow";
      bar.style.zIndex = 2000;
      document.body.appendChild(bar);
    }
    bar.textContent = msg;
    setTimeout(() => { if (bar) bar.remove(); }, 3000);
  }

  // Repaint <main> and the project sidebar in place. The sidebar's
  // refreshList re-arms its task-drop targets on this event (see
  // bktodoInitSidebarDrops), so a freshly created project becomes a
  // drop target immediately — no full page reload needed.
  function refreshSidebarAndMain() {
    document.dispatchEvent(new Event("bktodo:refresh-needed"));
  }

  // The project-detail view has sub-project drop rows that only the
  // wrapped bktodoRefreshMain re-arms, so use that here.
  function refreshDetail() {
    if (typeof window.bktodoRefreshMain === "function") {
      window.bktodoRefreshMain();
    } else {
      window.location.reload();
    }
  }

  async function postForm(url, data) {
    const fd = new FormData();
    fd.append("csrf_token", csrfToken());
    Object.entries(data).forEach(([k, v]) => fd.append(k, v));
    return fetch(url, {
      method: "POST",
      body: fd,
      headers: { "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin",
    });
  }

  // Wire an editing input: Enter commits, Escape cancels, blurring an empty
  // box cancels. `commit` is async and must flip nothing on its own — we
  // guard re-entry here so a slow request can't fire twice.
  function wireEditor(input, commit, cancel) {
    let busy = false;
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        if (busy) return;
        if (!input.value.trim()) { cancel(); return; }
        busy = true;
        commit().then((ok) => { if (!ok) busy = false; });
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        cancel();
      }
    });
    input.addEventListener("blur", () => {
      if (!busy && !input.value.trim()) cancel();
    });
  }

  // ---- 1. Section quick-create on the project detail page --------------
  function openSectionInput(projectId) {
    const main = document.querySelector("main");
    if (!main) return;
    let block = document.getElementById("inline-section-create");
    if (block) { block.querySelector("input").focus(); return; }
    block = document.createElement("div");
    block.className = "section-block mt-4";
    block.id = "inline-section-create";
    block.innerHTML =
      '<div class="section-header d-flex align-items-center">' +
      '<i class="bi bi-bookmark text-info me-2"></i>' +
      '<input type="text" class="form-control form-control-sm" ' +
      'placeholder="Section name — Enter to add, Esc to cancel" ' +
      'style="max-width: 24rem;" autocomplete="off">' +
      "</div>";
    main.appendChild(block);
    const input = block.querySelector("input");
    input.focus();
    input.scrollIntoView({ block: "center", behavior: "smooth" });

    wireEditor(
      input,
      async () => {
        try {
          const resp = await postForm(
            `/projects/${projectId}/sections/new`,
            { name: input.value.trim() }
          );
          if (!resp.ok) {
            flashError(`Add section failed (${resp.status}).`);
            return false;
          }
          refreshDetail();
          return true;
        } catch (err) {
          flashError("Add section failed (network).");
          return false;
        }
      },
      () => block.remove()
    );
  }

  // Capture phase so we win over bulk.js's bubble-phase "s" = someday.
  // Re-query the marker each time so it survives in-place refreshes.
  document.addEventListener(
    "keydown",
    (ev) => {
      if (ev.key.toLowerCase() !== "s") return;
      if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
      const t = ev.target;
      const tag = (t.tagName || "").toUpperCase();
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (t.isContentEditable) return;
      const detail = document.getElementById("project-detail");
      if (!detail) return;
      ev.preventDefault();
      ev.stopImmediatePropagation();
      openSectionInput(detail.dataset.projectId);
    },
    true
  );

  // ---- 2. Project / folder quick-create on the Projects page -----------
  function openProjectRow(kind) {
    const tbody = document.querySelector("#project-list-table tbody");
    if (!tbody) { window.location.href = "/projects/new"; return; }
    let row = document.getElementById("inline-project-create");
    if (row) { row.querySelector("input").focus(); return; }
    row = document.createElement("tr");
    row.id = "inline-project-create";
    const icon = kind === "folder"
      ? '<i class="bi bi-folder2-open text-warning me-1"></i>'
      : '<i class="bi bi-collection text-muted me-1"></i>';
    row.innerHTML =
      "<td></td>" +
      '<td colspan="6">' + icon +
      '<input type="text" class="form-control form-control-sm d-inline-block" ' +
      'placeholder="' + (kind === "folder" ? "Folder" : "Project") +
      ' name — Enter to create, Esc to cancel" ' +
      'style="max-width: 24rem;" autocomplete="off"></td>';
    // Drop it just under the Inbox row (the first row).
    const first = tbody.querySelector("tr");
    if (first && first.nextSibling) tbody.insertBefore(row, first.nextSibling);
    else tbody.appendChild(row);
    const input = row.querySelector("input");
    input.focus();

    wireEditor(
      input,
      async () => {
        try {
          const resp = await postForm("/projects/quick-create", {
            name: input.value.trim(),
            kind,
          });
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) {
            flashError(data.error || `Create failed (${resp.status}).`);
            return false;
          }
          refreshSidebarAndMain();
          return true;
        } catch (err) {
          flashError("Create failed (network).");
          return false;
        }
      },
      () => row.remove()
    );
  }

  // Delegated so it survives table re-renders.
  document.addEventListener("click", (ev) => {
    const el = ev.target.closest("[data-inline-create]");
    if (!el) return;
    ev.preventDefault();
    openProjectRow(el.dataset.inlineCreate);
  });

  // ---- 3. Project / folder quick-create in the project sidebar ---------
  function openSidebarRow(kind) {
    const list = document.querySelector("#project-sidebar .ps-list");
    if (!list) { window.location.href = "/projects/new"; return; }
    let li = document.getElementById("ps-inline-create");
    if (li) { li.querySelector("input").focus(); return; }
    li = document.createElement("li");
    li.id = "ps-inline-create";
    li.className = "ps-row px-2 py-1 d-flex align-items-center gap-1";
    const icon = kind === "folder"
      ? '<i class="bi bi-folder2-open text-warning"></i>'
      : '<i class="bi bi-collection text-info"></i>';
    li.innerHTML = icon +
      '<input type="text" class="form-control form-control-sm" ' +
      'autocomplete="off" placeholder="' +
      (kind === "folder" ? "Folder" : "Project") + ' name — Enter / Esc">';
    // Drop it just below the Inbox row (the first list item).
    const first = list.querySelector("li");
    if (first && first.nextSibling) list.insertBefore(li, first.nextSibling);
    else list.appendChild(li);
    const input = li.querySelector("input");
    input.focus();

    wireEditor(
      input,
      async () => {
        try {
          const resp = await postForm("/projects/quick-create", {
            name: input.value.trim(),
            kind,
          });
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) {
            flashError(data.error || `Create failed (${resp.status}).`);
            return false;
          }
          refreshSidebarAndMain();
          return true;
        } catch (err) {
          flashError("Create failed (network).");
          return false;
        }
      },
      () => li.remove()
    );
  }

  document.addEventListener("click", (ev) => {
    const el = ev.target.closest("[data-inline-create-sidebar]");
    if (!el) return;
    ev.preventDefault();
    openSidebarRow(el.dataset.inlineCreateSidebar);
  });
})();
