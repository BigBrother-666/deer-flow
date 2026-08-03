"""Tests for namespaced tag resolution + normalization (design §4.2.1)."""

from __future__ import annotations

from pterodactyl_rag.tags import (
    MAX_TAGS_PER_DOC,
    infer_tags_from_path,
    normalize_tag,
    normalize_tags,
    normalize_value,
    plugin_of,
    resolve_tags,
)


def test_normalize_value_collapses_variants() -> None:
    assert normalize_value("EssentialsX") == "essentialsx"
    assert normalize_value("essentials x") == "essentials-x"
    assert normalize_value("Essentials-X") == "essentials-x"
    assert normalize_value("  Luck__Perms  ") == "luck__perms"
    assert normalize_value("!!!") == ""


def test_normalize_tag_namespaced() -> None:
    assert normalize_tag("Plugin: EssentialsX") == "plugin:essentialsx"
    assert normalize_tag("MC:1.21") == "mc:1.21"
    # empty namespace or value -> dropped
    assert normalize_tag(":x") == ""
    assert normalize_tag("plugin:") == ""


def test_normalize_tag_bare() -> None:
    assert normalize_tag("Economy") == "economy"


def test_normalize_tags_dedupes_stably_and_caps() -> None:
    tags = ["plugin:EssentialsX", "plugin: essentialsx ", "economy", "economy"]
    # first two normalize to the SAME plugin tag; dedupe keeps one
    assert normalize_tags(tags) == ["plugin:essentialsx", "economy"]

    many = [f"t{i}" for i in range(MAX_TAGS_PER_DOC + 10)]
    assert len(normalize_tags(many)) == MAX_TAGS_PER_DOC


def test_infer_tags_from_path() -> None:
    assert infer_tags_from_path("EssentialsX/config/perms.md") == [
        "plugin:essentialsx",
        "category:config",
    ]
    # second segment not a known category -> only plugin
    assert infer_tags_from_path("WorldGuard/misc/notes.md") == ["plugin:worldguard"]
    # bare filename -> nothing
    assert infer_tags_from_path("readme.md") == []


def test_resolve_tags_frontmatter_appends() -> None:
    tags = resolve_tags(
        "EssentialsX/config/x.md",
        {"tags": ["platform:Paper", "mc:1.21"]},
    )
    assert tags == [
        "plugin:essentialsx",
        "category:config",
        "platform:paper",
        "mc:1.21",
    ]


def test_resolve_tags_explicit_plugin_overrides_path() -> None:
    tags = resolve_tags(
        "essx/config/x.md",  # folder mis-named
        {"plugin": "EssentialsX"},
    )
    assert plugin_of(tags) == "essentialsx"
    # category from path is preserved
    assert "category:config" in tags
    # only one plugin tag
    assert sum(t.startswith("plugin:") for t in tags) == 1


def test_resolve_tags_string_frontmatter_tags() -> None:
    tags = resolve_tags("A/faq/x.md", {"tags": "Economy"})
    assert "economy" in tags


def test_plugin_of() -> None:
    assert plugin_of(["category:config", "plugin:foo"]) == "foo"
    assert plugin_of(["category:config"]) is None
