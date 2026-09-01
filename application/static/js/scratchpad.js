/* Scratchpad — the two ways out of the pad.
 *
 * The pad itself is an outline, edited by static/js/outliner.js exactly as a
 * note is, and saved to /scratchpad/save by the same code path. What is left
 * here is what is specific to a scratch pad: converting part of it away.
 *
 * The unit of conversion is the BLOCK. Marking spans rendered blocks, so
 * whatever the selection touched converts whole, with everything under it —
 * which is also what makes the removal exact. The old pad matched the
 * converted string back against the text to find what to cut, and gave up when
 * it had moved on; a block is a thing, and it is either still there or it is
 * not.
 *
 * Self-guards: does nothing unless the scratchpad page is on screen.
 */
(function () {
  const page = document.querySelector(".scratchpad-page");
  if (!page) return;

  const tasksBtn = document.getElementById("scratchpadToTasks");
  const noteBtn = document.getElementById("scratchpadToNote");
  const checkBtn = document.getElementById("scratchpadChecklist");
  const scopeHint = document.getElementById("scratchpadScope");
  const clearBtn = document.getElementById("scratchpadClear");
  const status = document.getElementById("note-save-status");

  const outliner = () => window.CairnOutliner;

  function setStatus(text) { if (status) status.textContent = text; }

  /** Nothing worth converting: bullets without words are still bullets. */
  function o_empty(sc) {
    return !sc.text.replace(/^[\t ]*-[\t ]*$/gm, "").trim();
  }

  /** What the buttons act on: the marked blocks, else the whole pad. */
  function scope() {
    const o = outliner();
    return o ? o.selection() : { marked: false, blocks: [], text: "" };
  }

  function refreshScope() {
    const sc = scope();
    const lines = sc.text.split("\n").filter((l) => l.trim()).length;
    if (scopeHint) {
      scopeHint.textContent = sc.marked
        ? `${sc.blocks.length} marked block(s), ${lines} line(s) — the buttons act on those`
        : "Nothing marked — the buttons act on the whole scratchpad";
    }
    [tasksBtn, noteBtn].forEach((btn) => {
      const label = btn && btn.querySelector(".capture-scope");
      if (label) label.textContent = sc.marked ? "Selection" : "Everything";
    });
  }

  // A click that collapses the selection fires no "select", so the pointer and
  // key events are watched too.
  ["selectionchange", "mouseup", "keyup"].forEach((ev) =>
    document.addEventListener(ev, () => setTimeout(refreshScope, 0)));

  /** Converted blocks leave the pad — exactly those, even when the scope was
   *  "everything". The pad stays editable while the dialog is open, and
   *  clearing it wholesale would take anything jotted down in between. */
  async function converted(sc) {
    const o = outliner();
    if (!o) return;
    await o.remove(sc.blocks);
    setStatus("Converted");
    refreshScope();
  }

  // These act on what is marked, and a mark inside a block lives in that
  // block's open textarea (outliner.js opens the block a mark lands in). Both
  // ways of closing it have to be held off, or the mark is gone before the
  // button reads it: the outline closes the open block on a mousedown
  // anywhere off itself, and the textarea commits on blur. The button still
  // gets its click.
  [tasksBtn, noteBtn, checkBtn].forEach((btn) => {
    if (!btn) return;
    btn.dataset.keepsBlockOpen = "1";
    btn.addEventListener("mousedown", (e) => e.preventDefault());
  });

  if (tasksBtn) {
    tasksBtn.addEventListener("click", () => {
      const sc = scope();
      if (o_empty(sc)) { setStatus("Nothing to convert"); return; }
      window.CairnCapture.tasks(sc.text, { onDone: () => converted(sc) });
    });
  }

  if (noteBtn) {
    noteBtn.addEventListener("click", () => {
      const sc = scope();
      if (o_empty(sc)) { setStatus("Nothing to convert"); return; }
      const first = sc.text.split("\n").find((l) => l.trim()) || "";
      window.CairnCapture.note(sc.text, {
        title: first.replace(/^\s*(?:[-*+•]|\d+[.)]|#{1,6}|>)\s+/, "")
                    .replace(/^\[[ xX]\]\s*/, "").trim().slice(0, 60),
        onDone: () => converted(sc),
      });
    });
  }

  // ---- checklist: turn the marked blocks into checkboxes and back ----
  if (checkBtn) {
    checkBtn.addEventListener("click", async () => {
      const o = outliner();
      if (!o) return;
      const sc = scope();
      const targets = sc.blocks.length ? sc.blocks : [];
      if (!targets.length) { setStatus("Mark something first"); return; }
      // Boxed already? Then this un-boxes. Reading the first one is how the
      // line-based version decided too (md-checklist.js), so the button keeps
      // behaving the way it reads: one press, one direction.
      const boxed = /^\[[ xX]\]/.test(targets[0].content.trim());
      targets.forEach((b) => o.setText(b, boxed
        ? b.content.replace(/^\s*\[[ xX]\]\s*/, "")
        : `[ ] ${b.content}`));
      await o.rerender(targets);
      refreshScope();
    });
  }

  // ---- clear ----
  if (clearBtn) {
    clearBtn.addEventListener("click", async () => {
      const o = outliner();
      if (!o) return;
      if (!o.isEmpty() && !confirm("Clear the scratchpad? The text is gone.")) return;
      await o.reset();
      setStatus("Cleared");
      refreshScope();
    });
  }

  refreshScope();
})();
