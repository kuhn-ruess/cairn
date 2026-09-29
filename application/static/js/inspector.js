/* Right-side inspector panel (OmniFocus-style).
 *
 * Any element with [data-inspector-url] opens the offcanvas, fetches that URL
 * with ?inspector=1, and drops the returned HTML fragment into the panel. The
 * form inside is intercepted on submit: we POST via fetch, then reload the
 * page so the list reflects the change.
 */
(function () {
  const panel = document.getElementById("inspector-panel");
  if (!panel) return;
  const body = panel.querySelector(".offcanvas-body");
  const titleEl = panel.querySelector(".offcanvas-title");
  const bsOffcanvas = bootstrap.Offcanvas.getOrCreateInstance(panel);

  // ---- Pin: keep the inspector open across clicks / Esc / page loads ----
  const PIN_KEY = "cairn:inspector-pinned";
  const pinBtn = document.getElementById("inspector-pin");
  // The task-detail page renders its OWN inline inspector (#inline-inspector).
  // Don't ALSO open the pinned offcanvas there — it'd sit empty beside the
  // inline one. The pin state still persists for list pages.
  const hasInline = !!document.getElementById("inline-inspector");
  // Only auto-open the pinned inspector on pages that actually host
  // inspectable items (task/project lists). Settings, admin, profile, …
  // have nothing to inspect — a pinned-open empty panel there is just noise.
  const isInspectorHost = () => !!document.querySelector("[data-inspector-url]");
  function isPinned() {
    try { return localStorage.getItem(PIN_KEY) === "1"; }
    catch (e) { return false; }
  }
  function reflectPin() {
    const on = isPinned();
    document.body.classList.toggle("inspector-pinned", on && !hasInline);
    if (pinBtn) {
      pinBtn.setAttribute("aria-pressed", on ? "true" : "false");
      pinBtn.classList.toggle("is-pinned", on);
      const icon = pinBtn.querySelector("i");
      if (icon) icon.className = on ? "bi bi-pin-angle-fill" : "bi bi-pin-angle";
      pinBtn.title = on ? "Unpin — let the inspector close" : "Pin — keep the inspector open";
    }
  }
  function showPinnedPlaceholder() {
    if (body.innerHTML.trim()) return;
    body.innerHTML =
      '<div class="text-muted small text-center py-5 px-3">' +
      "Select a task or project to inspect it here.</div>";
  }
  if (pinBtn) {
    pinBtn.addEventListener("click", () => {
      const next = !isPinned();
      try { localStorage.setItem(PIN_KEY, next ? "1" : "0"); } catch (e) {}
      reflectPin();
      if (next && !hasInline && isInspectorHost()) { bsOffcanvas.show(); showPinnedPlaceholder(); }
    });
  }
  reflectPin();
  // Pinned → open the column on load so it's a permanent fixture (but not on
  // the detail page, which already has its own inline inspector).
  if (isPinned() && !hasInline && isInspectorHost()) { bsOffcanvas.show(); showPinnedPlaceholder(); }

  // When the user closes the inspector after at least one successful save,
  // refresh the list behind it so the change is visible. We *don't* reload
  // immediately on save — that would close the panel.
  let needsReloadOnClose = false;
  // True once the user has typed into an as-yet-unsaved NEW task/project form.
  // Guards every path that would throw the draft away (outside-click, Escape,
  // the ×/Cancel, opening another item, navigating off). Reset on (re)render.
  let unsavedNewTask = false;
  // Returns false when the user wants to keep an unsaved draft. Clearing the
  // flag on "discard" keeps the follow-up hide/open from asking twice.
  function confirmDiscardDraft() {
    if (!unsavedNewTask) return true;
    if (!confirm("Discard this unsaved new item?")) return false;
    unsavedNewTask = false;
    return true;
  }
  panel.addEventListener("show.bs.offcanvas", () => {
    document.body.classList.add("inspector-docked");
  });
  // First open paints the panel shifted left until a second open/scroll. Once
  // the open animation has finished and the panel is at full width, kick its
  // compositing layer so WebKit re-paints it in the right place. See forceReflow.
  panel.addEventListener("shown.bs.offcanvas", () => {
    forceReflow();
  });
  // Single choke point for every close path (×, Cancel, Escape, programmatic
  // hide): never drop an unsaved draft without asking.
  panel.addEventListener("hide.bs.offcanvas", (ev) => {
    if (!confirmDiscardDraft()) ev.preventDefault();
  });
  panel.addEventListener("hidden.bs.offcanvas", () => {
    document.body.classList.remove("inspector-docked");
    // Closing the panel (X / Esc / backdrop) while pinned means "I'm done with
    // it" — drop the sticky so it doesn't reopen on the next page.
    if (isPinned()) {
      try { localStorage.setItem(PIN_KEY, "0"); } catch (e) {}
      reflectPin();
    }
    if (needsReloadOnClose) {
      needsReloadOnClose = false;
      window.location.reload();
    }
  });

  // The inspector is docked (data-bs-backdrop="false"), so Bootstrap's
  // backdrop-click and focus-based Escape don't fire. Wire both ourselves
  // so the panel never gets "stuck" open swallowing clicks behind it.
  function isOpen() {
    return panel.classList.contains("show");
  }
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && isOpen() && !isPinned()) bsOffcanvas.hide();
  });
  document.addEventListener("click", (ev) => {
    if (!isOpen() || isPinned()) return;
    // Clicks inside the panel, or on something that opens the inspector
    // (a trigger reloads the body — handled elsewhere), must not close it.
    if (panel.contains(ev.target)) return;
    if (ev.target.closest("[data-inspector-url]")) return;
    // A menu opened from inside the panel but rendered on <body> (so the
    // panel's scroll box can't clip it) is still part of the inspector.
    if (ev.target.closest("[data-keeps-inspector-open]")) return;
    // Don't let an accidental outside-click discard an unsaved new task — keep
    // the panel open so the work survives. Escape / the × still close it.
    if (unsavedNewTask) return;
    bsOffcanvas.hide();
  }, true);

  // On first open the panel paints shifted left into the page margin (a WebKit
  // bug: the sticky flex child's promoted compositing layer — transform:
  // translateZ(0) in app.css — is positioned wrong until a later repaint, e.g.
  // a second open or a scroll). It's a paint/layer bug, not a layout one, so
  // reading offsetWidth alone doesn't fix it. Briefly drop the promoted layer
  // (transform:none, via inline !important to beat the stylesheet), flush, then
  // restore it on the next frame so WebKit re-establishes the layer at the
  // correct position. translateZ(0) and none have identical visual offset
  // (zero), so the toggle is invisible.
  function forceReflow() {
    panel.style.setProperty("transform", "none", "important");
    void panel.offsetWidth;
    requestAnimationFrame(() => {
      panel.style.removeProperty("transform"); // back to stylesheet translateZ(0)
      void panel.offsetWidth;
    });
  }

  function withInspectorParam(url) {
    const u = new URL(url, window.location.origin);
    u.searchParams.set("inspector", "1");
    return u.toString();
  }

  async function openInspector(url, label) {
    // A fresh open starts a new edit session — clear the dirty flag so we
    // don't reload prematurely when this open hasn't seen a save yet.
    needsReloadOnClose = false;
    body.innerHTML =
      '<div class="d-flex justify-content-center py-5">' +
      '<div class="spinner-border text-warning" role="status"></div></div>';
    titleEl.textContent = label || "Inspector";
    bsOffcanvas.show();

    try {
      const resp = await fetch(withInspectorParam(url), {
        headers: { "X-Inspector-Request": "1" },
      });
      if (!resp.ok) {
        // No raw 403/404 in the panel — show a plain, friendly explanation
        // (e.g. a teammate opening something not shared with them).
        body.innerHTML = inspectorErrorHtml(resp.status);
        forceReflow();
        return;
      }
      const html = await resp.text();
      body.innerHTML = html;
      wireForm();
      // The real (taller, full-width) form just replaced the spinner — relayout
      // so the sticky panel doesn't keep the spinner-era geometry.
      forceReflow();
      document.dispatchEvent(new Event("inspector:loaded"));
    } catch (err) {
      body.innerHTML = inspectorErrorHtml(0, err.message);
      forceReflow();
    }
  }

  // Re-render the offcanvas with fresh server markup. Exposed for the handful
  // of controls that change the item behind the panel's back (the Zammad
  // refresh below): a bktodo:refresh-needed swap only reaches an inspector that
  // sits INSIDE <main>, which the offcanvas does not.
  window.bktodoOpenInspector = openInspector;

  // Friendly inspector message for a non-OK fetch. Keeps raw HTTP status pages
  // out of the panel — an unauthorized teammate sees an explanation, not a 404.
  function inspectorErrorHtml(status, detail) {
    let icon = "bi-exclamation-triangle";
    let msg;
    if (status === 403) {
      icon = "bi-lock";
      msg = "You don't have permission to view this item. Ask its owner or a " +
            "team admin to share it with you.";
    } else if (status === 404) {
      msg = "This item no longer exists — it may have been deleted.";
    } else {
      msg = "Couldn't load this item" +
            (detail ? " (" + detail + ")" : (status ? " (error " + status + ")" : "")) + ".";
    }
    return '<div class="text-muted text-center py-5 px-3">' +
      '<i class="bi ' + icon + ' fs-3 d-block mb-2"></i>' +
      escapeHtml(msg) + "</div>";
  }

  function wireForm() {
    const form = body.querySelector("form[data-inspector-form]");
    // Make the offcanvas header title a link to the full detail page.
    const detailUrl = form && form.getAttribute("data-detail-url");
    if (detailUrl && titleEl) {
      const text = titleEl.textContent;
      titleEl.textContent = "";
      const a = document.createElement("a");
      a.href = detailUrl;
      a.className = "inspector-title-link";
      a.textContent = text;
      a.title = "Open the full detail page";
      titleEl.appendChild(a);
    }
    // Track unsaved input on a NEW task form so an accidental outside-click
    // can't discard it. A saved form re-renders as an /edit form, so this
    // resets to false automatically. (The comment box is its own action — don't
    // let typing a comment count as unsaved task data.)
    unsavedNewTask = false;
    if (form && /\/(tasks|projects)\/new(\?|$)/.test(form.getAttribute("action") || "")) {
      form.addEventListener("input", (ev) => {
        if (ev.target && ev.target.name === "comment_body") return;
        unsavedNewTask = true;
      });
    }
    if (form) {
      form.addEventListener("submit", onFormSubmit);
      // Enter saves: Cmd/Ctrl+Enter from anywhere (incl. textareas), and plain
      // Enter from single-line fields. Textareas keep Enter = newline; buttons,
      // links and selects keep their native Enter behaviour.
      form.addEventListener("keydown", (ev) => {
        if (ev.key !== "Enter" || ev.isComposing) return;
        const t = ev.target;
        // The comment box lives inside the task form but is its own action:
        // Cmd/Ctrl+Enter posts the comment (never a task save), plain Enter
        // stays a newline. This keeps the two saves from clobbering each other.
        if (t && t.name === "comment_body") {
          if (ev.metaKey || ev.ctrlKey) {
            ev.preventDefault();
            const wrap = t.closest("[data-inspector-comment-form]");
            const addBtn = wrap && wrap.querySelector(".comment-add-btn");
            if (addBtn) addBtn.click();
          }
          return;
        }
        // Enter in the comment's time field posts the comment too — it is
        // part of that action, not a task field.
        if (t && t.name === "log_duration") {
          ev.preventDefault();
          const wrap = t.closest("[data-inspector-comment-form]");
          const addBtn = wrap && wrap.querySelector(".comment-add-btn");
          if (addBtn) addBtn.click();
          return;
        }
        // Enter in the time section books the entry (time-entries.js) — it is
        // not a task save. Saving too re-rendered the whole panel, and when
        // that render ran before the booking landed it replaced the time
        // section with one that didn't have the new entry: the time looked
        // unsaved. The entry editor and the picker's search are the same.
        if (t && t.closest && t.closest(".time-add, .tt-edit, .tt-pick, .time-block")) {
          // A focused button keeps its native Enter = click.
          if (t.tagName !== "BUTTON" && t.tagName !== "A") ev.preventDefault();
          return;
        }
        const tag = t && t.tagName;
        if (tag === "BUTTON" || tag === "A" || tag === "SELECT" ||
            (t && t.isContentEditable)) return;
        const isTextarea = tag === "TEXTAREA";
        if (!(ev.metaKey || ev.ctrlKey) && isTextarea) return;
        ev.preventDefault();
        if (typeof form.requestSubmit === "function") form.requestSubmit();
        else form.dispatchEvent(new Event("submit", { cancelable: true }));
      });
    }

    body.querySelectorAll("form[data-inspector-side]").forEach((sideForm) => {
      sideForm.addEventListener("submit", onSideSubmit);
    });

    // Project inspector: keep the color picker and the hidden form
    // field in sync. The inline <script> in _inspector.html doesn't
    // run (innerHTML doesn't execute scripts), so do it here.
    const picker = body.querySelector("#proj-color-picker");
    const hex = body.querySelector("#proj-color-hex");
    if (picker && hex) {
      // Only mirror a *saved* colour into the picker. If none is set, leave
      // the hidden field empty — copying the picker's default here would
      // silently stamp every untouched project/folder with a colour on save.
      if (hex.value) picker.value = hex.value;
      picker.addEventListener("input", () => { hex.value = picker.value; });
    }

    // When the user flips Project → Folder → Section the offcanvas
    // header still says whatever the original opener wrote (often
    // "New project"). Sync it with the current kind so the title
    // matches what's actually being created/edited.
    const kindSel = body.querySelector('select[name="kind"]');
    if (kindSel) {
      const isNew = !body.querySelector(".inspector-saved-banner");
      const updateTitle = () => {
        const cap = kindSel.value.charAt(0).toUpperCase() + kindSel.value.slice(1);
        // For edits, prefix with the current name from the title input
        // so it reads "Cap · Name"; for "new" it reads "New <kind>".
        const nameInput = body.querySelector("#proj-name-input");
        if (isNew) {
          titleEl.textContent = `New ${kindSel.value}`;
        } else if (nameInput) {
          titleEl.textContent = `${cap} · ${nameInput.value || ""}`.trim();
        }
      };
      kindSel.addEventListener("change", updateTitle);
      updateTitle();
    }

    // Add-comment (inspector only, no page reload). It's a button, not a
    // nested form — the inspector is one big form and HTML forbids nesting.
    const commentBtn = body.querySelector("[data-inspector-comment-form] .comment-add-btn");
    if (commentBtn) commentBtn.addEventListener("click", onCommentAdd);

    // Expand/collapse the comments panel.
    const expandBtn = body.querySelector(".comments-expand-toggle");
    if (expandBtn) {
      expandBtn.addEventListener("click", () => {
        const block = body.querySelector(".comments-block");
        if (block) block.classList.toggle("is-expanded");
      });
    }

    // Apply the remembered comment sort order to the freshly-loaded panel.
    applyCommentSort(body);

    // ``autofocus`` is only honoured on initial page load; after we swap
    // the inspector body (e.g. after "Add another") we want the title to
    // be ready to type into immediately.
    const auto = body.querySelector("[autofocus]");
    if (auto) setTimeout(() => auto.focus(), 30);

    // Live smart-parse: as the user types tokens into the title, mirror
    // them into the structured form fields below.
    setupInspectorSmartParse(body);
  }

  // ============= Inspector live smart-parse =============
  const WEEKDAYS = {
    monday: 1, mon: 1, tuesday: 2, tue: 2, tues: 2,
    wednesday: 3, wed: 3, thursday: 4, thu: 4, thur: 4, thurs: 4,
    friday: 5, fri: 5, saturday: 6, sat: 6, sunday: 0, sun: 0,
    montag: 1, dienstag: 2, mittwoch: 3, donnerstag: 4,
    freitag: 5, samstag: 6, sonntag: 0,
  };

  function setupInspectorSmartParse(scope) {
    const titleInput = scope.querySelector('form[data-inspector-form] input[name="title"]');
    if (!titleInput) return;
    let timer = null;
    titleInput.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => applyInspectorSmartParse(titleInput), 80);
    });
  }

  function applyInspectorSmartParse(titleInput) {
    const form = titleInput.closest("form");
    if (!form) return;
    const text = titleInput.value;
    if (!text) return;

    // Priority
    const pm = text.match(/(?<!\S)p([1-4])(?!\S)/i);
    if (pm) setSelect(form, 'select[name="priority"]', pm[1]);

    // Flag
    if (/(?<!\S)!(?!\S)/.test(text)) {
      const cb = form.querySelector('input[name="flagged"]');
      if (cb) cb.checked = true;
    }

    // Tags (multi-select, additive)
    const tagsSel = form.querySelector('select[name="tags"]');
    if (tagsSel) {
      const matches = [...text.matchAll(/(?<!\S)@([\w\-äöüÄÖÜß]+)\b/g)];
      if (matches.length) {
        const wanted = new Set(matches.map((m) => m[1].toLowerCase()));
        for (const opt of tagsSel.options) {
          const label = opt.textContent.replace(/^@/, "").trim().toLowerCase();
          if (wanted.has(label)) opt.selected = true;
        }
      }
    }

    // Project — first match wins. Match by label, ignoring the emoji prefix.
    const projSel = form.querySelector('select[name="project"]');
    if (projSel) {
      const mp = text.match(/(?<!\S)#([\w\-äöüÄÖÜß]+)\b/);
      if (mp) {
        const wanted = mp[1].toLowerCase();
        for (const opt of projSel.options) {
          const label = opt.textContent.trim().replace(/^[^\w]+/, "").toLowerCase();
          if (label === wanted) {
            projSel.value = opt.value;
            break;
          }
        }
      }
    }

    // Due date
    const dueInput = form.querySelector('input[name="due_date"]');
    if (dueInput) {
      const date = parseDateKeyword(text);
      if (date) dueInput.value = formatLocalDateTime(date);
    }
  }

  function setSelect(form, selector, value) {
    const el = form.querySelector(selector);
    if (el) el.value = value;
  }

  function parseDateKeyword(text) {
    if (/\b(today|heute)\b/i.test(text)) return endOfDay(new Date());
    if (/\b(tomorrow|morgen)\b/i.test(text)) return endOfDay(addDays(new Date(), 1));
    if (/\b(yesterday|gestern)\b/i.test(text)) return endOfDay(addDays(new Date(), -1));
    if (/\b(next\s+week|naechste\s+woche)\b/i.test(text))
      return endOfDay(addDays(new Date(), 7));
    let m = text.match(/\bin\s+(\d+)\s+(?:day|days|tag|tage)\b/i);
    if (m) return endOfDay(addDays(new Date(), parseInt(m[1], 10)));
    m = text.match(/\b(?:next\s+)?([a-zA-Z]+)\b/);
    if (m) {
      const wd = WEEKDAYS[m[1].toLowerCase()];
      if (wd !== undefined) {
        const today = new Date();
        let delta = (wd - today.getDay() + 7) % 7;
        if (delta === 0) delta = 7;
        return endOfDay(addDays(today, delta));
      }
    }
    m = text.match(/\b(\d{4})-(\d{2})-(\d{2})\b/);
    if (m) return endOfDay(new Date(+m[1], +m[2] - 1, +m[3]));
    m = text.match(/\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b/);
    if (m) return endOfDay(new Date(+m[3], +m[2] - 1, +m[1]));
    return null;
  }
  function addDays(d, n) { const r = new Date(d); r.setDate(r.getDate() + n); return r; }
  function endOfDay(d)   { const r = new Date(d); r.setHours(23, 59, 0, 0); return r; }
  function pad(n)        { return String(n).padStart(2, "0"); }
  function formatLocalDateTime(d) {
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  // Comment visibility toggles work both inside the inspector and on
  // standalone pages (task detail), so handle them via delegation.
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".comment-visibility-toggle");
    if (btn) onVisibilityToggle({ preventDefault() {}, currentTarget: btn });
  });

  // Comment delete — author (or task owner) only; the button is only rendered
  // for them. Same delegation as the visibility toggle so it works inside the
  // inspector and on the standalone task-detail page.
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".comment-delete-btn");
    if (btn) onCommentDelete({ preventDefault() {}, currentTarget: btn });
  });

  // Comment edit — author (or task owner) only; button only rendered for them.
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".comment-edit-btn");
    if (btn) onCommentEdit({ preventDefault() {}, currentTarget: btn });
  });

  function onCommentEdit(ev) {
    ev.preventDefault();
    const btn = ev.currentTarget;
    const row = btn.closest(".comment-item, .task-detail-comment");
    if (!row || row.querySelector(".comment-edit-form")) return; // already open
    const textEl = row.querySelector(".comment-text");
    const rawEl = row.querySelector(".comment-raw");
    if (!textEl) return;
    const raw = rawEl ? rawEl.textContent : textEl.textContent;
    const { taskId } = btn.dataset;
    const form = document.createElement("div");
    form.className = "comment-edit-form mt-1";
    form.innerHTML =
      '<textarea class="form-control form-control-sm mb-1" rows="3"></textarea>' +
      '<div class="d-flex gap-2">' +
      '<button type="button" class="btn btn-sm btn-primary comment-edit-save">Save</button>' +
      '<button type="button" class="btn btn-sm btn-link text-muted comment-edit-cancel">Cancel</button>' +
      "</div>";
    const ta = form.querySelector("textarea");
    ta.value = raw;
    textEl.classList.add("d-none");
    textEl.after(form);
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);

    const close = () => { form.remove(); textEl.classList.remove("d-none"); };
    form.querySelector(".comment-edit-cancel").addEventListener("click", close);
    form.querySelector(".comment-edit-save").addEventListener("click", async () => {
      const value = ta.value.trim();
      if (!value) { ta.focus(); return; }
      // Read the index at SAVE time — a concurrent delete re-indexes the button.
      const index = btn.dataset.index;
      const saveBtn = form.querySelector(".comment-edit-save");
      saveBtn.disabled = true;
      try {
        const fd = new FormData();
        const csrf = document.querySelector('input[name="csrf_token"]');
        if (csrf) fd.append("csrf_token", csrf.value);
        fd.append("body", value);
        const resp = await fetch(`/tasks/${taskId}/comments/${index}/edit`, {
          method: "POST", body: fd, headers: { "X-Inspector-Request": "1" },
        });
        if (!resp.ok) throw new Error("edit failed: " + resp.status);
        const data = await resp.json();
        textEl.innerHTML = data.body_html || escapeHtml(data.body);
        if (rawEl) rawEl.textContent = data.body;
        close();
      } catch (err) {
        console.error(err);
        saveBtn.disabled = false;
      }
    });
  }

  async function onCommentDelete(ev) {
    ev.preventDefault();
    const btn = ev.currentTarget;
    if (!window.confirm("Delete this comment?")) return;
    const { taskId, index } = btn.dataset;
    const idx = parseInt(index, 10);
    btn.disabled = true;
    try {
      const fd = new FormData();
      const csrf = document.querySelector('input[name="csrf_token"]');
      if (csrf) fd.append("csrf_token", csrf.value);
      const resp = await fetch(`/tasks/${taskId}/comments/${idx}/delete`, {
        method: "POST", body: fd, headers: { "X-Inspector-Request": "1" },
      });
      if (!resp.ok) throw new Error("delete failed: " + resp.status);
      // Remove the row (inspector uses .comment-item, detail .task-detail-comment)
      // and re-index everything below it so subsequent operations still target
      // the right embedded-list position.
      const row = btn.closest(".comment-item, .task-detail-comment");
      const scope = row && row.parentElement;
      if (row) row.remove();
      if (scope) reindexComments(scope, idx);
      const counter =
        (scope && scope.closest("section, .comments-block") || document)
          .querySelector(".comments-block .badge, #comments .badge, h2 .text-muted");
      if (counter) {
        const n = parseInt(counter.textContent.replace(/\D/g, ""), 10);
        if (!isNaN(n)) counter.textContent = counter.textContent.replace(/\d+/, String(Math.max(0, n - 1)));
      }
    } catch (err) {
      console.error(err);
      btn.disabled = false;
    }
  }

  // After a comment at ``removed`` is deleted, shift every still-rendered row
  // with a higher index down by one — on the container's data-comment-index and
  // on its action buttons' data-index.
  function reindexComments(scope, removed) {
    scope.querySelectorAll("[data-comment-index]").forEach((row) => {
      const cur = parseInt(row.dataset.commentIndex, 10);
      if (isNaN(cur) || cur <= removed) return;
      const next = cur - 1;
      row.dataset.commentIndex = String(next);
      row.querySelectorAll("[data-index]").forEach((b) => {
        const bi = parseInt(b.dataset.index, 10);
        if (bi === cur) b.dataset.index = String(next);
      });
    });
  }

  // ---- Comment sort order (newest vs oldest first) -----------------------
  // Remembered per browser. The DOM is rendered newest-first (each row keeps
  // its real data-index), so the default needs no reversal — the newest is at
  // the top of the scroll box. "Oldest first" flips the view via the
  // column-reverse ``oldest-first`` class.
  const COMMENT_SORT_KEY = "cairn:comment-sort";
  function commentsNewestFirst() {
    try { return localStorage.getItem(COMMENT_SORT_KEY) !== "oldest"; }
    catch (e) { return true; }
  }
  function applyCommentSort(scope) {
    const newest = commentsNewestFirst();
    (scope || document)
      .querySelectorAll(".comments-list, .task-detail-comments")
      .forEach((list) => list.classList.toggle("oldest-first", !newest));
    document.querySelectorAll(".comment-sort-toggle").forEach((btn) => {
      const icon = btn.querySelector("i");
      if (icon) icon.className = newest ? "bi bi-sort-down" : "bi bi-sort-up";
      btn.title = newest
        ? "Newest first — click to show oldest first"
        : "Oldest first — click to show newest first";
    });
  }
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest(".comment-sort-toggle")) return;
    try {
      localStorage.setItem(COMMENT_SORT_KEY, commentsNewestFirst() ? "oldest" : "newest");
    } catch (e) { /* private mode — fall back to default each load */ }
    applyCommentSort();
  });
  applyCommentSort();  // initial pass for the current page (e.g. task detail)

  async function onCommentAdd(ev) {
    ev.preventDefault();
    const btn = ev.currentTarget;
    const wrap = btn.closest("[data-inspector-comment-form]");
    if (!wrap) return;
    const textarea = wrap.querySelector("textarea[name=comment_body]");
    const value = (textarea && textarea.value.trim()) || "";
    if (!value) { if (textarea) textarea.focus(); return; }
    btn.disabled = true;
    try {
      const fd = new FormData();
      const csrf = document.querySelector('input[name="csrf_token"]');
      if (csrf) fd.append("csrf_token", csrf.value);
      fd.append("body", value);
      // Labels ticked beside the box come off with the same request.
      wrap
        .querySelectorAll('input[name="remove_tags"]:checked')
        .forEach((cb) => fd.append("remove_tags", cb.value));
      // Time typed beside the box is booked with the same request.
      const dur = wrap.querySelector('input[name="log_duration"]');
      const tproj = wrap.querySelector('input[name="log_project_id"]');
      if (dur && dur.value.trim()) {
        fd.append("log_duration", dur.value.trim());
        if (tproj) fd.append("log_project_id", tproj.value);
      }
      const resp = await fetch(wrap.dataset.action, {
        method: "POST",
        body: fd,
        headers: { "X-Inspector-Request": "1" },
      });
      if (resp.status === 400) {
        // Refused before anything was written (an unreadable duration) —
        // keep the text, point at the field.
        const err = await resp.json().catch(() => ({}));
        if (dur) {
          dur.classList.add("is-invalid");
          dur.title = err.error || "Can't read this duration";
          dur.focus();
        }
        return;
      }
      if (!resp.ok) throw new Error("comment failed: " + resp.status);
      const data = await resp.json();
      appendCommentToList(data, wrap);
      reflectRemovedLabels(data.removed_tags || [], wrap);
      textarea.value = "";
      if (dur) { dur.value = ""; dur.classList.remove("is-invalid"); }
      if (data.time_block) {
        const tb = (wrap.closest(".inspector-form") || body || document)
          .querySelector(".time-block");
        if (tb) tb.innerHTML = data.time_block;
      }
    } catch (err) {
      console.error(err);
    } finally {
      btn.disabled = false;
    }
  }

  function appendCommentToList(c, form) {
    const list = body.querySelector(".comments-list");
    if (!list) return;
    const placeholder = list.querySelector("p.text-muted");
    if (placeholder) placeholder.remove();
    const action = form.dataset.action || form.action || "";
    const taskId = action.split("/").slice(-2, -1)[0]; // /tasks/<id>/comment
    const div = document.createElement("div");
    div.className = "comment-item";
    div.dataset.commentIndex = String(c.index);
    div.innerHTML =
      '<div class="d-flex align-items-center gap-2 small text-muted">' +
      `<strong class="text-body">${escapeHtml(c.author)}</strong>` +
      `<span>· ${new Date(c.created_at).toLocaleString()}</span>` +
      `<button type="button" class="comment-visibility-toggle ms-auto" ` +
      `data-task-id="${taskId}" data-index="${c.index}" data-public="${c.is_public ? "1" : "0"}" ` +
      `title="Click to toggle internal / public">` +
      visibilityBadgeHtml(c.is_public) +
      "</button>" +
      // The current user authored this comment, so they can always edit/delete.
      `<button type="button" class="comment-edit-btn btn btn-sm btn-link text-muted p-0 ms-1" ` +
      `data-task-id="${taskId}" data-index="${c.index}" title="Edit this comment">` +
      '<i class="bi bi-pencil"></i></button>' +
      `<button type="button" class="comment-delete-btn btn btn-sm btn-link text-muted p-0 ms-1" ` +
      `data-task-id="${taskId}" data-index="${c.index}" title="Delete this comment">` +
      '<i class="bi bi-trash"></i></button></div>' +
      // Use the server-rendered markdown (so pasted images / formatting show
      // immediately); fall back to escaped plain text if it's missing.
      `<div class="comment-text md-body mt-1">${c.body_html || escapeHtml(c.body)}</div>` +
      // Hidden raw markdown so the edit form can prefill the original source.
      `<div class="comment-raw d-none"></div>`;
    // Store the raw markdown as text (not innerHTML) so the edit form prefills
    // the original source rather than the rendered HTML.
    const rawEl = div.querySelector(".comment-raw");
    if (rawEl) rawEl.textContent = c.body || "";
    // Comments render newest-first, so a new one goes to the top of the DOM.
    list.insertBefore(div, list.firstChild);
    list.scrollTop = 0;  // keep the freshly added comment in view
    // Visibility toggle wires up via the document-level delegate.
    // Bump the badge counter.
    const counter = body.querySelector(".comments-block .badge");
    if (counter) counter.textContent = String(parseInt(counter.textContent, 10) + 1);
  }

  // A comment that took labels off leaves two things behind that still show
  // the old truth: the chip offering the removal, and — the one that bites —
  // the label picker above, whose <select> is the inspector form's source of
  // truth. Left selected, the next Save would put the label straight back on.
  function reflectRemovedLabels(names, wrap) {
    if (!names.length) return;
    const lower = names.map((n) => n.toLowerCase());
    const row = wrap.querySelector(".comment-drop-labels");
    if (row) {
      row.querySelectorAll('input[name="remove_tags"]').forEach((cb) => {
        if (lower.indexOf((cb.value || "").toLowerCase()) === -1) return;
        const label = row.querySelector('label[for="' + cb.id + '"]');
        if (label) label.remove();
        cb.remove();
      });
      if (!row.querySelector('input[name="remove_tags"]')) row.remove();
    }
    const scope = wrap.closest(".inspector-form") || body || document;
    scope.querySelectorAll("select[multiple][data-tag-toggle]").forEach((sel) => {
      let touched = false;
      Array.from(sel.options).forEach((o) => {
        if (o.selected && lower.indexOf(o.value.toLowerCase()) !== -1) {
          o.selected = false;
          touched = true;
        }
      });
      // tag-toggle.js draws its chips from the <select>; this is how something
      // else says "I changed it, draw it again".
      if (touched) sel.dispatchEvent(new Event("tags:external-change"));
    });
  }

  function visibilityBadgeHtml(isPublic) {
    return isPublic
      ? '<span class="badge text-bg-success"><i class="bi bi-globe"></i> public</span>'
      : '<span class="badge text-bg-secondary"><i class="bi bi-lock"></i> internal</span>';
  }

  async function onVisibilityToggle(ev) {
    ev.preventDefault();
    const btn = ev.currentTarget;
    const { taskId, index } = btn.dataset;
    btn.disabled = true;
    try {
      const fd = new FormData();
      const csrf = document.querySelector('input[name="csrf_token"]');
      if (csrf) fd.append("csrf_token", csrf.value);
      const resp = await fetch(
        `/tasks/${taskId}/comments/${index}/toggle-visibility`,
        { method: "POST", body: fd, headers: { "X-Inspector-Request": "1" } }
      );
      if (!resp.ok) throw new Error("toggle failed: " + resp.status);
      const data = await resp.json();
      btn.dataset.public = data.is_public ? "1" : "0";
      btn.innerHTML = visibilityBadgeHtml(data.is_public);
    } catch (err) {
      console.error(err);
    } finally {
      btn.disabled = false;
    }
  }

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  // The comment box lives inside the task form but is a separate action.
  // Before saving the task, persist any typed-but-unposted comment so the
  // save doesn't silently discard it (the form re-render would wipe the
  // textarea). The freshly-saved fragment then shows the new comment.
  async function flushPendingComment(form) {
    const wrap = form.querySelector("[data-inspector-comment-form]");
    if (!wrap) return;
    const textarea = wrap.querySelector("textarea[name=comment_body]");
    const value = textarea && textarea.value.trim();
    if (!value) return;
    try {
      const fd = new FormData();
      const csrf = form.querySelector('input[name="csrf_token"]') ||
                   document.querySelector('input[name="csrf_token"]');
      if (csrf) fd.append("csrf_token", csrf.value);
      fd.append("body", value);
      await fetch(wrap.dataset.action, {
        method: "POST", body: fd, headers: { "X-Inspector-Request": "1" },
      });
      textarea.value = "";  // don't resend it with the task save / re-render
    } catch (e) { /* non-fatal — proceed with the task save */ }
  }

  async function onFormSubmit(ev) {
    ev.preventDefault();
    const form = ev.currentTarget;
    const submitBtn = form.querySelector('button[type="submit"], input[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;

    try {
      await flushPendingComment(form);
      const resp = await fetch(form.action || window.location.href, {
        method: form.method || "POST",
        body: new FormData(form),
        headers: { "X-Inspector-Request": "1" },
        redirect: "follow",
      });
      if (resp.status >= 200 && resp.status < 400) {
        // The save dropped the actor out of scope (e.g. they reassigned a
        // task they only saw via assignment). Don't keep an inspector open
        // on a task they can no longer load — navigate where the server says.
        const goTo = resp.headers.get("X-Inspector-Redirect");
        if (goTo) { window.location.href = goTo; return; }
        // "Create then attach": if this was a NEW task with held attachments,
        // grab them (the pending container lives in the form we just submitted)
        // so we can upload them to the freshly-created task below.
        const wasCreate = /\/tasks\/new(\?|$)/.test(form.getAttribute("action") || "");
        const pendingC = wasCreate ? form.querySelector("[data-pending-attachments]") : null;
        const pendingCount = (pendingC && window.PendingAttachments)
          ? window.PendingAttachments.count(pendingC) : 0;
        const newTitle = pendingCount
          ? (form.querySelector('input[name="title"]') || {}).value : "";
        // The new task's id, surfaced by the create route (works on add-another
        // too, whose fragment has no id).
        const createdId = resp.headers.get("X-Created-Task-Id");
        // "Open after creating" checkbox: jump to the new task's detail page
        // once it's saved (and any held attachments have uploaded).
        const openCreated = resp.headers.get("X-Open-Created-Task") === "1";
        // Server returns the saved-state inspector fragment. Keep the panel
        // open and just swap the body.
        body.innerHTML = await resp.text();
        wireForm();
        document.dispatchEvent(new Event("inspector:loaded"));
        scrollSavedBannerIntoView();
        autoFadeSavedBanner();
        if (pendingCount && createdId) {
          // Upload the held files to the freshly-created task (they append to
          // its note server-side). On a normal create reopen so the note shows
          // them; on add-another keep the fresh empty form for the next task.
          await window.PendingAttachments.flush(pendingC, createdId);
          const reForm = body.querySelector("form[data-inspector-form]");
          const stillNew = /\/tasks\/new(\?|$)/.test(
            (reForm && reForm.getAttribute("action")) || "");
          // Skip the inspector reopen when we're about to navigate to the
          // full detail page (open-after-create) — that would be wasted work.
          if (!stillNew && !openCreated) openInspector("/tasks/" + createdId, newTitle);
        }
        if (openCreated && createdId) {
          window.location.href = "/tasks/" + createdId;
          return;
        }
        // Refresh the underlying main content so newly created sections /
        // updated counters / moved tasks are visible immediately, without
        // closing the inspector.
        document.dispatchEvent(new Event("bktodo:refresh-needed"));
        needsReloadOnClose = false;
      } else if (resp.status === 422 || resp.status === 400) {
        body.innerHTML = await resp.text();
        wireForm();
        document.dispatchEvent(new Event("inspector:loaded"));
      } else {
        throw new Error("save failed: " + resp.status);
      }
    } catch (err) {
      const alertDiv = document.createElement("div");
      alertDiv.className = "alert alert-danger";
      alertDiv.textContent = err.message;
      body.prepend(alertDiv);
      if (submitBtn) submitBtn.disabled = false;
    }
  }

  function scrollSavedBannerIntoView() {
    const banner = body.querySelector(".inspector-saved-banner");
    if (banner) banner.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function autoFadeSavedBanner() {
    const banner = body.querySelector(".inspector-saved-banner");
    if (!banner) return;
    setTimeout(() => banner.classList.add("fade-out"), 1500);
    setTimeout(() => banner.remove(), 2500);
  }

  async function onSideSubmit(ev) {
    if (!confirm(ev.currentTarget.dataset.confirm || "Are you sure?")) {
      ev.preventDefault();
      return;
    }
    ev.preventDefault();
    const f = ev.currentTarget;
    const resp = await fetch(f.action, {
      method: f.method || "POST",
      body: new FormData(f),
      headers: { "X-Inspector-Request": "1" },
    });
    if (!resp.ok && !resp.redirected) return;
    bsOffcanvas.hide();
    // The server may redirect to a new place (e.g., the newly created
    // project after converting a task). Follow that redirect rather than
    // reloading the current page — otherwise the new item lives outside
    // the user's current view and looks like it disappeared.
    const target = resp.redirected ? resp.url : null;
    if (target && target !== window.location.href) {
      window.location.href = target;
    } else {
      window.location.reload();
    }
  }

  document.addEventListener("click", (ev) => {
    // Let bulk-selection handle modifier-clicks (Cmd/Ctrl for toggle,
    // Shift for range) — never open the inspector when the user is
    // multi-selecting.
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey) return;
    const trigger = ev.target.closest("[data-inspector-url]");
    if (!trigger) return;
    ev.preventDefault();
    // Loading another item replaces the panel body — same loss as closing it.
    if (!confirmDiscardDraft()) return;
    openInspector(trigger.dataset.inspectorUrl, trigger.dataset.inspectorTitle);
  });

  // Clicking a link behind the panel navigates away and takes the draft with
  // it — the browser's own prompt is the only guard that works there.
  window.addEventListener("beforeunload", (ev) => {
    if (!unsavedNewTask) return;
    ev.preventDefault();
    ev.returnValue = "";
  });
})();

/* Inspector quick-plan: one click sets plan_date (today/tomorrow/day-after)
 * plus a short comment, saved immediately via a lightweight endpoint — no full
 * form Save. Delegated on document (not the offcanvas) so it also drives the
 * inline inspector on the task-detail page, and survives the body swap that
 * follows a normal save. */
(function () {
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-quick-plan]");
    if (!btn) return;
    const block = btn.closest("[data-quick-plan-block]");
    if (!block) return;
    ev.preventDefault();
    const when = btn.dataset.quickPlan;
    const noteEl = block.querySelector("[data-quick-plan-note]");
    const status = block.querySelector("[data-quick-plan-status]");
    const fd = new FormData();
    fd.append("csrf_token", block.dataset.csrf || "");
    fd.append("when", when);
    fd.append("plan_note", noteEl ? noteEl.value : "");
    btn.disabled = true;
    try {
      const resp = await fetch("/tasks/" + block.dataset.taskId + "/quick-plan", {
        method: "POST",
        body: fd,
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (!resp.ok) throw new Error("quick-plan failed: " + resp.status);
      const data = await resp.json();
      if (when === "clear" && noteEl) noteEl.value = "";
      // Keep the inspector's own plan_date field in sync so a subsequent full
      // Save doesn't clobber what we just set (datetime-local wants YYYY-MM-DDTHH:MM).
      const planField = document.getElementById("plan_date");
      if (planField) planField.value = data.plan_date ? data.plan_date.slice(0, 16) : "";
      if (status) {
        status.textContent = when === "clear" ? "Cleared" : "Planned ✓";
        status.hidden = false;
        setTimeout(() => { status.hidden = true; }, 1500);
      }
      // Refresh the list behind the panel so the comment prefix shows at once.
      document.dispatchEvent(new Event("bktodo:refresh-needed"));
    } catch (e) {
      if (status) { status.textContent = "Failed"; status.hidden = false; }
    } finally {
      btn.disabled = false;
    }
  });
})();

/* "Refresh from Zammad": pull THIS ticket now instead of waiting for the next
 * scheduled pass. Delegated on document — like quick-plan above — so it drives
 * the inline inspector on the detail page as well as the offcanvas, and keeps
 * working after either of them has been re-rendered.
 *
 * It is one outbound call to a live external system per click, so the button
 * says it is busy, refuses a second click while it is, and puts a failure where
 * the user can read it instead of doing nothing visible. */
(function () {
  const PAUSE_MS = 700;   // long enough to read "Updated" before the re-render

  function summarise(data) {
    const bits = [];
    if (data.comments_imported) bits.push(data.comments_imported + " new comments");
    if (data.time_entries_imported) bits.push(data.time_entries_imported + " time entries");
    if (data.reassigned) bits.push("reassigned");
    return "Updated" + (bits.length ? " — " + bits.join(", ") : "");
  }

  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-zammad-refresh]");
    if (!btn) return;
    const block = btn.closest("[data-zammad-refresh-block]");
    if (!block) return;
    ev.preventDefault();
    // A second click while the first is in flight would start a second sync of
    // the same ticket against a live Zammad. One at a time.
    if (block.dataset.busy === "1") return;
    block.dataset.busy = "1";

    const status = block.querySelector("[data-zammad-refresh-status]");
    const icon = block.querySelector("[data-zammad-refresh-icon]");
    const taskId = block.dataset.taskId;
    // Whether this inspector is the offcanvas (which a <main> swap never
    // touches) or the detail page's inline one (which it re-renders).
    const inPanel = !!btn.closest("#inspector-panel");

    function say(text, cls) {
      if (!status) return;
      status.className = "ms-1 small " + (cls || "text-muted");
      status.textContent = text;
      status.hidden = false;
    }

    btn.disabled = true;
    if (icon) icon.classList.add("spin");
    say("Asking Zammad…");
    try {
      const fd = new FormData();
      fd.append("csrf_token", block.dataset.csrf || "");
      const resp = await fetch("/tasks/" + taskId + "/zammad-refresh", {
        method: "POST",
        body: fd,
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      let data = {};
      try { data = await resp.json(); } catch (e) { /* non-JSON error page */ }
      if (!resp.ok || !data.ok) {
        // Token gone, ticket deleted, Zammad down: say which, so there is
        // something to do about it. The button stays clickable for a retry.
        say(data.error || ("Zammad refresh failed (" + resp.status + ")"),
            "text-danger");
        btn.disabled = false;
        if (icon) icon.classList.remove("spin");
        block.dataset.busy = "0";
        return;
      }
      say(summarise(data), "text-success");
      setTimeout(() => {
        // Show what came back without a page reload. The offcanvas has to be
        // re-fetched; the inline inspector rides the <main> refresh, which also
        // updates the list behind it.
        if (inPanel && typeof window.bktodoOpenInspector === "function") {
          window.bktodoOpenInspector("/tasks/" + taskId, data.title || "");
          document.dispatchEvent(new Event("bktodo:refresh-needed"));
        } else {
          document.dispatchEvent(new Event("bktodo:refresh-needed"));
        }
      }, PAUSE_MS);
    } catch (err) {
      say("Zammad refresh failed: " + err.message, "text-danger");
      btn.disabled = false;
      if (icon) icon.classList.remove("spin");
      block.dataset.busy = "0";
    }
  });
})();
