"""RAG retrieval over Pterodactyl/Minecraft plugin documentation.

A standalone package (independent of ``deerflow-harness``) that ingests local
plugin documentation (split -> embed -> index into pgvector) and serves
retrieval tools over MCP so the DeerFlow Pterodactyl agent can look up plugin
config keys, permission nodes, and commands from authoritative docs.

See ``docs/plans/2026-07-29-pterodactyl-rag-mcp-design.md`` for the full design.
"""

from __future__ import annotations

__version__ = "0.1.0"
