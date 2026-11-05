"""Tenant manifest: the factory-owned source of truth for a Client Repo's metric policy."""

import hashlib
import json
import pathlib
import re
import sys
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from atobar_contracts.goal import GoalBudget, GoalStallPolicy, TenantGoal

TENANTS_DIR = pathlib.Path(__file__).resolve().parents[3] / "tenants"
REPO_RE = re.compile(r"(?!\.{1,2}/)[A-Za-z0-9_.-]+/(?!\.{1,2}\Z)[A-Za-z0-9_.-]+")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
METRIC_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

FORBIDDEN_CMD_PATTERNS = [
    re.compile(r"SUPABASE_", re.IGNORECASE),
    re.compile(r"GITHUB_TOKEN", re.IGNORECASE),
    re.compile(r"FLOW_RUN_CREDENTIAL", re.IGNORECASE),
    re.compile(r"CP_SESSION_SECRET|CP_LLM_KEK|CP_ADMIN_TOKEN|FE_INTERNAL_TOKEN", re.IGNORECASE),
    re.compile(r"AWS_SECRET_ACCESS_KEY", re.IGNORECASE),
    re.compile(r"/etc/atobar", re.IGNORECASE),
    re.compile(r"\.git-credentials", re.IGNORECASE),
    re.compile(r"/dev/shm/atobar-cred", re.IGNORECASE),
]


def validate_compiled_metric_command(run_cmd: str, protected_paths: Optional[list[str]] = None) -> list[str]:
    """Deterministic safety check for a compiled metric command, shared by the Spot runner and the Flow Engine."""
    if not run_cmd or not run_cmd.strip():
        return ["metric run command cannot be empty"]
    problems: list[str] = []
    if "\n" in run_cmd or "\r" in run_cmd:
        problems.append("metric command must be a single line (newlines/carriage returns are forbidden)")
    if "${{" in run_cmd:
        problems.append("metric command must not contain GitHub Actions expressions ('${{')")
    problems.extend(
        f"metric command references forbidden secret/path pattern '{pat.pattern}'"
        for pat in FORBIDDEN_CMD_PATTERNS
        if pat.search(run_cmd)
    )
    for prot in protected_paths or [".github/workflows/**", ".atobar/**"]:
        prefix = prot[:-3] if prot.endswith("/**") else prot
        if prefix and prefix in run_cmd and any(w in run_cmd for w in ("rm ", "mv ", "cp ", ">", "tee ", "sed -i")):
            problems.append(f"metric command mutates protected path '{prefix}'")
    return problems


class LoopLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_review_rounds: int = Field(default=4, ge=1, le=10)
    max_repeated_finding: int = Field(default=2, ge=1, le=5)
    max_pr_wall_clock_minutes: int = Field(default=240, ge=10, le=1440)
    max_pr_cost_usd: float = Field(default=25, gt=0, le=200)
    agent_runtime: Literal["native", "nanobot"] = "nanobot"


class MetricsRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid")
    python_version: str = "3.12"
    setup: list[str] = Field(default_factory=list)

    @field_validator("setup")
    @classmethod
    def _validate_setup_lines(cls, lines: list[str]) -> list[str]:
        for idx, line in enumerate(lines):
            problems = validate_compiled_metric_command(line)
            if problems:
                raise ValueError(f"metrics_runtime.setup[{idx}] invalid: {'; '.join(problems)}")
        return lines


class MetricSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = "check_run_output"
    check_name: str = "flow-metrics"
    workflow_path: str = ".github/workflows/flow-metrics.yml"


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=METRIC_KEY_RE.pattern)
    run: Optional[str] = Field(default=None, description="Shell command whose last stdout line is a number.")
    source: Optional[MetricSource] = None
    comparator: Literal[">=", "<=", ">", "<", "=="]
    threshold: float
    baseline_mode: Literal["absolute", "no_regression"] = "absolute"
    tolerance_pct: Optional[float] = None
    min_progress_delta: Optional[float] = Field(default=None, ge=0.0)
    min_samples: int = Field(default=1, ge=1)

    @field_validator("run")
    @classmethod
    def _validate_run_cmd(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        problems = validate_compiled_metric_command(v)
        if problems:
            raise ValueError(f"metric.run invalid: {'; '.join(problems)}")
        return v


class ArbitrationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["shadow", "recommend", "auto_merge"] = "shadow"
    merge_method: Literal["squash", "merge", "rebase"] = "squash"
    require_human_label: Optional[str] = None


class RoleLLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["gemini", "claude", "deepseek", "grok", "openai", "anthropic", "xai"] = "gemini"
    model: str

    @field_validator("provider", mode="before")
    @classmethod
    def _canonical_provider(cls, v: str) -> str:
        if not isinstance(v, str):
            return v
        # Canonical store names; aliases normalize so manifest, CP key
        # storage, and the engine factory agree.
        p = {"anthropic": "claude", "xai": "grok", "google": "gemini"}.get(v.strip().lower(), v.strip().lower())
        if p not in ("gemini", "claude", "deepseek", "grok", "openai"):
            raise ValueError(f"unsupported LLM provider: {v!r}")
        return p


class TenantLLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default: Optional[RoleLLMConfig] = None
    developer: Optional[RoleLLMConfig] = None
    evaluator: Optional[RoleLLMConfig] = None
    arbitrator: Optional[RoleLLMConfig] = None

    def for_role(self, role: str) -> Optional[RoleLLMConfig]:
        """Role-specific config, falling back to `default`."""
        return getattr(self, role, None) or self.default


class TenantManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str
    default_branch: str = "main"
    owner_github_login: str
    required_checks: list[str] = Field(default_factory=list)
    protected_paths: list[str] = Field(
        default_factory=lambda: [".github/workflows/**", ".atobar/**"]
    )
    metrics_workflow_path: str = ".github/workflows/flow-metrics.yml"
    arbitration: ArbitrationPolicy = Field(default_factory=ArbitrationPolicy)
    arbitration_mode: Optional[Literal["shadow", "recommend", "auto_merge"]] = Field(
        default=None,
        description="Deprecated: use arbitration.mode instead.",
    )
    llm: TenantLLMConfig = Field(default_factory=TenantLLMConfig)
    loop: LoopLimits = Field(default_factory=LoopLimits)
    metrics_runtime: MetricsRuntime = Field(default_factory=MetricsRuntime)
    metrics: list[Metric] = Field(min_length=1)
    goal: Optional[TenantGoal] = None

    @model_validator(mode="after")
    def _sync_arbitration_mode(self) -> "TenantManifest":
        if self.arbitration_mode is not None:
            self.arbitration.mode = self.arbitration_mode
        for idx, setup_line in enumerate(self.metrics_runtime.setup):
            problems = validate_compiled_metric_command(setup_line, self.protected_paths)
            if problems:
                raise ValueError(f"metrics_runtime.setup[{idx}] invalid: {'; '.join(problems)}")
        for m in self.metrics:
            if m.run is not None:
                problems = validate_compiled_metric_command(m.run, self.protected_paths)
                if problems:
                    raise ValueError(f"metric '{m.key}' run invalid: {'; '.join(problems)}")
        if self.goal is not None:
            metric_keys = {m.key for m in self.metrics}
            unknown_targets = [k for k in self.goal.targets if k not in metric_keys]
            if unknown_targets:
                raise ValueError(f"goal.targets references unknown metric key(s): {unknown_targets}")
            unknown_guardrails = [k for k in self.goal.guardrails if k not in metric_keys]
            if unknown_guardrails:
                raise ValueError(f"goal.guardrails references unknown metric key(s): {unknown_guardrails}")
            overlap = set(self.goal.targets) & set(self.goal.guardrails)
            if overlap:
                raise ValueError(
                    f"metric key(s) cannot be in both goal.targets and goal.guardrails: {sorted(overlap)}"
                )
        return self

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str) -> str:
        if not REPO_RE.fullmatch(v):
            raise ValueError("repo must be owner/name")
        return v

    @field_validator("metrics")
    @classmethod
    def _unique(cls, v: list[Metric]) -> list[Metric]:
        keys = [m.key for m in v]
        if len(keys) != len(set(keys)):
            raise ValueError("metric keys must be unique")
        return v

    @property
    def slug(self) -> str:
        return self.repo.replace("/", "__")

    def sha256(self) -> str:
        return canonical_sha(self.model_dump(mode="json"))


class FlowMetricItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    value: float
    unit: Optional[str] = None
    evidence: Optional[dict[str, Any]] = None


class FlowMetricsReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    tenant: str
    commit_sha: str
    metrics: list[FlowMetricItem]

    @field_validator("tenant")
    @classmethod
    def _tenant(cls, v: str) -> str:
        if not REPO_RE.fullmatch(v):
            raise ValueError("tenant must be owner/name")
        return v

    @field_validator("commit_sha")
    @classmethod
    def _sha(cls, v: str) -> str:
        if not SHA_RE.fullmatch(v):
            raise ValueError("commit_sha must be 40-char lowercase hex")
        return v


def canonical_sha(manifest_or_dict: Any) -> str:
    """Single canonical SHA-256 digest over a TenantManifest or its dict representation."""
    if isinstance(manifest_or_dict, TenantManifest):
        data = manifest_or_dict.model_dump(mode="json")
    elif isinstance(manifest_or_dict, dict):
        cleaned = {k: v for k, v in manifest_or_dict.items() if k != "created_by"}
        try:
            data = TenantManifest.model_validate(cleaned).model_dump(mode="json")
        except Exception:
            data = cleaned
    else:
        data = manifest_or_dict
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def slug_for(repo: str) -> str:
    return repo.replace("/", "__")


_SEEDED_PILOT_MANIFESTS: dict[str, dict[str, Any]] = {
    "arsenyspb/ollama-monitor": {
        "repo": "arsenyspb/ollama-monitor",
        "default_branch": "main",
        "owner_github_login": "arsenyspb",
        "arbitration": {"mode": "shadow"},
        "loop": {
            "max_review_rounds": 4,
            "max_repeated_finding": 2,
            "max_pr_wall_clock_minutes": 30,
            "max_pr_cost_usd": 25.0,
            "agent_runtime": "nanobot",
        },
        "metrics_runtime": {
            "python_version": "3.10",
            "setup": [
                "python -m ensurepip --upgrade",
                "python -m pip install --upgrade pip",
                "pip install -r requirements-dev.txt",
            ],
        },
        "metrics": [
            {
                "key": "e2e_install_time_seconds",
                "run": "bash -c 'start=$(date +%s); pip install -e . -q; end=$(date +%s); echo $((end-start))'",
                "comparator": "<=",
                "threshold": 10.0,
            },
            {
                "key": "dependency_count",
                "run": "pip freeze | wc -l | awk '{print $1}'",
                "comparator": "<=",
                "threshold": 0.0,
                "baseline_mode": "no_regression",
            },
        ],
    }
}


def load_manifest(repo: str, tenants_dir: pathlib.Path = TENANTS_DIR) -> TenantManifest:
    path = tenants_dir / f"{slug_for(repo)}.json"
    if path.exists():
        manifest = TenantManifest.model_validate_json(path.read_text())
        if manifest.repo != repo:
            raise ValueError(f"{path} declares repo={manifest.repo}, expected {repo}")
        return manifest
    if tenants_dir == TENANTS_DIR and repo in _SEEDED_PILOT_MANIFESTS:
        return TenantManifest.model_validate(_SEEDED_PILOT_MANIFESTS[repo])
    raise FileNotFoundError(f"no tenant manifest at {path}; add it to atobar-flow first")


def validate_all(tenants_dir: pathlib.Path = TENANTS_DIR) -> list[str]:
    """Validates every json manifest under tenants_dir."""
    manifest_paths = list(tenants_dir.glob("*.json"))
    validated = []
    for p in sorted(manifest_paths):
        manifest = TenantManifest.model_validate_json(p.read_text())
        expected_slug = slug_for(manifest.repo)
        if p.stem != expected_slug:
            raise ValueError(f"Manifest file name {p.name} does not match slug {expected_slug}.json")
        validated.append(manifest.repo)
    return validated


if __name__ == "__main__":
    try:
        repos = validate_all()
        print(f"Validated {len(repos)} tenant manifests: {', '.join(repos)}")
    except Exception as e:
        print(f"Error validating tenant manifests: {e}", file=sys.stderr)
        sys.exit(1)
