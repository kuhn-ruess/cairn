/* Markdown checklist lines — shared by the scratchpad (a plain textarea)
   and the note editor (CodeMirror), so the two can't drift apart on what
   "- [ ]" means. Only the pure line transform lives here; each editor applies
   it to its own selection. (Not to be confused with checklist.js, which drives
   the task inspector's stored checklist.)

     CairnMdChecklist.toggleLines(["a", "- b"])  →  ["- [ ] a", "- [ ] b"] */
(function () {
  // A bullet ("- ", "* ", "1. ") and a bullet that already carries a box.
  const BULLET = /^(\s*)(?:[-*+]|\d+[.)])\s+/;
  const BOX = /^(\s*)([-*+]|\d+[.)])\s+\[[ xX]\]\s+/;

  function toggleLines(lines) {
    // A block that is already all boxes goes back to plain bullets; anything
    // else becomes a checklist. A mixed selection therefore completes to a
    // checklist first, which is the move people mean by it.
    const real = lines.filter((l) => l.trim());
    const allBoxes = real.length > 0 && real.every((l) => BOX.test(l));
    return lines.map((line) => {
      if (!line.trim()) return line;
      if (allBoxes) return line.replace(BOX, "$1$2 ");
      if (BOX.test(line)) return line;
      if (BULLET.test(line)) return line.replace(BULLET, "$&[ ] ");
      const indent = line.match(/^\s*/)[0];
      return indent + "- [ ] " + line.slice(indent.length);
    });
  }

  window.CairnMdChecklist = { toggleLines, BULLET, BOX };
})();
