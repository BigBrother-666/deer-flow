"""Pterodactyl panel integration for DeerFlow.

Wraps the Pterodactyl **Client API** as native agent tools so the super agent can
operate existing (Minecraft) game servers: inspect status/resources, read logs and
config files, and perform power/file/backup operations.

All mutating operations (power actions, file writes/deletes, backups, startup vars)
are gated behind a human-in-the-loop hard gate implemented by
``deerflow.community.pterodactyl.guard.PterodactylGuardMiddleware``.
"""
