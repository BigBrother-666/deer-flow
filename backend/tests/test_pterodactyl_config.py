"""Unit tests for pterodactyl config resolution."""

from types import SimpleNamespace

import pytest

from deerflow.community.pterodactyl.config import DEFAULT_TIMEOUT, load_config
from deerflow.community.pterodactyl.errors import PterodactylConfigError


def _fake_config(extra: dict | None):
    group = SimpleNamespace(model_extra=extra) if extra is not None else None
    return SimpleNamespace(get_tool_group_config=lambda name: group if name == "pterodactyl" else None)


def test_load_config_resolves_group_settings():
    cfg = load_config(_fake_config({"panel_url": "https://p.example.com/", "api_key": "ptlc_x", "timeout": 15}))
    assert cfg.panel_url == "https://p.example.com/"
    assert cfg.api_key == "ptlc_x"
    assert cfg.timeout == 15
    assert cfg.base_url == "https://p.example.com/api/client"


def test_base_url_strips_trailing_slash():
    cfg = load_config(_fake_config({"panel_url": "https://p.example.com///", "api_key": "k"}))
    assert cfg.base_url == "https://p.example.com/api/client"


def test_timeout_defaults_when_invalid():
    cfg = load_config(_fake_config({"panel_url": "https://p", "api_key": "k", "timeout": "nope"}))
    assert cfg.timeout == DEFAULT_TIMEOUT


def test_missing_panel_url_raises():
    with pytest.raises(PterodactylConfigError, match="panel_url"):
        load_config(_fake_config({"api_key": "k"}))


def test_missing_api_key_raises():
    with pytest.raises(PterodactylConfigError, match="api_key"):
        load_config(_fake_config({"panel_url": "https://p"}))


def test_missing_group_raises():
    with pytest.raises(PterodactylConfigError):
        load_config(_fake_config(None))
