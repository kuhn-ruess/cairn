"""Ticking a ``- [ ]`` line straight from a rendered Markdown body.

The renderer turns those markers into real checkboxes and numbers them by their
order in the source (``views/filters._checkboxes``); this flips the nth one back
in the Markdown without parsing the document. Shared by every body that offers
tickable boxes — standalone notes and the note field on tasks/projects — so the
numbering can't drift between them.
"""
import re

# The nth ``- [ ]`` / ``- [x]`` line, in the same order the renderer numbers
# its checkboxes.
TASK_LINE_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)\[([ xX])\]", re.M)


def toggle(text: str, index: int):
    """Flip checkbox ``index`` in ``text``.

    Returns ``(new_text, checked)`` — ``checked`` being the state it now has —
    or ``None`` when there is no such checkbox.
    """
    matches = list(TASK_LINE_RE.finditer(text or ""))
    if not 0 <= index < len(matches):
        return None
    m = matches[index]
    was_checked = m.group(2).lower() == "x"
    new_text = (text[:m.start()] + m.group(1)
                + ("[ ]" if was_checked else "[x]") + text[m.end():])
    return new_text, not was_checked
