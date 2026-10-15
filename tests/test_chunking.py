from contextlayer import chunking

DOC = """Intro paragraph about the service.

## Architecture

The service runs six replicas. It writes to Postgres.

### Storage

Entries are immutable.

## Operating it

Page the on-call. Follow the runbook.
"""


def test_sections_follow_heading_levels():
    headings = [h for h, _ in chunking.sections(DOC)]
    assert headings == [None, "Architecture", "Architecture > Storage", "Operating it"]


def test_section_chunks_carry_their_heading():
    chunks = chunking.section(DOC)
    assert chunks[1].text.startswith("Architecture\n\nThe service runs six replicas.")
    assert all(c.strategy == "section" for c in chunks)


def test_fixed_windows_overlap_and_cover_everything():
    text = " ".join(f"w{i}" for i in range(300))
    chunks = chunking.fixed(text, size=40, overlap=8)
    assert chunks[0].text.split()[0] == "w0"
    assert chunks[-1].text.split()[-1] == "w299"
    first, second = chunks[0].text.split(), chunks[1].text.split()
    assert set(first) & set(second), "consecutive windows should overlap"


def test_sentence_chunks_never_split_a_sentence():
    sentences = [s.lstrip("#").strip() for s in chunking.split_sentences(DOC)]
    chunks = chunking.sentence(DOC, max_tokens=12)
    assert len(chunks) > 1
    # Together the chunks cover the text in order...
    assert " ".join(c.text for c in chunks).split() == " ".join(sentences).split()
    # ...every chunk ends on a sentence boundary, never on a dangling heading...
    assert all(c.text.endswith(".") for c in chunks)
    # ...and a heading opens the chunk that holds its text.
    assert any(c.text.startswith("Operating it\n\nPage the on-call.") for c in chunks)


def test_parent_child_links_children_to_unsearchable_parents():
    chunks = chunking.parent_child(DOC, child_tokens=8)
    parents = [c for c in chunks if c.strategy == "parent"]
    children = [c for c in chunks if c.strategy == "child"]
    assert parents and children
    assert not any(p.searchable for p in parents)
    for child in children:
        parent = chunks[child.parent_index]
        assert parent.strategy == "parent"
        assert child.heading == parent.heading


def test_long_sections_are_split_and_keep_their_heading():
    body = " ".join(f"Sentence number {i} explains one more step of the procedure." for i in range(120))
    chunks = chunking.section(f"## Procedure\n\n{body}", max_tokens=100)
    assert len(chunks) > 1
    assert all(c.text.startswith("Procedure\n\n") for c in chunks)
    assert all(chunking.count_tokens(c.text) <= 110 for c in chunks)  # 100 + the heading


TABLE_AND_LIST = """Retention rules follow.

| Data | Retention |
| --- | --- |
| Logs | 14 days |

When paged:

1. Acknowledge the page.
2. Open the runbook.
"""


def test_tables_and_lists_keep_their_lines():
    [one] = chunking.sentence(TABLE_AND_LIST, max_tokens=200)
    assert "| Data | Retention |\n| --- | --- |\n| Logs | 14 days |" in one.text
    assert "1. Acknowledge the page.\n2. Open the runbook." in one.text
    window = chunking.fixed(TABLE_AND_LIST, size=200)[0].text
    assert "| --- | --- |\n| Logs | 14 days |" in window
