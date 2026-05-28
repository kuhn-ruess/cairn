"""HTML → Markdown conversion used by importers.

Notes in Cairn are stored as Markdown so the textarea editor, search,
and rendering all stay simple. Importers that pull HTML bodies
(Zammad emails, future web ingests) run them through here first.
"""
from markdownify import markdownify as _md


def html_to_markdown(html: str) -> str:
    """Convert an HTML fragment to a clean Markdown string.

    Returns an empty string for falsy input. Tuned for email/article
    bodies: ATX headings, fenced code blocks, no trailing whitespace
    on lines, links inlined.
    """
    if not html:
        return ""
    text = _md(
        html,
        heading_style="ATX",
        code_language_callback=lambda _: "",
        bullets="-",
        strip=["script", "style", "meta", "link"],
    )
    # markdownify can leave runs of blank lines on messy email HTML.
    # Collapse any 3+ blank lines down to one paragraph break.
    out_lines: list[str] = []
    blank = 0
    for line in text.splitlines():
        if not line.strip():
            blank += 1
            if blank <= 1:
                out_lines.append("")
        else:
            blank = 0
            out_lines.append(line.rstrip())
    return "\n".join(out_lines).strip()
