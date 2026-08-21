"""The note body as a tree of blocks — Logseq's outline, in Markdown.

A note is still ONE Markdown string (``Item.note``). This module is the only
place that knows it is really an outline, and it does so in Logseq's own file
format, so the mirrored ``.md`` file stays a plain, portable document::

    - A block
    	- A child block
    	  a second line of the same block
    	  id:: 0193f2a1-6c2e-7a10-9f1b-2c4d5e6f7a8b
    	  collapsed:: true
    		- A grandchild
    - TODO Another block

Three rules, and that is the whole format:

* one tab per level, in front of the ``- `` bullet;
* a block's further lines sit two spaces past its own bullet indent;
* such a line of the form ``key:: value`` is a PROPERTY of the block, not text.

Keeping the text authoritative is what lets the WebDAV mirror, the wiki links,
search and ``services/capture`` carry on untouched — they all work on the string.

Two parsers hide behind :func:`parse`:

* **strict** — the file already starts with a bullet, so it was written by us.
  Reading it back gives exactly the tree that produced it.
* **loose** — anything else, i.e. every note written before the outline existed.
  Paragraphs become top-level blocks, headings blocks of their own, existing
  Markdown lists become nested blocks, fenced code stays in one piece. Nothing
  is converted in the database: a legacy note simply READS as an outline, and
  only the first edit writes it back in strict form.
"""
import re
import uuid as _uuid
from dataclasses import dataclass, field

# One tab per level; a block's continuation lines sit two spaces past it.
INDENT = "\t"
CONT = "  "

# ``\t\t- text``. The bullet is the only thing that starts a block.
BULLET_RE = re.compile(r"^(\t*)-(?: (.*))?$")
# ``key:: value`` on a line of its own — a block property.
PROP_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_-]*):: ?(.*)$")
# ``` or ~~~, with any indent. Fenced code must survive both parsers intact.
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^#{1,6} ")
# A Markdown list item in a legacy note (spaces OR tabs, "-", "*", "+", "1.").
LOOSE_ITEM_RE = re.compile(r"^([ \t]*)(?:[-*+]|\d+[.)])[ \t]+(.*)$")

# The three tells :func:`is_outline` reads. We indent with tabs and write
# properties two spaces past the bullet; a human indents a list with spaces.
NESTED_BULLET_RE = re.compile(r"^\t+-(?: |$)", re.M)
BLOCK_PROP_RE = re.compile(r"^\t*  [A-Za-z0-9_][A-Za-z0-9_-]*:: ", re.M)
SPACED_ITEM_RE = re.compile(r"^ +(?:[-*+]|\d+[.)])[ \t]", re.M)

# A list marker at the head of a line, for :func:`parse_lines` — the bullet the
# outline draws replaces it.
LIST_MARKER_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+")

# Logseq's task keywords, in the order Ctrl+Enter cycles them.
KEYWORDS = ("TODO", "DOING", "DONE")
KEYWORD_RE = re.compile(r"^(TODO|DOING|DONE|LATER|NOW|WAITING|CANCELED)\b ?")

# A Markdown checkbox at the head of a block. This app's notes are full of
# "- [ ] …" lists, and in an outline that line's dash IS the bullet — leaving
# "[ ] …" as the block's text, which Markdown renders as three literal
# characters. So the box is read off the block instead, which keeps the FILE a
# perfectly ordinary Markdown checklist while the app still renders a checkbox.
CHECKBOX_RE = re.compile(r"^\[([ xX])\] ?")


@dataclass
class Block:
    """One bullet: its own text, its properties, its children."""
    content: str = ""
    props: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    # Position in document order, assigned by :func:`parse`. This is how the
    # page addresses a block that has no ``id::`` yet.
    idx: int = 0

    @property
    def uuid(self) -> str:
        return self.props.get("id", "")

    @property
    def collapsed(self) -> bool:
        return str(self.props.get("collapsed", "")).lower() == "true"

    @property
    def keyword(self) -> str:
        """``TODO``/``DOING``/… if the block starts with one, else ``""``."""
        m = KEYWORD_RE.match(self.content or "")
        return m.group(1) if m else ""

    @property
    def checked(self):
        """``True``/``False`` for a checklist block, ``None`` when it isn't one."""
        m = CHECKBOX_RE.match(KEYWORD_RE.sub("", self.content or "", count=1))
        return m.group(1).lower() == "x" if m else None

    @property
    def body(self) -> str:
        """The content without the two things drawn beside it rather than in
        it — the task keyword and the checkbox. What actually gets rendered."""
        text = KEYWORD_RE.sub("", self.content or "", count=1)
        return CHECKBOX_RE.sub("", text, count=1)


# ---------------------------------------------------------------------------
# Serialize
# ---------------------------------------------------------------------------

def serialize(blocks) -> str:
    """The tree back to Markdown. Inverse of the strict parser.

    An empty line INSIDE a block is written as the block's continuation indent
    and nothing else, rather than as a truly blank line — a blank line is how
    blocks are separated in a hand-edited file, and the two must not collide.
    """
    out = []

    def emit(block, depth):
        pad = INDENT * depth
        lines = (block.content or "").split("\n")
        out.append(f"{pad}- {lines[0]}" if lines[0] else f"{pad}-")
        for line in lines[1:]:
            out.append(f"{pad}{CONT}{line}")
        for key, value in block.props.items():
            out.append(f"{pad}{CONT}{key}:: {value}")
        for child in block.children:
            emit(child, depth + 1)

    for block in blocks or []:
        emit(block, 0)
    return "\n".join(out) + ("\n" if out else "")


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def is_outline(text: str) -> bool:
    """Was this body written by the outliner, or by hand in a Markdown editor?

    Getting this wrong is not cosmetic: read a hand-written ``- a\\n  - b`` as
    strict and the sub-item is swallowed as a further LINE of its parent rather
    than becoming a child block. So the tells are checked in order of how much
    they prove:

    * a tab-indented bullet or a block property — only we write those;
    * a SPACE-indented list item — only a human writes those;
    * otherwise, a leading bullet is enough.

    The one genuinely ambiguous body is a flat outline holding a space-indented
    list inside a single block. It reads as nesting on the next load, which is
    what the person who typed it meant, and from then on it carries tabs and is
    stable. It converges after one step and never loses text.
    """
    text = text or ""
    if NESTED_BULLET_RE.search(text) or BLOCK_PROP_RE.search(text):
        return True
    if SPACED_ITEM_RE.search(text):
        return False
    for line in text.split("\n"):
        if line.strip():
            return bool(BULLET_RE.match(line))
    return False


def parse(text: str) -> list:
    """The body as a list of top-level :class:`Block`, ``idx`` filled in."""
    return _number(_strict(text) if is_outline(text) else _loose(text))


def parse_lines(text: str) -> list:
    """:func:`parse` for text whose LINES were always items, not prose.

    The braindump pad is the case: a line there is a task-to-be, and four
    spaces in front of it meant "sub-task" — which is exactly how
    ``services/capture.parse_tasks`` still reads it. Running the note parser
    over that would glue a whole page of items into one paragraph, because to a
    note four lines with no blank line between them ARE one paragraph.

    Text we already wrote takes the strict path, same as everywhere else.
    """
    return _number(_strict(text) if is_outline(text) else _lines(text))


def _number(blocks) -> list:
    for i, block in enumerate(walk(blocks)):
        block.idx = i
    return blocks


def _split(text: str) -> list:
    """Strict pass one: the raw lines belonging to each block, bullet-stripped.

    Returns ``[(depth, [line, ...])]`` in document order. Fenced code is the
    reason this is a pass of its own — inside a fence a ``- `` line is code,
    not a new block, so the fence state has to be tracked while splitting.
    """
    out = []
    fence = None          # the open fence marker, or None
    pad = ""              # indent of the block currently being filled

    def track(line):
        """Open or close a code fence on ``line``."""
        nonlocal fence
        m = FENCE_RE.match(line)
        if not m:
            return
        mark = m.group(1)[0] * 3
        if fence is None:
            fence = mark
        elif mark == fence:
            fence = None

    for raw in (text or "").split("\n"):
        m = None if fence else BULLET_RE.match(raw)
        if m:
            pad = m.group(1)
            line = m.group(2) or ""
            out.append((len(pad), [line]))
            track(line)
            continue
        if not out:
            # Text ahead of the first bullet: give it a block to live in rather
            # than dropping it on the floor.
            out.append((0, []))
            pad = ""
        prefix = pad + CONT
        if raw.startswith(prefix):
            line = raw[len(prefix):]
        elif fence:
            line = raw            # inside code, never touch what we can't map
        else:
            line = raw.strip()
        out[-1][1].append(line)
        track(line)
    return out


def _finish(lines) -> tuple:
    """One block's lines → ``(content, props)``.

    Properties are collected wherever they appear but always written back at
    the end (see :func:`serialize`), so a hand-edited file gets normalised on
    its next save. Fenced code is skipped, or a ``key:: value`` in a snippet
    would silently become a property.
    """
    content, props = [], {}
    fence = None
    for line in lines:
        m = FENCE_RE.match(line)
        if m:
            mark = m.group(1)[0] * 3
            fence = None if fence == mark else (fence or mark)
            content.append(line)
            continue
        pm = None if fence else PROP_RE.match(line)
        if pm:
            props[pm.group(1)] = pm.group(2).strip()
            continue
        content.append(line)
    while content and not content[-1].strip():
        content.pop()          # trailing blanks are block separators
    return "\n".join(content), props


def _assemble(records) -> list:
    """``[(depth, lines)]`` → a tree. A depth that jumps by more than one is
    clamped, so a hand-indented file can't produce an orphan."""
    roots = []
    stack = [roots]            # stack[d] is the child list at depth d
    for depth, lines in records:
        depth = min(depth, len(stack) - 1)
        content, props = _finish(lines)
        block = Block(content=content, props=props)
        stack[depth].append(block)
        del stack[depth + 1:]
        stack.append(block.children)
    return roots


def _strict(text: str) -> list:
    return _assemble(_split(text))


def _indent_of(s: str) -> int:
    """Visual width of a legacy list item's indent (tabs count as four)."""
    return len(s.replace("\t", "    "))


def _loose(text: str) -> list:
    """Pass for notes written before the outline existed.

    Paragraphs, headings and fenced code become top-level blocks; an existing
    Markdown list becomes nested blocks, with a list item's own continuation
    lines staying part of that item.
    """
    records = []
    lines = (text or "").split("\n")
    stack = []                 # [(indent, depth)] of the enclosing list items
    buf, buf_depth = [], 0

    def flush():
        nonlocal buf
        while buf and not buf[-1].strip():
            buf.pop()
        if buf:
            records.append((buf_depth, buf))
        buf = []

    i = 0
    while i < len(lines):
        raw = lines[i]

        # Fenced code: swallow whole, at the depth of the list item it sits in.
        fm = FENCE_RE.match(raw)
        if fm:
            mark = fm.group(1)[0] * 3
            base = len(raw) - len(raw.lstrip())
            block = [raw.strip()]
            i += 1
            while i < len(lines):
                line = lines[i]
                block.append(line[base:] if line[:base].strip() == "" else line.strip())
                i += 1
                if FENCE_RE.match(line) and line.strip().startswith(mark):
                    break
            if buf:
                buf.extend(block)          # code inside the current paragraph
            else:
                records.append((stack[-1][1] if stack else 0, block))
            continue

        if not raw.strip():
            flush()
            i += 1
            continue

        if HEADING_RE.match(raw.strip()):
            flush()
            stack = []
            records.append((0, [raw.strip()]))
            i += 1
            continue

        item = LOOSE_ITEM_RE.match(raw)
        if item:
            flush()
            indent = _indent_of(item.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            depth = (stack[-1][1] + 1) if stack else 0
            stack.append((indent, depth))
            buf, buf_depth = [item.group(2)], depth
            i += 1
            continue

        # Plain text. Inside an open block it is a further line of that block;
        # otherwise it starts a paragraph, and a paragraph ends the list.
        if buf:
            buf.append(raw.strip())
        else:
            stack = []
            buf, buf_depth = [raw.strip()], 0
        i += 1

    flush()
    return _assemble(records)


def _lines(text: str) -> list:
    """One block per non-blank line, nested by how far it is indented.

    Indent widths are taken the way ``capture.parse_tasks`` takes them (a tab
    is four), so what the pad showed as a sub-task stays one.
    """
    records = []
    stack = []                 # [(indent width, depth)] of the open ancestors
    for raw in (text or "").split("\n"):
        if not raw.strip():
            continue
        flat = raw.expandtabs(4)
        indent = len(flat) - len(flat.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        depth = (stack[-1][1] + 1) if stack else 0
        stack.append((indent, depth))
        # The list marker becomes the block's own bullet; a checkbox stays,
        # because that is content the block still carries.
        records.append((depth, [LIST_MARKER_RE.sub("", raw.strip(), count=1)]))
    return _assemble(records)


# ---------------------------------------------------------------------------
# Walking
# ---------------------------------------------------------------------------

def walk(blocks):
    """Every block, depth-first, in document order."""
    for block in blocks or []:
        yield block
        yield from walk(block.children)


def find(blocks, uuid: str):
    """The block carrying ``id:: uuid``, or None."""
    if not uuid:
        return None
    for block in walk(blocks):
        if block.uuid == uuid:
            return block
    return None


def path_to(blocks, uuid: str) -> list:
    """The chain of blocks from the top down to ``uuid`` (last one included),
    or ``[]`` if it isn't here. Drives the zoom breadcrumb."""
    def descend(nodes, trail):
        for block in nodes:
            here = trail + [block]
            if block.uuid == uuid:
                return here
            hit = descend(block.children, here)
            if hit:
                return hit
        return []
    return descend(blocks or [], []) if uuid else []


def by_idx(blocks, idx: int):
    """The block at document position ``idx``, or None."""
    for block in walk(blocks):
        if block.idx == idx:
            return block
    return None


def ensure_id(block) -> str:
    """This block's uuid, minting one the first time it is actually needed.

    Ids are handed out lazily on purpose: a note nobody references stays a
    clean Markdown file with no machine noise in it.
    """
    if not block.props.get("id"):
        block.props["id"] = str(_uuid.uuid4())
    return block.props["id"]


def collect_ids(blocks) -> list:
    """Every ``id::`` in the tree — what ``Item.block_ids`` stores."""
    return [b.uuid for b in walk(blocks) if b.uuid]


def plain(text: str) -> str:
    """The body as flat prose: bullets and properties gone, blocks joined.

    Used by the list previews and search snippets, which must not show a note's
    ``id::`` lines back to the reader.
    """
    return "\n".join(b.content for b in walk(parse(text)) if b.content.strip())
