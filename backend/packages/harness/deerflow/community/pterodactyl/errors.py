"""Domain errors for the Pterodactyl integration.

These are normalized into recoverable tool-error strings by the tool layer so a
failed API call keeps the run going (handled downstream by
``ToolErrorHandlingMiddleware``) instead of aborting.
"""

from __future__ import annotations


class PterodactylError(Exception):
    """Base class for all Pterodactyl integration errors."""


class PterodactylConfigError(PterodactylError):
    """Raised when required configuration (panel_url / api_key) is missing or invalid."""


class PterodactylAPIError(PterodactylError):
    """Raised when the Pterodactyl API returns a non-success response.

    Attributes:
        status_code: HTTP status code returned by the panel, if any.
        detail: Human-readable detail extracted from the panel error body.
    """

    def __init__(self, message: str, *, status_code: int | None = None, detail: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class PterodactylAuthError(PterodactylAPIError):
    """Raised on 401/403 responses (invalid or insufficient API key)."""


class PterodactylNotFoundError(PterodactylAPIError):
    """Raised on 404 responses (unknown server or file)."""


class PterodactylTimeoutError(PterodactylError):
    """Raised when a request to the panel times out."""
