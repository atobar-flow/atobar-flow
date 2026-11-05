"""Compatibility re-export of client_templates from atobar_contracts."""

from atobar_contracts.client_templates import (
    CONTROL_LABELS,
    DEFAULT_CP_HOSTNAME,
    DEFAULT_CP_URL,
    HEADER,
    INIT_REPO,
    ONBOARD_COMMAND,
    ONBOARD_SCRIPT_URL,
    onboard_command,
    onboard_script_url,
    render_onboard_script,
    render_trigger,
    resolve_cp_hostname,
    resolve_cp_url,
)

__all__ = [
    "CONTROL_LABELS",
    "DEFAULT_CP_HOSTNAME",
    "DEFAULT_CP_URL",
    "HEADER",
    "INIT_REPO",
    "ONBOARD_COMMAND",
    "ONBOARD_SCRIPT_URL",
    "onboard_command",
    "onboard_script_url",
    "render_onboard_script",
    "render_trigger",
    "resolve_cp_hostname",
    "resolve_cp_url",
]
