"""Renders the factory-owned workflows and metric probes committed into a Client Repo."""

import json
import pathlib
import shutil

from atobar_contracts.client_templates import (
    HEADER,
    render_into,
    render_issue_template,
    render_metrics,
    render_pr_template,
    render_review_template,
)
from atobar_contracts.client_templates import render_trigger as client_render_trigger
from atobar_contracts.manifest import TENANTS_DIR, TenantManifest

__all__ = [
    "HEADER",
    "TENANTS_DIR",
    "render_into",
    "render_issue_template",
    "render_metrics",
    "render_pr_template",
    "render_review_template",
    "render_trigger",
]


def render_trigger(m: TenantManifest, cp_url: str) -> str:
    return client_render_trigger(m.slug, cp_url)
