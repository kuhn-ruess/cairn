"""HTML → Markdown conversion used by importers.

Notes in Cairn are stored as Markdown so the textarea editor, search,
and rendering all stay simple. Importers that pull HTML bodies
(Zammad emails, future web ingests) run them through here first.
"""
from markdownify import markdownify as _md


def _unwrap_layout_tables(html: str) -> str:
    """Drop the grid from tables that are only there for layout.

    An HTML email signature is built from nested ``<table>``s, and markdownify
    turns every one of them into Markdown table syntax. Nested, that collapses
    into a wall of ``| | --- | |`` on a single line — which no Markdown parser
    reads as a table either, so it reaches the reader as literal pipes. A table
    that carries no ``<th>`` of its own is one of those: unwrap it and keep the
    text, one row per line. A real data table has a header row and is left
    alone, pipes and all.

    Innermost first, so a layout table wrapping a data table doesn't take the
    real one apart with it.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return html
    def own(table, names):
        """The ``names`` elements belonging to THIS table — ``find_all``
        descends into nested tables, and those rows are not ours to take
        apart."""
        return [el for el in table.find_all(names)
                if el.find_parent("table") is table]

    for table in reversed(tables):
        if own(table, "th"):
            continue
        for cell in own(table, ["td", "th"]):
            cell.insert_after(" ")
            cell.unwrap()
        for row in own(table, "tr"):
            row.insert_after(soup.new_tag("br"))
            row.unwrap()
        for part in own(table, ["thead", "tbody", "tfoot", "caption",
                                "colgroup", "col"]):
            part.unwrap()
        table.unwrap()
    return str(soup)


def html_to_markdown(html: str) -> str:
    """Convert an HTML fragment to a clean Markdown string.

    Returns an empty string for falsy input. Tuned for email/article
    bodies: ATX headings, fenced code blocks, no trailing whitespace
    on lines, links inlined, and layout tables flattened
    (see :func:`_unwrap_layout_tables`).
    """
    if not html:
        return ""
    text = _md(
        _unwrap_layout_tables(html),
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
