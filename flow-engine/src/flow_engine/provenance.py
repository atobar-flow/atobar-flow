"""GitHub-verified metric provenance and compiled metric command safety checks (#301 WP-2/WP-6)."""

import hashlib
import io
import json
import re
import zipfile
from typing import Any, Optional

from atobar_contracts.manifest import FORBIDDEN_CMD_PATTERNS, validate_compiled_metric_command

__all__ = ["FORBIDDEN_CMD_PATTERNS", "validate_compiled_metric_command", "verify_and_extract_metrics"]


MAX_METRIC_ARTIFACT_ZIP_BYTES = 1_048_576
MAX_METRIC_JSON_BYTES = 262_144
MAX_METRIC_ZIP_ENTRIES = 5


def verify_and_extract_metrics(
    *,
    repo: str,
    commit_sha: str,
    run_id: Optional[int],
    run_attempt: Optional[int],
    workflow_path: str,
    expected_workflow_path: str,
    gh: Any,
    pr_number: Optional[int] = None,
) -> tuple[dict[str, float], str, int]:
    """Verifies a GitHub Actions workflow run and extracts values from the flow-metrics zip artifact.

    Returns (values_dict, artifact_sha256, verified_run_attempt).
    Raises ValueError if any provenance check fails or if the run/artifact cannot be verified.
    """
    if run_id is None:
        raise ValueError("Missing run_id: unverified metric observations are rejected")
    if gh is None or not hasattr(gh, "get_workflow_run") or not hasattr(gh, "download_flow_metrics_artifact"):
        raise ValueError("GitHub provenance verifier unavailable: cannot verify workflow run or artifact")

    run_info = gh.get_workflow_run(repo, int(run_id))
    if not run_info or not isinstance(run_info, dict):
        raise ValueError(f"Workflow run {run_id} not found for {repo}")

    status = str(run_info.get("status", ""))
    conclusion = str(run_info.get("conclusion", ""))
    head_sha = str(run_info.get("head_sha", ""))
    pr_entries = [
        pr
        for pr in (run_info.get("pull_requests") or [])
        if isinstance(pr, dict)
    ]
    pr_head_by_num: dict[int, str] = {}
    for pr in pr_entries:
        num = pr.get("number")
        p_head = (pr.get("head") or {}).get("sha") if isinstance(pr.get("head"), dict) else None
        if num is not None and p_head:
            pr_head_by_num[int(num)] = str(p_head)

    run_path = str(run_info.get("path") or workflow_path)
    is_valid_in_progress = status == "in_progress" and conclusion in ("", "None", "null")
    is_valid_completed = status == "completed" and conclusion == "success"
    if not (is_valid_in_progress or is_valid_completed):
        raise ValueError(f"Workflow run {run_id} is not completed/success (got {status}/{conclusion})")

    if pr_entries and pr_number is not None:
        if int(pr_number) not in {int(pr["number"]) for pr in pr_entries if pr.get("number") is not None}:
            raise ValueError(f"Submitted pr_number {pr_number} is not associated with workflow run {run_id}")

    if commit_sha != head_sha:
        if pr_number is None or pr_head_by_num.get(int(pr_number)) != commit_sha:
            raise ValueError(
                f"Workflow run head_sha '{head_sha}' does not match commit_sha '{commit_sha}' for pr_number={pr_number}"
            )
    if expected_workflow_path and run_path != expected_workflow_path:
        raise ValueError(
            f"Workflow path '{run_path}' does not match manifest '{expected_workflow_path}'"
        )

    verified_attempt = int(run_info.get("run_attempt") or run_attempt or 1)
    zip_bytes = gh.download_flow_metrics_artifact(repo, int(run_id))
    if not zip_bytes:
        raise ValueError(f"flow-metrics artifact missing or empty for workflow run {run_id}")
    if len(zip_bytes) > MAX_METRIC_ARTIFACT_ZIP_BYTES:
        raise ValueError(
            f"flow-metrics artifact exceeds {MAX_METRIC_ARTIFACT_ZIP_BYTES} byte limit ({len(zip_bytes)} bytes)"
        )

    artifact_sha256 = hashlib.sha256(zip_bytes).hexdigest()
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            entries = zf.infolist()
            if len(entries) > MAX_METRIC_ZIP_ENTRIES:
                raise ValueError(
                    f"flow-metrics archive contains too many entries ({len(entries)} > {MAX_METRIC_ZIP_ENTRIES})"
                )
            try:
                info = zf.getinfo("flow-metrics.json")
            except KeyError as exc:
                raise ValueError("flow-metrics.json missing from artifact archive") from exc
            if info.file_size > MAX_METRIC_JSON_BYTES:
                raise ValueError(
                    f"flow-metrics.json uncompressed size ({info.file_size}) exceeds {MAX_METRIC_JSON_BYTES} byte limit"
                )
            with zf.open(info, "r") as fp:
                raw_bytes = fp.read(MAX_METRIC_JSON_BYTES + 1)
            if len(raw_bytes) > MAX_METRIC_JSON_BYTES:
                raise ValueError(
                    f"flow-metrics.json content exceeds {MAX_METRIC_JSON_BYTES} byte limit"
                )
            raw_json = raw_bytes.decode("utf-8")
        doc = json.loads(raw_json)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Invalid flow-metrics artifact archive for run {run_id}: {exc}") from exc

    artifact_sha = str(doc.get("commit_sha", ""))
    if artifact_sha != commit_sha:
        raise ValueError("Artifact flow-metrics.json commit_sha does not match commit_sha")
    if "pr_number" in doc:
        raw_doc_pr = doc["pr_number"]
        if raw_doc_pr is None:
            doc_pr: Optional[int] = None
        elif isinstance(raw_doc_pr, bool):
            raise ValueError("Invalid pr_number in artifact flow-metrics.json")
        else:
            try:
                doc_pr = int(raw_doc_pr)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid pr_number in artifact flow-metrics.json: {exc}") from exc
        sub_pr = int(pr_number) if pr_number is not None else None
        if doc_pr != sub_pr:
            raise ValueError(f"Submitted pr_number {sub_pr} does not match artifact pr_number {doc_pr}")

    raw_vals = doc.get("values")
    if not isinstance(raw_vals, dict) or not raw_vals:
        raise ValueError("Artifact flow-metrics.json contains no metric values")

    try:
        vals = {
            str(k): (
                (_ for _ in ()).throw(ValueError(f"Boolean metric value for '{k}'"))
                if isinstance(v, bool)
                else float(v)
            )
            for k, v in raw_vals.items()
        }
        raw_attempt = doc.get("run_attempt")
        if isinstance(raw_attempt, bool):
            raise ValueError("Boolean run_attempt in artifact")
        if raw_attempt is not None:
            parsed_attempt = int(raw_attempt)
            if parsed_attempt < 1:
                raise ValueError("run_attempt in artifact must be >= 1")
            final_attempt = parsed_attempt
        else:
            final_attempt = int(verified_attempt)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid metric values or run_attempt in artifact flow-metrics.json: {exc}") from exc
    return vals, artifact_sha256, final_attempt
