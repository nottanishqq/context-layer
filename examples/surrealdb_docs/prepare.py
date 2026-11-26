"""Turn the SurrealDB documentation into Markdown files the context layer can ingest.

    uv run python examples/surrealdb_docs/prepare.py                       # download and convert
    uv run python examples/surrealdb_docs/prepare.py --from llms-full.txt  # use a saved copy

SurrealDB publishes its whole documentation as one Markdown file, llms-full.txt: every
page one after another, each starting with a "Source: <url>" line. This script splits it
into one file per page with front matter `ctx ingest` understands, and rewrites links
between pages as [[wikilinks]], so they become links_to edges for graph expansion. It also
saves the release notes index, so "what is the latest version?" has an answer.
"""

from __future__ import annotations

import argparse
import re
import urllib.request
from pathlib import Path

import yaml

DOCS_URL = "https://surrealdb.com/docs/llms-full.txt"
RELEASES_URL = "https://surrealdb.com/releases.md"
OUT = Path(__file__).parent / "pages"

_SOURCE = re.compile(r"^Source: (https://surrealdb\.com/docs\S*)\s*$", re.MULTILINE)
_TITLE = re.compile(r"^# (.+)$", re.MULTILINE)
# [label](/docs/path.md#anchor), with or without the host, .md and anchor
_DOC_LINK = re.compile(r"\[([^\]\n]+)\]\((?:https://surrealdb\.com)?/docs([^)\s#]*?)(?:\.md)?(?:#[^)\s]*)?\)")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


def slug(path: str) -> str:
    """'/build/migrating/2x-to-3x' -> 'build-migrating-2x-to-3x', the shape [[wikilinks]] accept."""
    return re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-") or "home"


def split_pages(full: str) -> list[tuple[str, str]]:
    """(url, markdown) for every page in llms-full.txt."""
    parts = _SOURCE.split(full)  # [preamble, url, body, url, body, ...]
    return [(url, body.strip().removesuffix("---").strip()) for url, body in zip(parts[1::2], parts[2::2])]


def _document(title: str, kind: str, tags: list[str], url: str, body: str) -> str:
    front = yaml.safe_dump({"title": title, "kind": kind, "tags": tags, "url": url},
                           sort_keys=False, allow_unicode=True)
    return f"---\n{front}---\n{_IMAGE.sub('', body).strip()}\n"


def to_document(url: str, body: str) -> tuple[str, str]:
    """(slug, file content) for one docs page."""
    path = url.removeprefix("https://surrealdb.com/docs")
    segments = [s for s in path.split("/") if s]
    title = path or "SurrealDB documentation"
    if m := _TITLE.search(body):
        # The title moves to front matter; ingestion prepends it to every chunk.
        title, body = m.group(1).strip(), body[m.end():]
    body = _DOC_LINK.sub(lambda link: f"[[{slug(link.group(2))}|{link.group(1)}]]", body)
    return slug(path), _document(title, segments[0] if segments else "overview", segments[:2], url, body)


def releases_document(markdown: str) -> str:
    """The release notes index, with its own front matter replaced by ours."""
    return _document("SurrealDB release notes", "releases", ["releases"], "https://surrealdb.com/releases",
                     _FRONT_MATTER.sub("", markdown))


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "contextlayer-example"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert the SurrealDB docs for `ctx ingest`.")
    parser.add_argument("--from", dest="source", help="a saved llms-full.txt instead of downloading it")
    parser.add_argument("--out", type=Path, default=OUT,
                        help=f"output folder (default {OUT}); .md files already in it are replaced")
    args = parser.parse_args()

    full = Path(args.source).read_text(encoding="utf-8") if args.source else fetch(DOCS_URL)
    pages = [to_document(url, body) for url, body in split_pages(full)]
    pages.append(("releases", releases_document(fetch(RELEASES_URL))))

    seen: dict[str, str] = {}
    for name, text in pages:
        if name in seen:
            raise SystemExit(f"Two pages map to {name}.md; adjust slug() so they don't collide.")
        seen[name] = text

    args.out.mkdir(parents=True, exist_ok=True)
    for old in args.out.glob("*.md"):  # pages removed from the docs must not linger
        old.unlink()
    for name, text in seen.items():
        (args.out / f"{name}.md").write_text(text, encoding="utf-8")
    print(f"Wrote {len(seen)} pages to {args.out}")


if __name__ == "__main__":
    main()
