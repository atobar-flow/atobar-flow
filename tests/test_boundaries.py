"""AST import boundary guard across all 5 microservice workspace packages (#300 WP-0)."""

import ast
import base64
from datetime import datetime, timedelta, timezone
import json
import pathlib
import re
import shlex
import shutil
import subprocess
import tomllib
from typing import Any

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

SERVICES: dict[str, pathlib.Path] = {
    "atobar_contracts": REPO_ROOT / "contracts" / "src" / "atobar_contracts",
    "control_panel": REPO_ROOT / "control-panel" / "src" / "control_panel",
    "flow_engine": REPO_ROOT / "flow-engine" / "src" / "flow_engine",
    "flow_agent": REPO_ROOT / "flow-agent" / "src" / "flow_agent",
    "atobar_ops": REPO_ROOT / "atobar-ops" / "src" / "atobar_ops",
}

ALL_SERVICE_PACKAGES = set(SERVICES.keys())
LLM_SDKS = {"google.genai", "google", "nanobot", "openai", "anthropic", "litellm", "langchain"}

FORBIDDEN_BY_PACKAGE: dict[str, set[str]] = {
    "atobar_contracts": (
        (ALL_SERVICE_PACKAGES - {"atobar_contracts"})
        | {"supabase", "fastapi"}
        | LLM_SDKS
    ),
    "control_panel": ALL_SERVICE_PACKAGES - {"control_panel", "atobar_contracts"},
    "flow_engine": (ALL_SERVICE_PACKAGES - {"flow_engine", "atobar_contracts"}) | LLM_SDKS,
    "flow_agent": (
        (ALL_SERVICE_PACKAGES - {"flow_agent", "atobar_contracts"})
        | {"supabase", "fastapi"}
    ),
    "atobar_ops": ALL_SERVICE_PACKAGES - {"atobar_ops", "atobar_contracts"},
}


def _extract_imports(py_file: pathlib.Path) -> list[tuple[int, str]]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.append((node.lineno, node.module))
    return found


def _matches_forbidden(imported: str, forbidden: set[str]) -> str | None:
    top = imported.split(".", 1)[0]
    if top in forbidden:
        return top
    for rule in forbidden:
        if imported == rule or imported.startswith(f"{rule}."):
            return rule
    return None


def test_workspace_package_directories_exist() -> None:
    for pkg, pkg_dir in SERVICES.items():
        assert pkg_dir.is_dir(), f"Missing workspace package directory for {pkg}: {pkg_dir}"
        py_files = list(pkg_dir.rglob("*.py"))
        assert py_files, f"No Python files found in {pkg_dir}"


FORBIDDEN_FLOW_AGENT_CORE_IMPORTS: set[str] = {
    "flow_agent.agents",
    "flow_agent.knowledge",
    "flow_agent.llm",
    "flow_agent.roles",
    "flow_agent.runtime",
    "flow_agent.telemetry",
    "flow_agent.tools",
    "flow_agent.tools_mcp",
    "flow_agent.main",
}


def test_strict_ast_import_boundaries() -> None:
    violations: list[str] = []
    for pkg, pkg_dir in SERVICES.items():
        if not pkg_dir.is_dir():
            violations.append(f"{pkg}: directory {pkg_dir} does not exist")
            continue
        forbidden = FORBIDDEN_BY_PACKAGE[pkg]
        for py_file in sorted(pkg_dir.rglob("*.py")):
            rel = py_file.relative_to(REPO_ROOT)
            for lineno, mod in _extract_imports(py_file):
                hit = _matches_forbidden(mod, forbidden)
                if hit is not None:
                    violations.append(f"{rel}:{lineno} ({pkg}) imports forbidden '{mod}' (matched '{hit}')")
    assert violations == [], "AST boundary violations:\n" + "\n".join(violations)


def test_flow_agent_core_leaf_layer_boundary() -> None:
    """flow_agent.core must remain a leaf subpackage and never import higher-level flow_agent subpackages (#349)."""
    core_dir = SERVICES["flow_agent"] / "core"
    violations: list[str] = []
    for py_file in sorted(core_dir.glob("*.py")):
        rel = py_file.relative_to(REPO_ROOT)
        for lineno, mod in _extract_imports(py_file):
            hit = _matches_forbidden(mod, FORBIDDEN_FLOW_AGENT_CORE_IMPORTS)
            if hit is not None:
                violations.append(f"{rel}:{lineno} (flow_agent.core) imports forbidden '{mod}' (matched '{hit}')")
    assert violations == [], "flow_agent.core leaf-layer violations:\n" + "\n".join(violations)


# --- WP-5: Pytest importlib mode ---


def test_ac5_importlib_mode_allows_duplicate_test_basenames(tmp_path: pathlib.Path) -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = pyproject.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts", "")
    assert "--import-mode=importlib" in str(addopts)

    dir_a = tmp_path / "pkg_a" / "tests"
    dir_b = tmp_path / "pkg_b" / "tests"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    (dir_a / "test_same_basename.py").write_text("def test_one() -> None:\n    assert 1 + 1 == 2\n", encoding="utf-8")
    (dir_b / "test_same_basename.py").write_text("def test_two() -> None:\n    assert 2 * 2 == 4\n", encoding="utf-8")

    rc = pytest.main(["--import-mode=importlib", "-q", str(dir_a), str(dir_b)])
    assert rc == 0


# --- WP-6: MemoryStore copy-on-read ---


def _seed_memory_store(store: Any) -> str:
    t = store.create_tenant("arsenyspb/ollama-monitor", "arsenyspb")
    tenant_id = str(t.id)
    store.update_tenant_manifest(
        tenant_id,
        {
            "repo": "arsenyspb/ollama-monitor",
            "owner_github_login": "arsenyspb",
            "repository_id": "repo-999",
            "metrics": [
                {
                    "key": "coverage_pct",
                    "run": "pytest",
                    "comparator": ">=",
                    "threshold": 80.0,
                    "baseline_mode": "relative_to_base",
                    "min_samples": 1,
                }
            ],
        },
        "sha-1",
    )
    store.add_token(tenant_id, "tok-hash-1", "arsenyspb", "ci")
    store.upsert_wiki(tenant_id, "arsenyspb/ollama-monitor", {"purpose": "monitor", "technologies": ["python"]})
    store.upsert_metric_definition(
        tenant_id,
        {
            "key": "coverage_pct",
            "status": "active",
            "run": "pytest",
            "comparator": ">=",
            "threshold": 80.0,
            "compiled": {"run": "pytest"},
        },
    )
    store.record_observations(
        [
            {
                "tenant_id": tenant_id,
                "key": "coverage_pct",
                "value": 82.0,
                "commit_sha": "a" * 40,
                "pr_number": None,
                "run_id": 101,
                "run_attempt": 1,
            },
            {
                "tenant_id": tenant_id,
                "key": "coverage_pct",
                "value": 85.0,
                "commit_sha": "b" * 40,
                "pr_number": 7,
                "run_id": 102,
                "run_attempt": 1,
            },
        ]
    )
    store.put_llm_key(tenant_id, "openai", {"ciphertext": "enc-1", "key_hint": "sk-...1234"})
    store.set_role_llm_pref(tenant_id, "developer", "openai", "gpt-5", "medium")
    store.create_run_credential("run-1", tenant_id, "developer", "hash-1", "2099-01-01T00:00:00Z", goal_mode=True)
    store.record_loop_round({"tenant_id": tenant_id, "pr_number": 7, "round": 1, "role": "developer", "tokens": 10})
    stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    now_ts = datetime.now(timezone.utc).isoformat()
    store.upsert_session(
        {
            "instance_id": "i-stale",
            "run_id": "run-stale",
            "tenant_id": tenant_id,
            "role": "developer",
            "issue_id": 7,
            "status": "Running",
            "started_at": stale_ts,
            "heartbeat_at": stale_ts,
        }
    )
    store.upsert_session(
        {
            "instance_id": "i-live",
            "run_id": "run-1",
            "tenant_id": tenant_id,
            "role": "metric_compiler",
            "issue_id": 0,
            "metric_key": "coverage_pct",
            "current_todo": "compile:coverage_pct",
            "status": "Running",
            "started_at": now_ts,
            "heartbeat_at": now_ts,
        }
    )
    store.record_run_event("run-1", "step", {"msg": "ok"})
    store.upsert_tenant_goal(tenant_id, {"goal_sha": "g-sha", "status": "open", "spent_usd": 1.5})
    return tenant_id


# MemoryStore methods that are writes, or reads returning immutable scalars, so they need no copy-on-read check.
AC6_NON_READ_METHODS = frozenset(
    {
        # scalar / immutable reads
        "bound_metric_key_for_run",
        "get_spot_max_minutes",
        "get_tenant_llm_ciphertext",
        "has_processed_event",
        "issue_session_active",
        "ready_to_activate",
        # writes
        "add_token",
        "approve_metric_atomic",
        "claim_goal_planning",
        "claim_run_credential_envelope",
        "claim_run_credential_instance",
        "clear_goal_planning",
        "create_run_credential",
        "create_tenant",
        "delete_llm_key",
        "insert_arbitration_decision",
        "mark_run_credential_used",
        "put_llm_key",
        "rebuild_tenant_manifest",
        "record_goal_merge",
        "record_goal_spend",
        "record_loop_round",
        "record_observations",
        "record_processed_event",
        "record_run_event",
        "record_run_events",
        "record_security_audit",
        "retire_metric",
        "revoke_tokens",
        "set_arbitration_mode",
        "set_role_llm_pref",
        "set_tenant_goal",
        "set_tenant_guardrails",
        "update_session",
        "update_tenant_goal",
        "update_tenant_manifest",
        "update_tenant_status",
        "upsert_metric_definition",
        "upsert_session",
        "claim_wiki_refresh",
        "complete_wiki_refresh_run",
        "record_wiki_default_branch_merge",
        "update_wiki_state",
        "upsert_tenant_goal",
        "upsert_wiki",
    }
)


class _RecordingStore:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.accessed: set[str] = set()

    def __getattr__(self, name: str) -> Any:
        self.accessed.add(name)
        return getattr(self._inner, name)


@pytest.mark.parametrize("store_pkg", ["flow_engine", "control_panel"])
def test_ac6_memory_store_getters_return_deep_copies(store_pkg: str) -> None:
    if store_pkg == "flow_engine":
        from flow_engine.store import MemoryStore as StoreCls
    else:
        from control_panel.store import MemoryStore as StoreCls  # type: ignore[assignment]

    real_store = StoreCls()
    tid = _seed_memory_store(real_store)
    store = _RecordingStore(real_store)

    # 1. Tenant getters
    for getter in (
        lambda: store.tenant_by_id(tid),
        lambda: store.tenant_by_repo("arsenyspb/ollama-monitor"),
        lambda: store.tenant_for_token_hash("tok-hash-1"),
        lambda: store.tenant_for_repository_id("repo-999"),
    ):
        t_obj = getter()
        assert t_obj is not None
        t_obj.manifest["mutated"] = True
        assert "mutated" not in getter().manifest  # type: ignore[union-attr]

    tenants_list = store.list_tenants()
    tenants_list[0].manifest["mutated_list"] = True
    assert "mutated_list" not in store.list_tenants()[0].manifest

    # 2. Dict getters
    dict_getters = [
        lambda: store.wiki_for_tenant(tid),
        lambda: store.get_tenant_goal(tid),
        lambda: store.get_metric_definition(tid, "coverage_pct"),
        lambda: store.get_tenant_llm_key_record(tid, "openai"),
        lambda: store.get_run_credential("run-1"),
        lambda: store.get_session_by_instance("i-live"),
        lambda: store.get_session_by_run("run-1"),
        lambda: store.latest_metric_compiler_session(tid, "coverage_pct"),
    ]
    if hasattr(store, "active_session_for_role"):
        dict_getters.append(lambda: store.active_session_for_role(tid, "metric_compiler", metric_key="coverage_pct"))

    for d_getter in dict_getters:
        rec = d_getter()
        assert isinstance(rec, dict)
        rec["__poison__"] = "bad"
        for v in rec.values():
            if isinstance(v, dict):
                v["__nested_poison__"] = "bad"
            elif isinstance(v, list):
                v.append("__nested_poison__")
        reread = d_getter()
        assert isinstance(reread, dict)
        assert "__poison__" not in reread
        for v in reread.values():
            if isinstance(v, dict):
                assert "__nested_poison__" not in v
            elif isinstance(v, list):
                assert "__nested_poison__" not in v

    # 3. List-of-dict & nested-dict getters
    list_getters = [
        lambda: store.metric_definitions(tid),
        lambda: store.recent_observations(tid),
        lambda: store.observations_for_pr_or_sha(tid, pr_number=7),
        lambda: store.default_branch_observations(tid),
        lambda: store.active_tokens(tid),
        lambda: store.llm_keys(tid),
        lambda: store.loop_rounds_for_pr(tid, 7),
        lambda: store.loop_rounds_for_tenant(tid),
        lambda: store.active_sessions(),
        lambda: store.list_open_sessions(),
        lambda: store.run_events_for_run("run-1"),
    ]
    for l_getter in list_getters:
        items = l_getter()
        assert isinstance(items, list) and items
        items[0]["__poison__"] = "bad"
        items.clear()
        reread_items = l_getter()
        assert len(reread_items) >= 1
        assert "__poison__" not in reread_items[0]

    prefs = store.role_llm_prefs(tid)
    prefs["developer"]["model"] = "mutated-model"
    assert store.role_llm_prefs(tid)["developer"]["model"] == "gpt-5"

    for set_getter in (lambda: store.metric_keys(tid), lambda: store.observed_metric_keys(tid)):
        keys = set_getter()
        keys.add("__poison__")
        assert "__poison__" not in set_getter()

    # Completeness: every public method is either copy-checked above or explicitly classified, so a new
    # getter fails here until someone adds a deep-copy check for it (or exempts it with a reason).
    public = {n for n, v in vars(StoreCls).items() if callable(v) and not n.startswith("_")}
    unclassified = public - store.accessed - AC6_NON_READ_METHODS
    assert not unclassified, (
        f"New {store_pkg} MemoryStore methods need a copy-on-read check in this test "
        f"or an entry in AC6_NON_READ_METHODS: {sorted(unclassified)}"
    )


# --- WP-7: Spot UserData preserve-env parity & Store hasattr ratchet ---

# Variables intentionally exported by UserData that are not forwarded via sudo --preserve-env
EXEMPT_SPOT_EXPORTS: dict[str, str] = {}


def _eval_bash_exports(script: str) -> dict[str, str]:
    out = subprocess.run(
        ["bash", "-c", script + "\nenv -0"],
        capture_output=True,
        text=True,
        env={},
        check=True,
    )
    return dict(kv.split("=", 1) for kv in out.stdout.split("\0") if "=" in kv)


def _extract_sudo_preserve_env_sets(cloud_init_text: str) -> list[tuple[int, set[str]]]:
    found: list[tuple[int, set[str]]] = []
    for lineno, line in enumerate(cloud_init_text.splitlines(), 1):
        if "sudo" not in line or "--preserve-env=" not in line:
            continue
        prefix = line.split("bash -lc", 1)[0]
        for tok in shlex.split(prefix, comments=True):
            if tok.startswith("--preserve-env="):
                names = {n.strip() for n in tok.split("=", 1)[1].split(",") if n.strip()}
                found.append((lineno, names))
    return found


def _check_spot_exports_against_cloud_init(exported_names: set[str], cloud_init_text: str) -> list[str]:
    sudo_lines = _extract_sudo_preserve_env_sets(cloud_init_text)
    if not sudo_lines:
        return ["No `sudo --preserve-env=...` lines found in cloud_init.sh.tpl"]
    required = exported_names - set(EXEMPT_SPOT_EXPORTS)
    errors: list[str] = []
    for lineno, preserved in sudo_lines:
        missing = sorted(required - preserved)
        if missing:
            errors.append(f"cloud_init.sh.tpl:{lineno} missing exported vars in --preserve-env: {missing}")
    return errors


def test_ac7_spot_userdata_exports_survive_sudo() -> None:
    from atobar_contracts.github_rest import launch_spot_instance

    class _FakeEC2:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def describe_launch_template_versions(self, **_: Any) -> dict[str, Any]:
            return {
                "LaunchTemplateVersions": [
                    {"LaunchTemplateData": {"UserData": base64.b64encode(b"#!/bin/bash\n").decode("ascii")}}
                ]
            }

        def run_instances(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {"Instances": [{"InstanceId": "i-spot-parity"}]}

    ec2 = _FakeEC2()
    launch_spot_instance(
        {
            "role": "planner",
            "target_repo": "arsenyspb/ollama-monitor",
            "issue_id": 42,
            "pr": 42,
            "head_sha": "a" * 40,
            "label": "flow:plan",
            "engine_ref": "main",
            "target_ref": "main",
            "run_id": "run-parity-1",
            "run_credential": "cred-parity-1",
            "agent_runtime": "nanobot",
            "cp_url": "https://cp.example.invalid",
            "metric_key": "coverage_pct",
            "metric_intent": "keep coverage high",
            "goal_mode": True,
            "plan_count": 2,
            "goal_objective": "Raise coverage to 85%",
        },
        ec2_client=ec2,
    )
    user_data_script = base64.b64decode(ec2.calls[0]["UserData"]).decode("utf-8")
    baseline_vars = set(_eval_bash_exports("true"))
    exported_vars = set(_eval_bash_exports(user_data_script)) - baseline_vars
    assert "FLOW_ROLE" in exported_vars
    assert "FLOW_METRIC_KEY" in exported_vars
    assert "FLOW_GOAL_MODE" in exported_vars

    cloud_init_path = REPO_ROOT / "infra" / "terraform" / "control-panel" / "scripts" / "cloud_init.sh.tpl"
    cloud_init_text = cloud_init_path.read_text(encoding="utf-8")
    assert _check_spot_exports_against_cloud_init(exported_vars, cloud_init_text) == []

    # Negative check (AC-7): removing any exported var from either sudo line must fail the guard
    for idx in range(2):
        lines = cloud_init_text.splitlines()
        sudo_indices = [i for i, ln in enumerate(lines) if "--preserve-env=" in ln]
        assert len(sudo_indices) >= 2
        target_idx = sudo_indices[idx]
        lines[target_idx] = lines[target_idx].replace("FLOW_GOAL_MODE,", "")
        errs = _check_spot_exports_against_cloud_init(exported_vars, "\n".join(lines))
        assert errs and "FLOW_GOAL_MODE" in errs[0]


def _count_store_hasattr_in_tree(tree: ast.AST) -> int:
    count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "hasattr"
            and node.args
            and "store" in ast.unparse(node.args[0]).lower()
        ):
            count += 1
    return count


def _evaluate_store_hasattr_ratchet(
    current_counts: dict[str, int], baseline_counts: dict[str, int]
) -> tuple[list[str], list[str]]:
    regressions: list[str] = []
    improvements: list[str] = []
    all_files = sorted(set(current_counts) | set(baseline_counts))
    for rel_path in all_files:
        cur = current_counts.get(rel_path, 0)
        base = baseline_counts.get(rel_path, 0)
        if cur > base:
            regressions.append(
                f"{rel_path}: hasattr(store, ...) count increased from {base} to {cur} "
                "(add the method to atobar_contracts.store.Store Protocol instead)"
            )
        elif cur < base:
            improvements.append(
                f"{rel_path}: hasattr(store, ...) count decreased from {base} to {cur} — "
                "lower the baseline in tests/hasattr_store_baseline.json"
            )
    return regressions, improvements


def test_ac7_store_hasattr_ratchet() -> None:
    baseline_path = REPO_ROOT / "tests" / "hasattr_store_baseline.json"
    baseline: dict[str, int] = json.loads(baseline_path.read_text(encoding="utf-8"))

    current: dict[str, int] = {}
    for tree_dir in ("contracts/src", "flow-engine/src", "control-panel/src"):
        for py_file in sorted((REPO_ROOT / tree_dir).rglob("*.py")):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            c = _count_store_hasattr_in_tree(tree)
            if c:
                current[str(py_file.relative_to(REPO_ROOT))] = c

    regressions, improvements = _evaluate_store_hasattr_ratchet(current, baseline)
    assert regressions == [], "Store hasattr() ratchet regressions:\n" + "\n".join(regressions)
    # The baseline must equal reality, otherwise removed calls could silently be re-added later.
    assert improvements == [], "Store hasattr() baseline is stale:\n" + "\n".join(improvements)

    # Negative check (AC-7): adding a hasattr(store, ...) call triggers a ratchet failure
    synthetic = dict(current)
    synthetic["flow-engine/src/flow_engine/store.py"] = synthetic.get("flow-engine/src/flow_engine/store.py", 0) + 1
    neg_regressions, _ = _evaluate_store_hasattr_ratchet(synthetic, baseline)
    assert len(neg_regressions) == 1 and "flow-engine/src/flow_engine/store.py" in neg_regressions[0]
    synthetic_lower = {k: v - 1 for k, v in baseline.items()}
    _, stale = _evaluate_store_hasattr_ratchet(synthetic_lower, baseline)
    assert len(stale) == len(baseline) and all("lower the baseline" in m for m in stale)


# --- WP-8: Gitleaks metric-key allowlist & fake-token detection ---


def test_ac8_gitleaks_config_allows_metric_keys_and_flags_fake_tokens(tmp_path: pathlib.Path) -> None:
    cfg_path = REPO_ROOT / ".gitleaks.toml"
    assert cfg_path.is_file(), "Missing .gitleaks.toml at repository root"
    parsed = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    assert parsed.get("extend", {}).get("useDefault") is True
    regexes = parsed.get("allowlist", {}).get("regexes", [])
    assert len(regexes) == 1
    allow_re = re.compile(regexes[0])

    # Domain metric keys with unit suffixes match the allowlist
    assert allow_re.fullmatch("p95_latency_ms")
    assert allow_re.fullmatch("coverage_pct")
    assert allow_re.fullmatch("bundle_size_bytes")
    # High-entropy tokens (even ending in _ms) do NOT match the allowlist
    hex_body = "".join(f"{i:x}{(i * 7 + 3) % 16:x}" for i in range(12))
    fake_token_ms = "_".join(("sk", "live", hex_body, "ms"))
    assert allow_re.fullmatch(fake_token_ms) is None

    gitleaks_bin = shutil.which("gitleaks")
    if gitleaks_bin:
        clean_dir = tmp_path / "clean"
        clean_dir.mkdir()
        (clean_dir / "fixture.py").write_text('m = {"key": "p95_latency_ms"}\n', encoding="utf-8")
        res_clean = subprocess.run(
            [gitleaks_bin, "dir", str(clean_dir), "--config", str(cfg_path), "--no-banner"],
            capture_output=True,
            text=True,
        )
        assert res_clean.returncode == 0, f"gitleaks flagged p95_latency_ms: {res_clean.stdout}\n{res_clean.stderr}"

        leak_dir = tmp_path / "leak"
        leak_dir.mkdir()
        fake_atb_tok = "atb_" + "A1b2C3d4E5f6G7h8I9j0"
        hardcoded_cp = "cp.atobar" + ".org"
        (leak_dir / "fixture.py").write_text(
            f'm1 = {{"key": "p95_latency_ms"}}\nm2 = {{"key": "{fake_token_ms}"}}\n'
            f'tok = "{fake_atb_tok}"\nhost = "{hardcoded_cp}"\n',
            encoding="utf-8",
        )
        report_path = tmp_path / "report.json"
        res_leak = subprocess.run(
            [
                gitleaks_bin,
                "dir",
                str(leak_dir),
                "--config",
                str(cfg_path),
                "--report-path",
                str(report_path),
                "--no-banner",
            ],
            capture_output=True,
            text=True,
        )
        assert res_leak.returncode != 0, "gitleaks failed to flag high-entropy fake token ending in _ms"
        findings = json.loads(report_path.read_text(encoding="utf-8"))
        secrets_flagged = [f.get("Secret") for f in findings]
        assert fake_token_ms in secrets_flagged
        assert fake_atb_tok in secrets_flagged
        assert hardcoded_cp in secrets_flagged
        assert "p95_latency_ms" not in secrets_flagged


# --- J45: CI FinOps & Test-Suite Modernization structural guards ---


def _load_ci_yml() -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def test_wp1_ci_has_single_billable_job_on_pull_request() -> None:
    ci_doc = _load_ci_yml()
    jobs = ci_doc.get("jobs") or {}
    assert list(jobs.keys()) == ["test"]
    for step in jobs["test"].get("steps") or []:
        uses = str(step.get("uses") or "")
        assert not uses.startswith("gitleaks/gitleaks-action")


def test_wp1_gitleaks_step_runs_when_previous_steps_fail() -> None:
    ci_doc = _load_ci_yml()
    steps = ci_doc["jobs"]["test"]["steps"]
    assert steps, "ci.yml test job must have steps"
    last_step = steps[-1]
    assert last_step.get("name") == "Secret scan (gitleaks)"
    assert "!cancelled()" in str(last_step.get("if") or "")


def test_wp1_ci_permissions_least_privilege() -> None:
    ci_doc = _load_ci_yml()
    assert ci_doc.get("permissions") == {"contents": "read"}


def test_wp1_gitleaks_version_parity() -> None:
    from atobar_ops import verify

    ci_doc = _load_ci_yml()
    test_job = ci_doc["jobs"]["test"]
    assert (test_job.get("env") or {}).get("GITLEAKS_VERSION") == verify.GITLEAKS_VERSION
    install_steps = [s for s in test_job["steps"] if s.get("name") == "Install gitleaks"]
    assert len(install_steps) == 1
    assert "sha256sum -c" in str(install_steps[0].get("run") or "")


def test_wp3_every_workflow_job_has_bounded_timeout() -> None:
    wf_dir = REPO_ROOT / ".github" / "workflows"
    wf_files = sorted(list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml")))
    assert wf_files, "Expected workflow files in .github/workflows/"
    for wf_path in wf_files:
        doc = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        for job_id, job in (doc.get("jobs") or {}).items():
            tm = job.get("timeout-minutes")
            assert isinstance(tm, int) and not isinstance(tm, bool) and 1 <= tm <= 30, (
                f"{wf_path.name}:{job_id} must set integer timeout-minutes in [1, 30], got {tm!r}"
            )


def test_wp3_ci_cancels_only_pull_requests() -> None:
    ci_doc = _load_ci_yml()
    concurrency = ci_doc.get("concurrency") or {}
    cancel_expr = str(concurrency.get("cancel-in-progress") or "")
    group_expr = str(concurrency.get("group") or "")
    assert "pull_request" in cancel_expr
    assert "pull_request" in group_expr and "run_id" in group_expr


def test_wp3_faulthandler_timeout_configured() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_opts = pyproject.get("tool", {}).get("pytest", {}).get("ini_options", {})
    fh_timeout = pytest_opts.get("faulthandler_timeout")
    assert isinstance(fh_timeout, int) and not isinstance(fh_timeout, bool) and 0 < fh_timeout <= 120


def test_wp4_ci_binary_downloads_are_checksummed() -> None:
    ci_doc = _load_ci_yml()
    for step in ci_doc["jobs"]["test"]["steps"]:
        run_script = str(step.get("run") or "")
        if "curl" in run_script:
            assert "sha256sum -c" in run_script, (
                f"Step {step.get('name')!r} in ci.yml uses curl without sha256sum -c verification"
            )


def test_j52_public_org_and_user_zero_boundary_ratchet() -> None:
    """J52 AC-1..AC-4: Enforce .env.sample + zero legacy domain/repo/account/Supabase literals."""
    env_sample = REPO_ROOT / ".env.sample"
    assert env_sample.is_file(), ".env.sample must exist at repo root"
    assert not (REPO_ROOT / ".env.example").exists(), ".env.example must be renamed to .env.sample"

    gitignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!.env.sample" in gitignore_text
    assert ".env" in gitignore_text.splitlines()

    sample_text = env_sample.read_text(encoding="utf-8")
    for req_line in (
        "FLOW_GITHUB_ORG=atobar-flow",
        "FLOW_FACTORY_REPO=atobar-flow/atobar-flow",
        "CP_HOSTNAME=",
        "CP_PUBLIC_URL=",
    ):
        assert req_line in sample_text, f".env.sample missing {req_line!r}"

    onboard_sh = REPO_ROOT / "scripts" / "onboard.sh"
    assert onboard_sh.is_file(), "scripts/onboard.sh must exist"
    assert "ATOBAR_CP_URL" in onboard_sh.read_text(encoding="utf-8")

    # Control Panel URL must compose dynamically from CP_HOSTNAME secret when CP_PUBLIC_URL is unset
    from atobar_contracts.client_templates import resolve_cp_hostname, resolve_cp_url

    old_env = {k: pathlib.os.environ.pop(k, None) for k in ("CP_PUBLIC_URL", "CONTROL_PANEL_URL", "ATOBAR_CP_URL", "CP_HOSTNAME")}
    try:
        pathlib.os.environ["CP_HOSTNAME"] = "secret-cp.internal.test"
        assert resolve_cp_url() == "https://secret-cp.internal.test"
        assert resolve_cp_hostname() == "secret-cp.internal.test"
    finally:
        pathlib.os.environ.pop("CP_HOSTNAME", None)
        for k, v in old_env.items():
            if v is not None:
                pathlib.os.environ[k] = v

    # Workflows must use secrets.* (never vars.*) for cloud/deployment configuration and pass actionlint
    wf_paths = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    for wf_path in wf_paths:
        wf_text = wf_path.read_text(encoding="utf-8")
        assert "vars." not in wf_text, f"{wf_path.name} must use secrets.* instead of vars.*"
    if shutil.which("actionlint"):
        al_res = subprocess.run(["actionlint", *[str(p) for p in wf_paths]], capture_output=True, text=True)
        assert al_res.returncode == 0, f"actionlint failed on .github/workflows:\n{al_res.stdout}\n{al_res.stderr}"

    forbidden_literals = (
        "cp.atobar" + ".org",
        "cp.karar" + ".asia",
        "karar" + ".asia",
        "arsenyspb/" + "atobar-flow",
        "arsenyspb/" + "atobar-init",
        "548573" + "046718",
        "kvmoyteikq" + "wjlywtsdit",
        "chernov-" + "dev-org",
        "varset-" + "KyzypmnkZzd5pZyq",
        "prj-" + "t7D12g942Vx6i4kS",
    )
    scan_roots = [
        REPO_ROOT / "contracts",
        REPO_ROOT / "control-panel",
        REPO_ROOT / "flow-engine",
        REPO_ROOT / "flow-agent",
        REPO_ROOT / "atobar-ops",
        REPO_ROOT / "tests",
        REPO_ROOT / ".github",
        REPO_ROOT / ".pi",
        REPO_ROOT / "infra",
        REPO_ROOT / "scripts",
        REPO_ROOT / "templates",
        REPO_ROOT / "docs",
        REPO_ROOT / "issues",
    ]
    root_files = [
        REPO_ROOT / ".env.sample",
        REPO_ROOT / "README.md",
        REPO_ROOT / "AI.md",
        REPO_ROOT / "AGENTS.md",
        REPO_ROOT / "JUMBO_STATUS.md",
        REPO_ROOT / "OWNER_STEPS.md",
    ]
    text_exts = {".py", ".yml", ".yaml", ".tf", ".hcl", ".tpl", ".sh", ".md", ".sample"}
    violations: list[str] = []
    paths_to_check = list(root_files)
    for root in scan_roots:
        if root.exists():
            paths_to_check.extend(sorted(root.rglob("*")))
    for path in paths_to_check:
        if not path.is_file() or (path.suffix not in text_exts and path.name != ".env.sample"):
            continue
        if ".terraform" in path.parts or "__pycache__" in path.parts:
            continue
        content = path.read_text(encoding="utf-8")
        for lit in forbidden_literals:
            if lit in content:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains forbidden literal {lit!r}")
    assert violations == [], "Forbidden hardcoded literals found:\n" + "\n".join(violations)


# --- J51: PRD, HLD & GitHub Documentation Mermaid / Link / Ownership Parity ---

_MERMAID_BLOCK_RE = re.compile(r"```mermaid[ \t]*\n(.*?)\n```", re.DOTALL)
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def test_j51_prd_and_hld_mermaid_and_table_ownership_parity() -> None:
    from atobar_contracts.wiki import validate_mermaid_diagram
    from tests.test_table_ownership import CP_OWNED_TABLES, FE_OWNED_TABLES

    prd_path = REPO_ROOT / "docs" / "PRD.md"
    hld_path = REPO_ROOT / "docs" / "HLD.md"
    assert prd_path.is_file(), "docs/PRD.md must exist"
    assert hld_path.is_file(), "docs/HLD.md must exist"

    prd_text = prd_path.read_text(encoding="utf-8")
    hld_text = hld_path.read_text(encoding="utf-8")
    readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Agentic Teams On BYOK And Repos" in readme_text
    assert "Agentic Teams On BYOK And Repos" in prd_text
    assert "Agentic Teams On BYOK And Repos" in hld_text

    for token in (
        "P1",
        "P2",
        "P3",
        "P4",
        "J1",
        "J2",
        "J3",
        "J4",
        "J5",
        "J6",
        "FR-1",
        "FR-7",
        "NFR-1",
        "NFR-4",
        "[AS-BUILT]",
        "[VISION]",
        "https://github.com/atobar-flow",
        "per-repo, per-role",
        "per-invoking-user",
    ):
        assert token in prd_text, f"docs/PRD.md missing required section/token {token!r}"

    for forbidden_econ in ("USD 5,000", "USD 15,000", "USD 45K", "USD 100K", "5K/mo", "15K/mo"):
        assert forbidden_econ not in prd_text, f"docs/PRD.md must not hardcode BYOK unit economics {forbidden_econ!r}"

    roadmap_text = (REPO_ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
    assert "per-invoking-user" in roadmap_text, "docs/ROADMAP.md must include per-invoking-user BYOK key management"

    for tbl in sorted(CP_OWNED_TABLES | FE_OWNED_TABLES):
        assert tbl in hld_text, f"docs/HLD.md missing owned table {tbl!r} from test_table_ownership.py"

    hld_mermaid_blocks = _MERMAID_BLOCK_RE.findall(hld_text)
    assert len(hld_mermaid_blocks) >= 5, f"Expected >= 5 Mermaid diagrams in docs/HLD.md, got {len(hld_mermaid_blocks)}"

    j51_docs = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "docs" / "README.md",
        prd_path,
        hld_path,
        REPO_ROOT / "docs" / "SWARM_ARCHITECTURE.md",
        REPO_ROOT / "docs" / "ONBOARDING.md",
        REPO_ROOT / "docs" / "ROADMAP.md",
        REPO_ROOT / "issues" / "J51-prd-hld-and-scrollytelling-reconciliation.md",
    ]
    for doc_path in j51_docs:
        content = doc_path.read_text(encoding="utf-8")
        assert "atobar.org" not in content, f"{doc_path} must reference https://github.com/atobar-flow, not atobar.org"
        for block in _MERMAID_BLOCK_RE.findall(content):
            validate_mermaid_diagram(block)
        for raw_target in _MD_LINK_RE.findall(content):
            target = raw_target.strip().split("#", 1)[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (doc_path.parent / target).resolve()
            assert resolved.exists(), f"Broken relative link {raw_target!r} in {doc_path.relative_to(REPO_ROOT)}"


