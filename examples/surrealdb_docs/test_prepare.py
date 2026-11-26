from pathlib import Path

from contextlayer.ingest import parse_markdown

from prepare import releases_document, split_pages, to_document

FULL = """# SurrealDB documentation - full corpus

> Preamble that is not a page.

---

Source: https://surrealdb.com/docs/build/migrating/from-old-surrealdb-versions/overview

# Upgrading

See the [2.x to 3.x guide](/docs/build/migrating/from-old-surrealdb-versions/2x-to-3x.md#v3-compatible-export)
and the [release notes](https://surrealdb.com/releases). ![diagram](/img/x.png)

---

Source: https://surrealdb.com/docs

# Getting started

Welcome.
"""


def test_split_and_convert_round_trip_through_ingest(tmp_path: Path):
    pages = split_pages(FULL)
    assert [url for url, _ in pages] == [
        "https://surrealdb.com/docs/build/migrating/from-old-surrealdb-versions/overview",
        "https://surrealdb.com/docs",
    ]
    assert not pages[0][1].endswith("---")

    name, text = to_document(*pages[0])
    assert name == "build-migrating-from-old-surrealdb-versions-overview"
    (tmp_path / f"{name}.md").write_text(text, encoding="utf-8")
    doc = parse_markdown(tmp_path / f"{name}.md")

    assert doc.title == "Upgrading" and doc.kind == "build"
    assert doc.metadata["url"] == pages[0][0]
    # Doc links become graph edges; external links and images don't.
    assert doc.links == [("build-migrating-from-old-surrealdb-versions-2x-to-3x", "2.x to 3.x guide")]
    assert "[release notes](https://surrealdb.com/releases)" in doc.content
    assert "![" not in doc.content and "# Upgrading" not in doc.content

    assert to_document(*pages[1])[0] == "home"


def test_releases_page_gets_our_front_matter(tmp_path: Path):
    text = releases_document("---\ntitle: x\nurl: y\n---\n\n# Releases\n\n![Logo](a.svg)\n[Latest stable: 3.3](z)\n")
    (tmp_path / "releases.md").write_text(text, encoding="utf-8")
    doc = parse_markdown(tmp_path / "releases.md")
    assert doc.kind == "releases" and doc.title == "SurrealDB release notes"
    assert "Latest stable: 3.3" in doc.content and "Logo" not in doc.content
