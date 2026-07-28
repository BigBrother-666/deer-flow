"""Configuration resolution for the Pterodactyl integration.

Shared settings (``panel_url``, ``api_key``, ``timeout``) are read from the
``pterodactyl`` tool *group* config so every tool inherits one source of truth,
falling back to any individual tool's config for overrides.
"""

from __future__ import annotations

from dataclasses import dataclass

from deerflow.config import get_app_config
from deerflow.config.app_config import AppConfig

from .errors import PterodactylConfigError

TOOL_GROUP = "pterodactyl"
DEFAULT_TIMEOUT = 30


@dataclass(frozen=True)
class PterodactylConfig:
    """Resolved connection settings for the Pterodactyl Client API."""

    panel_url: str
    api_key: str
    timeout: int = DEFAULT_TIMEOUT

    @property
    def base_url(self) -> str:
        """Client API root, e.g. ``https://panel.example.com/api/client``."""
        return f"{self.panel_url.rstrip('/')}/api/client"


def _coerce_timeout(value: object) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return parsed if parsed > 0 else DEFAULT_TIMEOUT


def load_config(app_config: AppConfig | None = None) -> PterodactylConfig:
    """Resolve Pterodactyl connection settings from app config.

    Prefers the ``pterodactyl`` tool-group config; individual tool configs may
    still carry the same keys. Raises ``PterodactylConfigError`` when required
    fields are absent so the tool surfaces a clear, recoverable error.
    """
    config = app_config or get_app_config()

    extra: dict[str, object] = {}
    group = config.get_tool_group_config(TOOL_GROUP)
    if group is not None and group.model_extra:
        extra.update(group.model_extra)

    panel_url = extra.get("panel_url")
    api_key = extra.get("api_key")

    if not panel_url or not isinstance(panel_url, str):
        raise PterodactylConfigError("Missing 'panel_url' for the pterodactyl tool group. Set it under tool_groups (or each tool) in config.yaml.")
    if not api_key or not isinstance(api_key, str):
        raise PterodactylConfigError("Missing 'api_key' for the pterodactyl tool group. Provide a Client API key via $PTERODACTYL_API_KEY in config.yaml.")

    return PterodactylConfig(
        panel_url=panel_url,
        api_key=api_key,
        timeout=_coerce_timeout(extra.get("timeout")),
    )
