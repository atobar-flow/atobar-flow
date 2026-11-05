"""Single-writer Supabase table ownership AST boundary guard (#301 WP-0.1)."""

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

CP_SRC = REPO_ROOT / "control-panel" / "src"
FE_SRC = REPO_ROOT / "flow-engine" / "src"

CP_OWNED_TABLES: frozenset[str] = frozenset(
    {
        "tenant_tokens",
        "tenant_llm_keys",
        "tenant_role_llm",
        "security_audit_log",
    }
)

FE_OWNED_TABLES: frozenset[str] = frozenset(
    {
        "tenants",
        "tenants_manifest_history",
        "metric_definitions",
        "metric_definitions_history",
        "metric_observations",
        "run_credentials",
        "sessions",
        "session_events",
        "run_events",
        "loop_rounds",
        "conductor_events",
        "arbitration_decisions",
        "repository_wikis",
        "tenant_goals",
    }
)

CP_OWNED_RPCS: frozenset[str] = frozenset()
FE_OWNED_RPCS: frozenset[str] = frozenset(
    {
        "approve_metric",
        "flow_claim_wiki_refresh",
        "flow_complete_wiki_refresh_run",
        "flow_record_default_branch_merge_for_wiki",
        "flow_update_wiki_policy",
        "flow_upsert_wiki_content",
        "goal_record_spend",
        "goal_record_merge",
    }
)

WRITE_METHODS: frozenset[str] = frozenset({"insert", "update", "upsert", "delete"})


def _resolve_table_in_expr(expr: ast.AST, var_tables: dict[str, str]) -> str | None:
    curr: ast.AST | None = expr
    while curr is not None:
        if isinstance(curr, ast.Call) and isinstance(curr.func, ast.Attribute):
            if (
                curr.func.attr == "table"
                and curr.args
                and isinstance(curr.args[0], ast.Constant)
                and isinstance(curr.args[0].value, str)
            ):
                return curr.args[0].value
            curr = curr.func.value
        elif isinstance(curr, ast.Attribute):
            curr = curr.value
        elif isinstance(curr, ast.Name):
            return var_tables.get(curr.id)
        else:
            break
    return None


def _scan_supabase_operations(
    src_root: pathlib.Path,
) -> tuple[list[tuple[str, int, str, str]], list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    """Return (table_writes, rpc_calls, all_table_refs) under src_root."""
    table_writes: list[tuple[str, int, str, str]] = []
    rpc_calls: list[tuple[str, int, str]] = []
    all_table_refs: list[tuple[str, int, str]] = []

    for py_file in sorted(src_root.rglob("*.py")):
        rel = str(py_file.relative_to(REPO_ROOT))
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))

        var_tables: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                t_name = _resolve_table_in_expr(node.value, var_tables)
                if t_name:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            var_tables[target.id] = t_name

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            attr = node.func.attr
            if (
                attr == "table"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                all_table_refs.append((rel, node.lineno, node.args[0].value))
            elif attr in WRITE_METHODS:
                tbl = _resolve_table_in_expr(node.func.value, var_tables)
                if tbl is not None:
                    table_writes.append((rel, node.lineno, attr, tbl))
            elif (
                attr == "rpc"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                rpc_calls.append((rel, node.lineno, node.args[0].value))

    return table_writes, rpc_calls, all_table_refs


def test_table_ownership_sets_are_disjoint() -> None:
    assert CP_OWNED_TABLES.isdisjoint(FE_OWNED_TABLES)
    assert CP_OWNED_RPCS.isdisjoint(FE_OWNED_RPCS)


def test_control_panel_only_writes_cp_owned_tables() -> None:
    writes, rpcs, _ = _scan_supabase_operations(CP_SRC)
    assert writes, "Expected at least one CP-owned table write in control-panel/src"
    violations: list[str] = []
    for rel, lineno, method, table in writes:
        if table in FE_OWNED_TABLES:
            violations.append(
                f"{rel}:{lineno} control-panel calls .{method}() on flow-engine owned table '{table}'"
            )
        elif table not in CP_OWNED_TABLES:
            violations.append(f"{rel}:{lineno} control-panel calls .{method}() on unowned table '{table}'")
    for rel, lineno, rpc_name in rpcs:
        if rpc_name in FE_OWNED_RPCS or rpc_name not in CP_OWNED_RPCS:
            violations.append(f"{rel}:{lineno} control-panel calls .rpc('{rpc_name}') owned by flow-engine")
    assert violations == [], "Control Panel single-writer violations:\n" + "\n".join(violations)


def test_flow_engine_only_writes_fe_owned_tables() -> None:
    writes, rpcs, all_refs = _scan_supabase_operations(FE_SRC)
    assert writes, "Expected at least one FE-owned table write in flow-engine/src"
    violations: list[str] = []
    for rel, lineno, method, table in writes:
        if table in CP_OWNED_TABLES:
            violations.append(
                f"{rel}:{lineno} flow-engine calls .{method}() on control-panel owned table '{table}'"
            )
        elif table not in FE_OWNED_TABLES:
            violations.append(f"{rel}:{lineno} flow-engine calls .{method}() on unowned table '{table}'")
    for rel, lineno, table in all_refs:
        if table in CP_OWNED_TABLES:
            violations.append(f"{rel}:{lineno} flow-engine accesses control-panel owned table '{table}'")
    for rel, lineno, rpc_name in rpcs:
        if rpc_name in CP_OWNED_RPCS or rpc_name not in FE_OWNED_RPCS:
            violations.append(f"{rel}:{lineno} flow-engine calls unowned .rpc('{rpc_name}')")
    assert violations == [], "Flow Engine single-writer violations:\n" + "\n".join(violations)
