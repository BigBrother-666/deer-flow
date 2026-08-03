"""Tests for local document loading, frontmatter, and tag wiring (design §4.3)."""

from __future__ import annotations

from pathlib import Path

from pterodactyl_rag.loaders import (
    iter_documents,
    load_file,
    split_frontmatter,
)


def test_split_frontmatter_valid() -> None:
    fm, body = split_frontmatter("---\nplugin: EssentialsX\ntags: [mc:1.21]\n---\n# Title\nbody")
    assert fm == {"plugin": "EssentialsX", "tags": ["mc:1.21"]}
    assert body.startswith("# Title")


def test_split_frontmatter_absent() -> None:
    fm, body = split_frontmatter("# No frontmatter\ntext")
    assert fm == {}
    assert body == "# No frontmatter\ntext"


def test_split_frontmatter_invalid_yaml_ignored() -> None:
    text = "---\n: : bad\n---\nbody"
    fm, body = split_frontmatter(text)
    assert fm == {}
    assert body == text


def test_load_markdown_infers_tags_and_title(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    f = docs / "EssentialsX" / "config" / "kits.md"
    f.parent.mkdir(parents=True)
    f.write_text("# Kits\nHow to configure kits.\n", encoding="utf-8")

    loaded = load_file(f, docs)
    assert loaded is not None
    assert loaded.is_markdown
    assert loaded.document.source_path == "EssentialsX/config/kits.md"
    assert loaded.document.title == "Kits"
    assert "plugin:essentialsx" in loaded.document.tags
    assert "category:config" in loaded.document.tags
    assert loaded.document.content_hash


def test_frontmatter_plugin_overrides_path(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    f = docs / "essx" / "config" / "x.md"
    f.parent.mkdir(parents=True)
    f.write_text("---\nplugin: EssentialsX\ntags: [platform:paper]\n---\nbody", encoding="utf-8")

    loaded = load_file(f, docs)
    assert loaded is not None
    assert "plugin:essentialsx" in loaded.document.tags
    assert "platform:paper" in loaded.document.tags
    assert sum(t.startswith("plugin:") for t in loaded.document.tags) == 1


def test_sidecar_tags_merged(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    d = docs / "WorldGuard" / "config"
    d.mkdir(parents=True)
    (d / ".rag.yaml").write_text("tags: [platform:spigot]\n", encoding="utf-8")
    f = d / "regions.md"
    f.write_text("# Regions\ntext", encoding="utf-8")

    loaded = load_file(f, docs)
    assert loaded is not None
    assert "platform:spigot" in loaded.document.tags
    assert "plugin:worldguard" in loaded.document.tags


def test_txt_file_no_frontmatter(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    f = docs / "Plug" / "notes.txt"
    f.parent.mkdir(parents=True)
    f.write_text("plain notes", encoding="utf-8")
    loaded = load_file(f, docs)
    assert loaded is not None
    assert not loaded.is_markdown
    assert loaded.text == "plain notes"


def test_unsupported_extension_skipped(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    f = docs / "x.png"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"\x89PNG")
    assert load_file(f, docs) is None


def test_iter_documents_skips_hidden_and_unsupported(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    (docs / "A").mkdir(parents=True)
    (docs / "A" / "good.md").write_text("# G\nx", encoding="utf-8")
    (docs / "A" / "skip.png").write_bytes(b"x")
    (docs / ".hidden").mkdir()
    (docs / ".hidden" / "secret.md").write_text("# S\nx", encoding="utf-8")

    paths = {ld.document.source_path for ld in iter_documents(docs)}
    assert paths == {"A/good.md"}
