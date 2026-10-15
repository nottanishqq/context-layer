"""Chunking strategies: how a document is cut into retrievable pieces.

Each strategy returns a list of `Chunk`s. They are pure functions over text,
so they can be compared on the same corpus without touching the database.

- `fixed`:    N-token windows with overlap. Simple, ignores structure.
- `sentence`: packs whole sentences up to a token budget. Never cuts mid-thought.
- `section`:  one chunk per Markdown heading section. Follows the author's structure.
- `parent_child`: small `child` chunks for precise retrieval, each linked to the
  larger `parent` section it came from, for generation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Chunk:
    text: str
    position: int
    strategy: str
    heading: str | None = None
    # Only set by parent_child: the index (in the returned list) of the parent chunk.
    parent_index: int | None = None
    # Parents are stored for generation but not embedded or searched.
    searchable: bool = True
    token_count: int = field(init=False)

    def __post_init__(self) -> None:
        self.token_count = count_tokens(self.text)


def count_tokens(text: str) -> int:
    """A cheap token estimate (~0.75 words per token for English)."""
    return max(1, round(len(text.split()) / 0.75))


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`\"'(\[])")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
# A block starting with one of these is a table, list or code: its lines are kept as lines.
_LINE_BLOCK = re.compile(r"^\s*(\||[-*+] |\d+[.)] |```)")


def _units(text: str) -> list[tuple[str, str]]:
    """Split text into (unit, separator) pairs, the separator being what joins a unit
    to the one before it. Prose becomes sentences joined by spaces. Tables, lists and
    code keep one unit per line, joined by newlines, so their rows survive chunking.
    Each block's first unit starts a new paragraph."""
    units: list[tuple[str, str]] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if _LINE_BLOCK.match(lines[0]):
            pieces, sep = lines, "\n"
        else:
            pieces, sep = [s.strip() for s in _SENTENCE_END.split(" ".join(block.split())) if s.strip()], " "
        units.extend((piece, "\n\n" if i == 0 else sep) for i, piece in enumerate(pieces))
    return units


def split_sentences(text: str) -> list[str]:
    """Sentences of prose, and lines of tables, lists and code."""
    return [unit for unit, _ in _units(text)]


def fixed(text: str, size: int = 120, overlap: int = 20) -> list[Chunk]:
    # Each word keeps the whitespace after it, so line breaks in tables and lists survive.
    words = re.findall(r"\S+\s*", text)
    window = max(1, round(size * 0.75))  # tokens -> words
    step = max(1, window - round(overlap * 0.75))
    chunks = []
    for start in range(0, len(words), step):
        piece = words[start : start + window]
        chunks.append(Chunk("".join(piece).strip(), len(chunks), "fixed"))
        if start + window >= len(words):
            break
    return chunks


def sentence(text: str, max_tokens: int = 120) -> list[Chunk]:
    chunks: list[Chunk] = []
    current = ""
    for unit, sep in _units(text):
        # A heading always starts a new chunk, so it stays with the text it introduces.
        is_heading = unit.startswith("#")
        if is_heading:
            unit = unit.lstrip("#").strip()
        joined = f"{current}{sep}{unit}" if current else unit
        if current and (is_heading or count_tokens(joined) > max_tokens):
            chunks.append(Chunk(current, len(chunks), "sentence"))
            current = unit
        else:
            current = joined
    if current:
        chunks.append(Chunk(current, len(chunks), "sentence"))
    return chunks


def sections(text: str) -> list[tuple[str | None, str]]:
    """Split Markdown into (heading path, body) pairs. Text before the first heading has no heading."""
    out: list[tuple[str | None, str]] = []
    stack: list[tuple[int, str]] = []  # (level, title) of the enclosing headings
    matches = list(_HEADING.finditer(text))
    preamble = text[: matches[0].start()] if matches else text
    if preamble.strip():
        out.append((None, preamble.strip()))
    for i, m in enumerate(matches):
        level, title = len(m.group(1)), m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip()
        if body:
            out.append((" > ".join(t for _, t in stack), body))
    return out


def section(text: str, max_tokens: int = 400) -> list[Chunk]:
    """One chunk per heading section; sections over `max_tokens` are split by sentence.

    The split matters: embedding models read a limited input (512 tokens for
    bge-small) and silently ignore the rest, so the end of a long section would
    be invisible to vector search. Each piece keeps its heading.
    """
    chunks: list[Chunk] = []
    for heading, body in sections(text):
        parts = [body] if count_tokens(body) <= max_tokens else [c.text for c in sentence(body, max_tokens)]
        for part in parts:
            chunks.append(Chunk(f"{heading}\n\n{part}" if heading else part, len(chunks), "section", heading=heading))
    return chunks


def parent_child(text: str, child_tokens: int = 60) -> list[Chunk]:
    chunks: list[Chunk] = []
    for heading, body in sections(text):
        parent = Chunk(
            f"{heading}\n\n{body}" if heading else body,
            len(chunks), "parent", heading=heading, searchable=False,
        )
        parent_index = len(chunks)
        chunks.append(parent)
        for child in sentence(body, max_tokens=child_tokens):
            # Prefix the heading so a child chunk still says what it is about.
            prefix = f"{heading}: " if heading else ""
            chunks.append(Chunk(
                prefix + child.text, len(chunks), "child",
                heading=heading, parent_index=parent_index,
            ))
    return chunks


STRATEGIES = {
    "fixed": fixed,
    "sentence": sentence,
    "section": section,
    "parent_child": parent_child,
}


def chunk(text: str, strategy: str = "section") -> list[Chunk]:
    try:
        return STRATEGIES[strategy](text)
    except KeyError:
        raise ValueError(f"Unknown strategy {strategy!r}; choose from {sorted(STRATEGIES)}") from None
