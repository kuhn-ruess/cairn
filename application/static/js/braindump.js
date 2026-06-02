/* Braindump editor — Markdown scratch pad with list-indent helpers and a
   one-click convert into projects/tasks. Self-guards: does nothing unless
   the braindump textarea is on the page. */
(function () {
  const ta = document.getElementById("braindumpText");
  if (!ta) return;

  const INDENT = "    "; // 4 spaces = one nesting level
  const btn = document.getElementById("braindumpConvert");
  const clearBtn = document.getElementById("braindumpClear");
  const status = document.getElementById("braindumpStatus");
  const result = document.getElementById("braindumpResult");
  const csrf = document.getElementById("braindumpCsrf");

  function setStatus(text) { if (status) status.textContent = text; }

  // ---- persistence: debounced server-side autosave (survives across
  //      devices/browsers, not just this one). The textarea is prefilled
  //      server-side, so there's nothing to load here. ----
  async function save(text) {
    const fd = new FormData();
    fd.append("text", text);
    if (csrf) fd.append("csrf_token", csrf.value);
    const resp = await fetch("/braindump/save", {
      method: "POST", body: fd, credentials: "same-origin",
    });
    if (!resp.ok) throw new Error("save failed");
  }

  let saveTimer = null;
  ta.addEventListener("input", () => {
    setStatus("Saving…");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      try { await save(ta.value); setStatus("Saved"); }
      catch (e) { setStatus("Save failed"); }
    }, 700);
  });

  // ---- selected whole-line range [start of first line, end of last line] ----
  function lineRange() {
    const v = ta.value;
    let s = ta.selectionStart, e = ta.selectionEnd;
    s = v.lastIndexOf("\n", s - 1) + 1;
    const nl = v.indexOf("\n", e);
    e = nl === -1 ? v.length : nl;
    return { s, e };
  }

  function replaceRange(s, e, text, selStart, selEnd) {
    ta.setRangeText(text, s, e, "preserve");
    if (selStart != null) ta.setSelectionRange(selStart, selEnd != null ? selEnd : selStart);
    ta.dispatchEvent(new Event("input"));
  }

  ta.addEventListener("keydown", (ev) => {
    // Tab / Shift+Tab — indent or outdent every line in the selection.
    if (ev.key === "Tab") {
      ev.preventDefault();
      const { s, e } = lineRange();
      const block = ta.value.slice(s, e);
      const lines = block.split("\n");
      let delta = 0, firstDelta = 0;
      const out = lines.map((line, i) => {
        if (ev.shiftKey) {
          const m = line.match(/^( {1,4}|\t)/);
          const removed = m ? m[0].length : 0;
          if (i === 0) firstDelta = -removed;
          delta -= removed;
          return line.slice(removed);
        }
        if (i === 0) firstDelta = INDENT.length;
        delta += INDENT.length;
        return INDENT + line;
      }).join("\n");
      const newStart = Math.max(s, ta.selectionStart + firstDelta);
      const newEnd = ta.selectionEnd + delta;
      replaceRange(s, e, out, newStart, newEnd);
      return;
    }

    // Enter — continue a list, preserving indentation and bullet style.
    if (ev.key === "Enter" && !ev.shiftKey) {
      const v = ta.value;
      const ls = v.lastIndexOf("\n", ta.selectionStart - 1) + 1;
      const line = v.slice(ls, ta.selectionStart);
      const m = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
      if (m) {
        ev.preventDefault();
        if (!m[3]) {
          // Empty bullet → break out of the list.
          replaceRange(ls, ta.selectionStart, "", ls);
          return;
        }
        let marker = m[2];
        if (/\d/.test(marker)) marker = (parseInt(marker, 10) + 1) + marker.replace(/\d+/, "");
        const insert = "\n" + m[1] + marker + " ";
        const pos = ta.selectionStart;
        replaceRange(pos, pos, insert, pos + insert.length);
      }
    }
  });

  // ---- clear ----
  if (clearBtn) {
    clearBtn.addEventListener("click", async () => {
      ta.value = "";
      result.innerHTML = "";
      ta.focus();
      clearTimeout(saveTimer);
      try { await save(""); setStatus("Cleared"); }
      catch (e) { setStatus("Save failed"); }
    });
  }

  // ---- convert ----
  if (btn) {
    btn.addEventListener("click", async () => {
      const text = ta.value.trim();
      result.innerHTML = "";
      if (!text) {
        result.innerHTML = '<div class="alert alert-warning">Nothing to convert.</div>';
        return;
      }
      btn.disabled = true;
      try {
        const fd = new FormData();
        fd.append("text", text);
        if (csrf) fd.append("csrf_token", csrf.value);
        const resp = await fetch("/braindump/convert", {
          method: "POST", body: fd, credentials: "same-origin",
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          result.innerHTML = '<div class="alert alert-danger">' +
            (data.error || "Conversion failed.") + "</div>";
          return;
        }
        const s = data.stats || {};
        result.innerHTML =
          '<div class="alert alert-success d-flex align-items-center gap-3">' +
          '<span>Created <strong>' + (s.tasks_created || 0) + '</strong> tasks' +
          (s.tasks_updated ? ', updated <strong>' + s.tasks_updated + '</strong>' : '') +
          ' in <strong>' + (s.projects_created || 0) + '</strong> new project(s)' +
          (s.projects_matched ? ' (+' + s.projects_matched + ' existing reused)' : '') +
          '.</span>' +
          '<a class="btn btn-sm btn-outline-success ms-auto" href="/projects/">View projects</a>' +
          "</div>";
        // Keep the text: converting is idempotent, so the dump stays as a
        // re-convertible source — tweak a date and convert again to update.
      } catch (err) {
        result.innerHTML = '<div class="alert alert-danger">' + err.message + "</div>";
      } finally {
        btn.disabled = false;
      }
    });
  }
})();
