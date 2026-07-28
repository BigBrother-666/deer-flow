"""Meta-tests: mutation registry must cover all non-read-only tools."""

from deerflow.community.pterodactyl import mutations, tools

# Tools that are intentionally read-only and must NOT require confirmation.
READ_ONLY_TOOLS = {
    "pterodactyl_list_servers",
    "pterodactyl_get_server",
    "pterodactyl_get_resources",
    "pterodactyl_list_files",
    "pterodactyl_read_file",
    "pterodactyl_list_backups",
    "pterodactyl_get_startup",
}


def _all_tool_names() -> set[str]:
    from langchain_core.tools import BaseTool

    names = set()
    for obj in vars(tools).values():
        if isinstance(obj, BaseTool) and obj.name.startswith("pterodactyl_"):
            names.add(obj.name)
    return names


def test_every_tool_is_classified():
    """No pterodactyl tool may be neither read-only nor registered as mutating."""
    all_names = _all_tool_names()
    classified = READ_ONLY_TOOLS | set(mutations.MUTATING_TOOLS)
    unclassified = all_names - classified
    assert not unclassified, f"Unclassified pterodactyl tools (HITL bypass risk): {unclassified}"


def test_read_only_and_mutating_are_disjoint():
    assert not (READ_ONLY_TOOLS & set(mutations.MUTATING_TOOLS))


def test_high_risk_is_subset_of_mutating():
    assert mutations.HIGH_RISK_TOOLS <= mutations.MUTATING_TOOLS


def test_confirmation_token_binds_to_operation():
    t1 = mutations.confirmation_token("pterodactyl_power_action", {"server_id": "a", "signal": "restart"})
    t2 = mutations.confirmation_token("pterodactyl_power_action", {"server_id": "a", "signal": "kill"})
    t3 = mutations.confirmation_token("pterodactyl_delete_file", {"server_id": "a"})
    assert t1 != t2 != t3
    # Deterministic and argument-order independent.
    same = mutations.confirmation_token("pterodactyl_power_action", {"signal": "restart", "server_id": "a"})
    assert t1 == same
