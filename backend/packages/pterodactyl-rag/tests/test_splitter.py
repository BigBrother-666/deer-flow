"""Tests for token/heading-aware chunking (design §4.3)."""

from __future__ import annotations

from pterodactyl_rag.splitter import split_document

MD = """\
Intro paragraph before any heading.

# Config
Top-level config text.

## Permissions
Permission node docs here.

## Commands
Command docs here.
"""


def test_markdown_heading_paths() -> None:
    chunks = split_document(MD, is_markdown=True, max_tokens=1000, overlap=50)
    paths = [c.heading_path for c in chunks]
    assert None in paths  # intro before first heading
    assert "Config" in paths
    assert "Config > Permissions" in paths
    assert "Config > Commands" in paths


def test_chunk_indices_are_sequential() -> None:
    chunks = split_document(MD, is_markdown=True, max_tokens=1000, overlap=50)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_tags_copied_onto_every_chunk() -> None:
    tags = ["plugin:essentialsx", "category:config"]
    chunks = split_document(MD, is_markdown=True, max_tokens=1000, overlap=50, tags=tags)
    assert chunks
    assert all(c.tags == tags for c in chunks)


def test_oversized_section_is_split_with_overlap() -> None:
    big = "# Big\n" + "word " * 500
    chunks = split_document(big, is_markdown=True, max_tokens=100, overlap=20)
    # a 500-word section at 100 tokens/chunk must produce multiple chunks
    assert len(chunks) > 1
    assert all(c.heading_path == "Big" for c in chunks)
    assert all((c.token_count or 0) <= 100 for c in chunks)


def test_plain_text_single_section() -> None:
    chunks = split_document("just some plain text\n\nsecond para", is_markdown=False, max_tokens=1000, overlap=50)
    assert len(chunks) == 1
    assert chunks[0].heading_path is None


def test_empty_text_yields_no_chunks() -> None:
    assert split_document("   \n  \n", is_markdown=True, max_tokens=100, overlap=10) == []


def test_deeper_heading_resets_breadcrumb() -> None:
    md = "# A\ntext a\n## B\ntext b\n# C\ntext c\n"
    chunks = split_document(md, is_markdown=True, max_tokens=1000, overlap=10)
    paths = [c.heading_path for c in chunks]
    # after returning to level-1 "C", breadcrumb should not still contain "B"
    assert "C" in paths
    assert "A > B" in paths
    assert "A > B > C" not in paths
