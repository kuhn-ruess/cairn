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
  function isPinned() {
    try { return localStorage.getItem(PIN_KEY) === "1"; }
    catch (e) { return false; }
  }
  function reflectPin() {
    const on = isPinned();
    document.body.classList.toggle("inspector-pinned", on);
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
      if (next) { bsOffcanvas.show(); showPinnedPlaceholder(); }
    });
  }
  reflectPin();
  // Pinned → open the column on load so it's a permanent fixture.
  if (isPinned()) { bsOffcanvas.show(); showPinnedPlaceholder(); }

  // When the user closes the inspector after at least one successful save,
  // refresh the list behind it so the change is visible. We *don't* reload
  // immediately on save — that would close the panel.
  let needsReloadOnClose = false;
  panel.addEventListener("show.bs.offcanvas", () => {
    document.body.classList.add("inspector-docked");
  });
  panel.addEventListener("hidden.bs.offcanvas", () => {
    document.body.classList.remove("inspector-docked");
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
    bsOffcanvas.hide();
  }, true);

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
      if (!resp.ok) throw new Error("fetch failed: " + resp.status);
      const html = await resp.text();
      body.innerHTML = html;
      wireForm();
      document.dispatchEvent(new Event("inspector:loaded"));
    } catch (err) {
      body.innerHTML =
        '<div class="alert alert-danger">' + err.message + "</div>";
    }
  }

  function wireForm() {
    const form = body.querySelector("form[data-inspector-form]");
    if (form) {
      form.addEventListener("submit", onFormSubmit);
      // Enter saves: Cmd/Ctrl+Enter from anywhere (incl. textareas), and plain
      // Enter from single-line fields. Textareas keep Enter = newline; buttons,
      // links and selects keep their native Enter behaviour.
      form.addEventListener("keydown", (ev) => {
        if (ev.key !== "Enter" || ev.isComposing) return;
        const t = ev.target;
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
      const resp = await fetch(wrap.dataset.action, {
        method: "POST",
        body: fd,
        headers: { "X-Inspector-Request": "1" },
      });
      if (!resp.ok) throw new Error("comment failed: " + resp.status);
      const data = await resp.json();
      appendCommentToList(data, wrap);
      textarea.value = "";
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
      "</button></div>" +
      `<div class="comment-body mt-1" style="white-space: pre-wrap;">${escapeHtml(c.body)}</div>`;
    list.appendChild(div);
    // Visibility toggle wires up via the document-level delegate.
    // Bump the badge counter.
    const counter = body.querySelector(".comments-block .badge");
    if (counter) counter.textContent = String(parseInt(counter.textContent, 10) + 1);
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

  async function onFormSubmit(ev) {
    ev.preventDefault();
    const form = ev.currentTarget;
    const submitBtn = form.querySelector('button[type="submit"], input[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;

    try {
      const resp = await fetch(form.action || window.location.href, {
        method: form.method || "POST",
        body: new FormData(form),
        headers: { "X-Inspector-Request": "1" },
        redirect: "follow",
      });
      if (resp.status >= 200 && resp.status < 400) {
        // Server returns the saved-state inspector fragment. Keep the panel
        // open and just swap the body.
        body.innerHTML = await resp.text();
        wireForm();
        scrollSavedBannerIntoView();
        autoFadeSavedBanner();
        // Refresh the underlying main content so newly created sections /
        // updated counters / moved tasks are visible immediately, without
        // closing the inspector.
        document.dispatchEvent(new Event("bktodo:refresh-needed"));
        needsReloadOnClose = false;
      } else if (resp.status === 422 || resp.status === 400) {
        body.innerHTML = await resp.text();
        wireForm();
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
    openInspector(trigger.dataset.inspectorUrl, trigger.dataset.inspectorTitle);
  });
})();
