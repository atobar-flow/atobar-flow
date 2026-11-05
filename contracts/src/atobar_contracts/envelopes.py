"""Pydantic envelope models, author binding, parser, and renderer for flow-* stigmergy."""

import hashlib
import json
import re
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TENANT_RE = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
FENCE_RE = re.compile(
    r"```flow-(ack|verdict|arbitration|plan)\s*\n(.*?)\n```",
    re.DOTALL,
)


def fingerprint(finding: Any) -> str:
    """Computes deterministic 'sha256:...' fingerprint over criterion + normalized evidence kind + message."""
    if isinstance(finding, dict):
        criterion = str(finding.get("criterion", ""))
        evidence = str(finding.get("evidence", ""))
        message = str(finding.get("message", ""))
    else:
        criterion = str(getattr(finding, "criterion", ""))
        evidence = str(getattr(finding, "evidence", ""))
        message = str(getattr(finding, "message", ""))

    criterion_norm = " ".join(criterion.strip().lower().split())

    # Normalised evidence kind: e.g. "check_run:12345" -> "check_run"
    evidence_kind = evidence.split(":", 1)[0] if ":" in evidence else evidence
    evidence_kind_norm = " ".join(evidence_kind.strip().lower().split())

    message_norm = " ".join(message.strip().lower().split())

    payload = f"{criterion_norm}|{evidence_kind_norm}|{message_norm}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class Finding(BaseModel, extra="forbid"):
    id: str
    criterion: str
    evidence: str
    severity: Literal["blocker", "major", "minor"] = "major"
    message: str = ""
    fingerprint: str = ""

    @model_validator(mode="after")
    def ensure_fingerprint(self) -> "Finding":
        if not self.fingerprint:
            self.fingerprint = fingerprint(self)
        return self


class Dispute(BaseModel, extra="forbid"):
    id: str
    reason: str


class BaseEnvelope(BaseModel, extra="forbid"):
    v: Literal[1] = 1
    tenant: str
    pr: int = Field(gt=0)
    head_sha: str
    run_id: Optional[str] = None

    @field_validator("tenant")
    @classmethod
    def validate_tenant(cls, v: str) -> str:
        if not TENANT_RE.fullmatch(v) or "/." in v or "./" in v:
            raise ValueError(f"Invalid tenant: {v!r}")
        return v

    @field_validator("head_sha")
    @classmethod
    def validate_head_sha(cls, v: str) -> str:
        if not SHA_RE.fullmatch(v):
            raise ValueError(f"Invalid head_sha: {v!r}, must be 40 lowercase hex characters")
        return v


class Ack(BaseEnvelope):
    role: Literal["developer"] = "developer"
    addressed_findings: list[str] = Field(default_factory=list)
    dispute: list[Dispute] = Field(default_factory=list)


class Verdict(BaseEnvelope):
    role: Literal["evaluator"] = "evaluator"
    round: int = Field(ge=1)
    verdict: Literal["approved", "changes_requested"]
    findings: list[Finding] = Field(default_factory=list)


class Arbitration(BaseEnvelope):
    role: Literal["arbitrator"] = "arbitrator"
    decision: Literal["pass", "fail", "blocked_requires_human"]
    mode: Literal["shadow", "recommend", "auto_merge"]
    action_taken: str
    observation_ids: list[str] = Field(default_factory=list)
    summary: Optional[str] = None
    progress: bool = False


class PlannedCriterion(BaseModel, extra="forbid"):
    id: str
    description: str
    verification_cmd: str = ""


class ProposedIssueDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    scope_files: list[str] = Field(default_factory=list)
    acceptance_criteria: list[PlannedCriterion] = Field(min_length=1)
    target_metric_key: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class Plan(BaseEnvelope):
    role: Literal["planner"] = "planner"
    complexity: Literal["single_pr", "multi_issue_epic"] = "single_pr"
    goal: str
    scope_files: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    acceptance_criteria: list[PlannedCriterion] = Field(default_factory=list)
    implementation_steps: list[str] = Field(default_factory=list)
    proposed_issues: list[ProposedIssueDraft] = Field(default_factory=list, max_length=3)


def extract_goal_targets(manifest: Any) -> list[str]:
    """Extracts target metric keys from a TenantManifest or manifest dict."""
    if manifest is None:
        return []
    goal = manifest.get("goal") if isinstance(manifest, dict) else getattr(manifest, "goal", None)
    if goal is None:
        return []
    targets = goal.get("targets") if isinstance(goal, dict) else getattr(goal, "targets", None)
    return [str(t) for t in (targets or [])]


def validate_goal_plan(plan: Plan, manifest: Any = None, *, require_proposed: bool = False) -> None:
    """Validates a goal-planning Plan envelope against manifest.goal.targets."""
    goal_targets = extract_goal_targets(manifest)
    is_goal_mode = require_proposed or bool(plan.proposed_issues) or (
        plan.complexity == "multi_issue_epic" and bool(goal_targets)
    )
    if not is_goal_mode:
        return
    if not (1 <= len(plan.proposed_issues) <= 3):
        raise ValueError(f"Goal planner must propose between 1 and 3 issues; got {len(plan.proposed_issues)}")
    for idx, draft in enumerate(plan.proposed_issues):
        if not draft.acceptance_criteria:
            raise ValueError(f"Proposed issue #{idx + 1} ({draft.title!r}) is missing acceptance_criteria")
        if goal_targets and draft.target_metric_key not in goal_targets:
            raise ValueError(
                f"Proposed issue #{idx + 1} references unknown target_metric_key {draft.target_metric_key!r}; "
                f"must be one of {goal_targets}"
            )


GOAL_ISSUE_LABEL = "flow:goal"
GOAL_SHA_MARKER_PREFIX = "<!-- atobar:goal_sha:"


def is_goal_planned_issue(issue: dict[str, Any], goal_sha: str = "") -> bool:
    """Returns True if an open GitHub issue was proposed by the outer goal loop."""
    body = str(issue.get("body") or "")
    if goal_sha and f"{GOAL_SHA_MARKER_PREFIX}{goal_sha} -->" in body:
        return True
    if GOAL_SHA_MARKER_PREFIX in body:
        return True
    raw_labels = issue.get("labels") or []
    for lbl in raw_labels:
        name = lbl.get("name") if isinstance(lbl, dict) else str(lbl)
        if name == GOAL_ISSUE_LABEL:
            return True
    return False


def format_proposed_issue_body(
    draft: ProposedIssueDraft,
    tracking_issue_number: Optional[int] = None,
    goal_sha: str = "",
) -> str:
    """Renders a ProposedIssueDraft into a structured GitHub issue markdown body."""
    lines: list[str] = []
    if goal_sha:
        lines.append(f"{GOAL_SHA_MARKER_PREFIX}{goal_sha} -->")
    else:
        lines.append(f"{GOAL_SHA_MARKER_PREFIX}active -->")
    lines += [
        f"## Goal\n{draft.goal}",
        f"\n## Target Metric & Rationale\n- **Target Metric:** `{draft.target_metric_key}`\n- **Rationale:** {draft.rationale}",
    ]
    if tracking_issue_number:
        lines.append(f"- **Tracking Goal Issue:** #{tracking_issue_number}")
    if draft.scope_files:
        scope_md = "\n".join(f"- `{p}`" for p in draft.scope_files)
        lines.append(f"\n## Scope\n{scope_md}")
    ac_lines = []
    for ac in draft.acceptance_criteria:
        cmd = f" (`{ac.verification_cmd}`)" if ac.verification_cmd else ""
        ac_lines.append(f"- [ ] **{ac.id}:** {ac.description}{cmd}")
    lines.append("\n## Acceptance Criteria\n" + "\n".join(ac_lines))
    return "\n".join(lines) + "\n"


def parse_and_validate_goal_plan(
    comment_body: str,
    author_login: str,
    bots: dict[str, str],
    manifest: Any,
    *,
    force_goal_mode: bool = False,
) -> Optional[Plan]:
    """Parses and validates an outer-loop goal Plan before any GitHub API call."""
    m = FENCE_RE.search(comment_body or "")
    if not m or m.group(1) != "plan":
        if force_goal_mode:
            raise ValueError("Goal planner turn-complete missing flow-plan envelope")
        return None
    try:
        raw_data = json.loads(m.group(2).strip())
    except Exception as exc:
        if force_goal_mode:
            raise ValueError(f"Invalid flow-plan JSON: {exc}") from exc
        return None
    if not isinstance(raw_data, dict):
        return None
    goal_targets = extract_goal_targets(manifest)
    has_proposed_key = "proposed_issues" in raw_data and raw_data.get("proposed_issues") is not None
    is_goal_turn = (
        force_goal_mode
        or bool(raw_data.get("proposed_issues"))
        or (raw_data.get("complexity") == "multi_issue_epic" and bool(goal_targets) and has_proposed_key)
    )
    if not is_goal_turn:
        return None
    expected_bot = bots.get("planner") or bots.get("developer") or "atobar-flow-agent[bot]"
    if author_login.strip().lower() != expected_bot.strip().lower():
        raise ValueError(f"Author {author_login!r} is not authorized to emit flow-plan")
    plan = Plan.model_validate(raw_data)
    validate_goal_plan(plan, manifest=manifest, require_proposed=True)
    return plan


def execute_goal_plan_proposals(
    plan: Plan,
    repo: str,
    manifest: Any,
    gh: Any,
    fallback_tracking_issue: Optional[int] = None,
) -> list[int]:
    """Creates proposed GitHub issues with autonomy-appropriate label and posts summary on tracking issue."""
    goal_cfg = (manifest.get("goal") if isinstance(manifest, dict) else getattr(manifest, "goal", None)) or {}
    goal_sha = ""
    if isinstance(goal_cfg, dict):
        autonomy = str(goal_cfg.get("autonomy") or "approve")
        tracking_issue = goal_cfg.get("tracking_issue_number") or fallback_tracking_issue or plan.pr
        if goal_cfg.get("objective"):
            goal_sha = hashlib.sha256(
                json.dumps(goal_cfg, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
    else:
        autonomy = str(getattr(goal_cfg, "autonomy", "approve") or "approve")
        tracking_issue = getattr(goal_cfg, "tracking_issue_number", None) or fallback_tracking_issue or plan.pr
        if hasattr(goal_cfg, "sha256"):
            goal_sha = goal_cfg.sha256()

    issue_label = "flow:feature" if autonomy == "auto" else "flow:needs-approval"
    labels_to_apply = [issue_label, GOAL_ISSUE_LABEL]
    created_numbers: list[int] = []
    summary_items: list[str] = []

    existing_by_title: dict[str, dict[str, Any]] = {}
    if hasattr(gh, "list_open_issues"):
        try:
            for item in gh.list_open_issues(repo) or []:
                if isinstance(item, dict) and item.get("title"):
                    existing_by_title[str(item["title"]).strip()] = item
        except Exception:
            pass

    for draft in plan.proposed_issues:
        issue_body = format_proposed_issue_body(
            draft,
            tracking_issue_number=int(tracking_issue) if tracking_issue else None,
            goal_sha=goal_sha,
        )
        res = existing_by_title.get(draft.title.strip())
        if res is None:
            res = gh.create_issue(repo, title=draft.title, body=issue_body, labels=labels_to_apply)
        num = int(res.get("number", 0)) if isinstance(res, dict) and res.get("number") else 0
        if num:
            created_numbers.append(num)
            summary_items.append(
                f"- #{num} **{draft.title}** (`{draft.target_metric_key}` · `{issue_label}`): {draft.rationale}"
            )
        else:
            summary_items.append(
                f"- **{draft.title}** (`{draft.target_metric_key}` · `{issue_label}`): {draft.rationale}"
            )

    if tracking_issue:
        summary_md = (
            f"🎯 **Goal Planner Proposed {len(plan.proposed_issues)} Issue(s)** (`autonomy={autonomy}`)\n\n"
            + "\n".join(summary_items)
            + f"\n\n{render(plan)}"
        )
        gh.create_comment(repo, int(tracking_issue), summary_md)

    return created_numbers


Envelope = Union[Ack, Verdict, Arbitration, Plan]

ENVELOPE_TAGS: dict[type[Any], str] = {
    Ack: "flow-ack",
    Verdict: "flow-verdict",
    Arbitration: "flow-arbitration",
    Plan: "flow-plan",
}

ROLE_BY_TYPE = {
    "ack": "developer",
    "verdict": "evaluator",
    "arbitration": "arbitrator",
    "plan": "planner",
}

MODEL_BY_TYPE: dict[str, type[BaseEnvelope]] = {
    "ack": Ack,
    "verdict": Verdict,
    "arbitration": Arbitration,
    "plan": Plan,
}

MODEL_BY_ROLE: dict[str, type[BaseEnvelope]] = {
    ROLE_BY_TYPE[k]: v for k, v in MODEL_BY_TYPE.items()
}


def compute_event_id(event_data: dict[str, Any]) -> str:
    """Computes a deterministic hash identifier for an incoming event payload."""
    serialized = json.dumps(event_data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def render(envelope: Envelope) -> str:
    """Renders an envelope into a fenced code block with flow-* tag."""
    tag = ENVELOPE_TAGS.get(type(envelope))
    if tag is None:
        raise ValueError(f"Unknown envelope type: {type(envelope)}")
    json_str = envelope.model_dump_json(indent=2)
    return f"```{tag}\n{json_str}\n```\n"


def render_hidden(envelope: Envelope) -> str:
    """Renders an envelope wrapped in an HTML comment so GitHub hides the raw JSON from human readers."""
    safe_fenced = render(envelope).rstrip().replace(">", r"\u003e")
    return f"<!-- atobar-envelope\n{safe_fenced}\n-->\n"


def parse(
    comment_body: str,
    author_login: str,
    bots: dict[str, str],
) -> Optional[Envelope]:
    """Parses a fenced flow-* envelope from a comment, enforcing author binding and strict schema."""
    m = FENCE_RE.search(comment_body)
    if not m:
        return None

    envelope_type = m.group(1)
    payload_str = m.group(2).strip()

    expected_role = ROLE_BY_TYPE.get(envelope_type)
    if not expected_role:
        return None

    expected_bot = bots.get(expected_role)
    if not expected_bot and expected_role == "planner":
        expected_bot = bots.get("developer")
    if not expected_bot or author_login.strip().lower() != expected_bot.strip().lower():
        return None

    try:
        data = json.loads(payload_str)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    if data.get("role") != expected_role:
        return None

    model_cls = MODEL_BY_TYPE.get(envelope_type)
    if not model_cls:
        return None

    try:
        val = model_cls.model_validate(data)
        if isinstance(val, (Ack, Verdict, Arbitration, Plan)):
            return val
        return None
    except ValidationError:
        return None
