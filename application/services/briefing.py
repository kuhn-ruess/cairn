"""Pull the "briefing" section out of a note's Markdown body.

A note linked to a calendar event is usually a mixed bag — pasted mails,
links, minutes taken during the meeting. Only a marked-off part of it is
worth showing on the event in the Today view, so the user marks it with a
heading:

    # Meeting with ACME

    pasted mail thread …

    ## Briefing
    - ask about the May invoice
    - deadline for the migration?

    ## Minutes
    …

Everything under that heading up to the next heading of the same or a
higher level is the briefing. Deeper sub-headings stay part of it.
"""
import re

# Heading text that opens a briefing section (compared lower-cased, with
# trailing colon and surrounding emphasis markers stripped).
BRIEFING_TITLES = {"briefing", "vorbereitung"}

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def _norm(title: str) -> str:
    """Heading text → comparable key ("## **Briefing:**" → "briefing")."""
    return re.sub(r"^[\s#*_]+|[\s#*_:]+$", "", title).lower()


def extract_briefing(markdown: str) -> str:
    """Return the briefing section of ``markdown``, or "" when there is none."""
    if not markdown or "#" not in markdown:
        return ""

    level = None          # heading level that opened the briefing
    in_fence = False      # inside a ``` block — no headings in there
    out = []

    for line in markdown.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        else:
            heading = _HEADING_RE.match(line) if not in_fence else None
            if heading:
                hlevel = len(heading.group(1))
                if level is None:
                    if _norm(heading.group(2)) in BRIEFING_TITLES:
                        level = hlevel
                    continue
                if hlevel <= level:
                    break
        if level is not None:
            out.append(line)

    return "\n".join(out).strip()
