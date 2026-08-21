/* Braindump editor — Markdown scratch pad with list-indent helpers, and the
   two ways out of it: mark text (or nothing, for the whole pad) and turn it
   into tasks or into a note. Whatever gets converted leaves the pad, so the
   dump empties as it is processed. The dialogs live in capture.js.
   Self-guards: does nothing unless the braindump textarea is on the page. */
(function () {
  const ta = document.getElementById("braindumpText");
  if (!ta) return;

  const INDENT = "    "; // 4 spaces = one nesting level
  const tasksBtn = document.getElementById("braindumpToTasks");
  const noteBtn = document.getElementById("braindumpToNote");
  const scopeHint = document.getElementById("braindumpScope");
  const clearBtn = document.getElementById("braindumpClear");
  const status = document.getElementById("braindumpStatus");
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
      const m = line.match(/^(\s*)([-*+]|\d+[.)])\s+(\[[ xX]\]\s+)?(.*)$/);
      if (m) {
        ev.preventDefault();
        if (!m[4]) {
          // Empty bullet → break out of the list.
          replaceRange(ls, ta.selectionStart, "", ls);
          return;
        }
        let marker = m[2];
        if (/\d/.test(marker)) marker = (parseInt(marker, 10) + 1) + marker.replace(/\d+/, "");
        // A checklist continues as a checklist — unticked, whatever the line
        // above was.
        const insert = "\n" + m[1] + marker + " " + (m[3] ? "[ ] " : "");
        const pos = ta.selectionStart;
        replaceRange(pos, pos, insert, pos + insert.length);
      }
    }
  });

  // ---- checklist: turn the marked lines into "- [ ] " and back ----
  const checkBtn = document.getElementById("braindumpChecklist");
  if (checkBtn) {
    checkBtn.addEventListener("click", () => {
      const { s, e } = lineRange();
      const out = window.CairnMdChecklist.toggleLines(ta.value.slice(s, e).split("\n"));
      replaceRange(s, e, out.join("\n"), s, s + out.join("\n").length);
      ta.focus();
    });
  }

  // ---- clear ----
  if (clearBtn) {
    clearBtn.addEventListener("click", async () => {
      if (ta.value.trim() && !confirm("Clear the braindump? The text is gone.")) return;
      ta.value = "";
      ta.focus();
      clearTimeout(saveTimer);
      try { await save(""); setStatus("Cleared"); }
      catch (e) { setStatus("Save failed"); }
      refreshScope();
    });
  }

  // ---- converting: marked text, or the whole pad when nothing is marked ----

  /** What the buttons act on: the marked range, else everything. */
  function scope() {
    const s = ta.selectionStart, e = ta.selectionEnd;
    if (e > s) return { start: s, end: e, text: ta.value.slice(s, e), marked: true };
    return { start: 0, end: ta.value.length, text: ta.value, marked: false };
  }

  function refreshScope() {
    if (!scopeHint) return;
    const sc = scope();
    const lines = sc.text.split("\n").filter((l) => l.trim()).length;
    scopeHint.textContent = sc.marked
      ? `${lines} marked line(s) — the buttons act on the selection`
      : "Nothing marked — the buttons act on the whole braindump";
    if (tasksBtn) tasksBtn.querySelector(".capture-scope").textContent =
      sc.marked ? "Selection" : "Everything";
    if (noteBtn) noteBtn.querySelector(".capture-scope").textContent =
      sc.marked ? "Selection" : "Everything";
  }

  /** Converted text leaves the pad. Offsets can be stale (the pad stays
      editable while the dialog is open), so the removal is verified against
      the text that was actually converted and skipped if it moved on. */
  function removeConverted(sc) {
    let { start, end } = sc;
    if (ta.value.slice(start, end) !== sc.text) {
      const at = ta.value.indexOf(sc.text);
      if (at < 0) { setStatus("Kept the text — it changed while converting"); return; }
      start = at; end = at + sc.text.length;
    }
    ta.value = (ta.value.slice(0, start) + ta.value.slice(end))
      .replace(/\n{3,}/g, "\n\n").replace(/^\n+/, "");
    ta.setSelectionRange(start, start);
    ta.dispatchEvent(new Event("input"));   // triggers the autosave
    refreshScope();
  }

  // ``select`` alone misses a click that collapses the selection, so the
  // pointer/key events are watched too.
  ["select", "input", "keyup", "mouseup", "blur"].forEach(
    (ev) => ta.addEventListener(ev, refreshScope));
  refreshScope();

  if (tasksBtn) {
    tasksBtn.addEventListener("click", () => {
      const sc = scope();
      window.CairnCapture.tasks(sc.text, { onDone: () => removeConverted(sc) });
    });
  }

  if (noteBtn) {
    noteBtn.addEventListener("click", () => {
      const sc = scope();
      const first = sc.text.split("\n").find((l) => l.trim()) || "";
      window.CairnCapture.note(sc.text, {
        title: first.replace(/^\s*(?:[-*+•]|\d+[.)]|#{1,6}|>)\s+/, "").trim().slice(0, 60),
        onDone: () => removeConverted(sc),
      });
    });
  }
})();
