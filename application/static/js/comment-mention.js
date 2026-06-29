/* @-mention autocomplete for comment boxes.
 *
 * Typing "@" in a comment textarea pops a dropdown of teammates (the same
 * assignable-users set behind the +name quick-add token, served by
 * /tasks/assignees). Picking one inserts "@username". On submit the server
 * (tasks._notify_mentions) resolves each @name to a teammate and fires an
 * in-app notification.
 *
 * Document-level delegated + a single floating dropdown appended to <body>, so
 * it keeps working after the inspector body or <main> is swapped out.
 */
(function () {
  // Both comment boxes: the inspector's (name="comment_body") and the full
  // task-detail form's (CommentForm body, scoped to its form). Mirrors the
  // selector in note-paste.js.
  const COMMENT_SEL =
    'textarea[name="comment_body"], .task-detail-comment-form textarea';

  const box = document.createElement("div");
  box.className = "qa-assignee-ac comment-mention-ac d-none";
  box.style.position = "fixed";
  box.style.zIndex = "2200";
  box.style.minWidth = "12rem";
  document.body.appendChild(box);

  let items = [];
  let index = -1;
  let token = null; // { textarea, start, end, query }
  let seq = 0;

  function escape(s) {
    return String(s).replace(/[&<>]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }

  function commentTextarea(el) {
    return el && el.matches && el.matches(COMMENT_SEL) ? el : null;
  }

  // The partial "@token" immediately before the caret, or null.
  function tokenAtCaret(textarea) {
    const pos = textarea.selectionStart || 0;
    const before = textarea.value.slice(0, pos);
    const m = before.match(/(?:^|\s)@([\w.\-äöüÄÖÜß]*)$/);
    if (!m) return null;
    // start points at the "@" itself (‑1 for it), so the whole "@partial" is
    // replaced by the picked "@username" — otherwise the original "@" stays and
    // we'd end up with two.
    return { textarea, start: pos - m[1].length - 1, end: pos, query: m[1] };
  }

  function hide() {
    box.classList.add("d-none");
    box.innerHTML = "";
    items = []; index = -1; token = null;
  }

  function position(textarea) {
    const r = textarea.getBoundingClientRect();
    box.style.left = r.left + "px";
    box.style.top = (r.bottom + 4) + "px";
    box.style.maxWidth = Math.max(r.width, 192) + "px";
  }

  function render() {
    if (!items.length) { hide(); return; }
    box.innerHTML = items.map((u, i) => {
      const label = u.display_name && u.display_name !== u.username
        ? `${escape(u.username)} <span class="text-muted">${escape(u.display_name)}</span>`
        : escape(u.username);
      return `<button type="button" class="qa-ac-item${i === index ? " active" : ""}" data-i="${i}">`
        + `<i class="bi bi-at"></i> ${label}${u.is_me ? ' <span class="text-muted">(me)</span>' : ""}</button>`;
    }).join("");
    box.classList.remove("d-none");
  }

  async function refresh(textarea) {
    const tok = tokenAtCaret(textarea);
    if (!tok) { hide(); return; }
    token = tok;
    position(textarea);
    const mine = ++seq;
    try {
      const resp = await fetch(`/tasks/assignees?q=${encodeURIComponent(tok.query)}`,
                               { credentials: "same-origin" });
      if (!resp.ok || mine !== seq) return;
      const data = await resp.json();
      items = data.users || [];
      index = items.length ? 0 : -1;
      render();
    } catch (e) { hide(); }
  }

  function apply(i) {
    const u = items[i];
    if (!u || !token) return;
    const ta = token.textarea;
    const v = ta.value;
    const replacement = "@" + u.username + " ";
    ta.value = v.slice(0, token.start) + replacement + v.slice(token.end);
    const caret = token.start + replacement.length;
    ta.setSelectionRange(caret, caret);
    hide();
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    ta.focus();
  }

  document.addEventListener("input", (ev) => {
    const ta = commentTextarea(ev.target);
    if (ta) refresh(ta);
  });

  // Capture phase so we intercept Enter/Tab before the comment form's own
  // Enter/Cmd-Enter handling kicks in.
  document.addEventListener("keydown", (ev) => {
    if (box.classList.contains("d-none") || !items.length) return;
    if (!commentTextarea(ev.target)) return;
    if (ev.key === "ArrowDown") {
      ev.preventDefault(); index = (index + 1) % items.length; render();
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault(); index = (index - 1 + items.length) % items.length; render();
    } else if (ev.key === "Enter" || ev.key === "Tab") {
      ev.preventDefault(); ev.stopPropagation(); apply(index);
    } else if (ev.key === "Escape") {
      ev.preventDefault(); ev.stopPropagation(); hide();
    }
  }, true);

  box.addEventListener("mousedown", (ev) => {
    const btn = ev.target.closest(".qa-ac-item");
    if (!btn) return;
    ev.preventDefault(); // keep textarea focus
    apply(parseInt(btn.dataset.i, 10));
  });

  // Hide on blur (deferred so a mousedown pick still registers) and on any
  // outside click / scroll that would leave the box floating in the wrong spot.
  document.addEventListener("focusout", (ev) => {
    if (commentTextarea(ev.target)) setTimeout(hide, 120);
  });
  document.addEventListener("scroll", () => { if (!box.classList.contains("d-none")) hide(); }, true);
})();
