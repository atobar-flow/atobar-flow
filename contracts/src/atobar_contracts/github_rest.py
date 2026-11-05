"""Pure httpx GitHub REST client and protocol shared by Control Panel, Flow Engine, and Flow Agent."""

import logging
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Optional, Protocol, cast
from urllib.parse import quote

import httpx

from atobar_contracts.issue_states import FLOW_LABEL_CATALOG
from atobar_contracts.manifest import METRIC_KEY_RE

logger = logging.getLogger("atobar_contracts.github_rest")
_COLLAB_CACHE: dict[str, tuple[float, list[str]]] = {}
_COLLAB_CACHE_TTL_S = 60.0


@dataclass(frozen=True)
class SpotInstanceTags:
    """Canonical tag contract for EC2 Spot worker instances across launch and janitor sweeps."""

    managed: bool = False
    tenant: str = ""
    issue_id: int = 0
    pr_number: int = 0
    run_id: str = ""
    max_minutes: Optional[int] = None
    role: str = "developer"
    head_sha: str = ""

    def to_ec2_tags(self) -> list[dict[str, str]]:
        slug = self.tenant.replace("/", "__")
        eff_max = self.max_minutes if (self.max_minutes is not None and self.max_minutes > 0) else 60
        tags = [
            {"Key": "atobar:managed", "Value": "true" if self.managed else "false"},
            {"Key": "atobar:tenant", "Value": self.tenant},
            {"Key": "atobar:issue", "Value": str(self.issue_id)},
            {"Key": "atobar:pr", "Value": str(self.pr_number)},
            {"Key": "atobar:run_id", "Value": self.run_id},
            {"Key": "atobar:max_minutes", "Value": str(eff_max)},
            {"Key": "Role", "Value": self.role},
            {"Key": "Tenant", "Value": self.tenant},
            {"Key": "Pr", "Value": str(self.pr_number)},
            {"Key": "IssueId", "Value": str(self.issue_id)},
            {"Key": "Repository", "Value": self.tenant},
            {"Key": "Name", "Value": f"flow-{self.role}-{slug}-{self.pr_number}"},
        ]
        if self.head_sha:
            tags.append({"Key": "HeadSha", "Value": self.head_sha})
        return tags

    @classmethod
    def from_ec2_instance(cls, inst: dict[str, Any]) -> "SpotInstanceTags":
        raw_tags = inst.get("Tags")
        tag_map: dict[str, str] = {}
        if isinstance(raw_tags, list):
            tag_map = {
                str(t.get("Key", "")): str(t.get("Value", ""))
                for t in raw_tags
                if isinstance(t, dict) and t.get("Key") is not None
            }
        else:
            dict_tags = inst.get("tags")
            if isinstance(dict_tags, dict):
                tag_map = {str(k): str(v) for k, v in dict_tags.items()}

        managed = tag_map.get("atobar:managed") == "true"
        run_id = tag_map.get("atobar:run_id") or ""
        tenant = tag_map.get("atobar:tenant") or tag_map.get("Tenant") or tag_map.get("Repository") or ""
        issue_str = (
            tag_map.get("atobar:issue")
            or tag_map.get("IssueId")
            or tag_map.get("atobar:pr")
            or tag_map.get("Pr")
            or "0"
        )
        pr_str = (
            tag_map.get("atobar:pr")
            or tag_map.get("atobar:issue")
            or tag_map.get("Pr")
            or tag_map.get("IssueId")
            or "0"
        )
        issue_id = int(issue_str) if issue_str.isdigit() else 0
        pr_number = int(pr_str) if pr_str.isdigit() else 0
        raw_max = tag_map.get("atobar:max_minutes", "").strip()
        max_minutes = int(raw_max) if (raw_max.isdigit() and int(raw_max) > 0) else None
        role = tag_map.get("Role") or "developer"
        head_sha = tag_map.get("HeadSha") or ""
        return cls(
            managed=managed,
            tenant=tenant,
            issue_id=issue_id,
            pr_number=pr_number,
            run_id=run_id,
            max_minutes=max_minutes,
            role=role,
            head_sha=head_sha,
        )


class GitHubClient(Protocol):
    def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]: ...
    def get_pr_files(self, repo: str, pr_number: int) -> list[str]: ...
    def find_pr_for_sha(self, repo: str, sha: str) -> Optional[int]: ...
    def get_commit_sha(self, repo: str, ref: str) -> str: ...
    def get_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]: ...
    def get_comment(self, repo: str, pr_number: int, comment_id: int) -> Optional[dict[str, Any]]: ...
    def get_pr_review(self, repo: str, pr_number: int, review_id: int) -> Optional[dict[str, Any]]: ...
    def get_pr_review_comment(self, repo: str, pr_number: int, comment_id: int) -> Optional[dict[str, Any]]: ...
    def get_pr_reviews(self, repo: str, pr_number: int) -> list[dict[str, Any]]: ...
    def get_pr_review_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]: ...
    def list_open_prs(self, repo: str) -> list[dict[str, Any]]: ...
    def get_check_runs(self, repo: str, ref: str) -> list[dict[str, Any]]: ...
    def create_check_run(self, repo: str, name: str, head_sha: str, conclusion: str, output: dict[str, str]) -> None: ...
    def create_comment(self, repo: str, issue_number: int, body: str) -> Any: ...
    def update_comment(self, repo: str, comment_id: int, body: str) -> Any: ...
    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> Any: ...
    def add_comment_reaction(
        self, repo: str, comment_id: int, content: str = "eyes", *, is_review_comment: bool = False
    ) -> Any: ...
    def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]: ...
    def add_label(self, repo: str, issue_number: int, label: str) -> None: ...
    def remove_label(self, repo: str, issue_number: int, label: str) -> None: ...
    def is_valid_assignee(self, repo: str, login: str) -> bool: ...
    def add_assignees(self, repo: str, issue_number: int, assignees: list[str]) -> None: ...
    def remove_assignees(self, repo: str, issue_number: int, assignees: list[str]) -> None: ...
    def dispatch_flow_run(self, repo: str, payload: dict[str, Any]) -> None: ...


_SAFE_REF_RE = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9_./-]+$")


def launch_spot_instance(payload: dict[str, Any], ec2_client: Any = None) -> dict[str, Any]:
    """Launches a tagged EC2 Spot worker instance directly from Flow Engine / Control Panel."""
    import base64

    role = str(payload.get("role") or "developer")
    target_repo = str(payload.get("target_repo") or payload.get("tenant") or payload.get("repo") or "")
    issue_id = int(payload.get("issue_id") or payload.get("pr") or payload.get("issue_number") or 0)
    pr_number = int(payload.get("pr") or issue_id)
    head_sha = str(payload.get("head_sha") or "")
    label = str(payload.get("label") or "")
    engine_ref = str(payload.get("engine_ref") or os.environ.get("ENGINE_REF") or "main")
    target_ref = head_sha or str(payload.get("target_ref") or "") or "main"
    for name, ref in (("target_ref", target_ref), ("engine_ref", engine_ref)):
        if not _SAFE_REF_RE.fullmatch(ref):
            raise ValueError(f"invalid {name} {ref!r}")
    run_id = str(payload.get("run_id") or "")
    run_cred = str(payload.get("run_credential") or "")
    agent_runtime = str(payload.get("agent_runtime") or "nanobot")
    max_minutes = int(payload.get("spot_max_instance_minutes") or os.environ.get("SPOT_MAX_INSTANCE_MINUTES", "60"))
    from atobar_contracts.client_templates import resolve_cp_url

    cp_url = resolve_cp_url(str(payload.get("cp_url") or ""))
    lt_id = str(payload.get("launch_template_id") or os.environ.get("FLOW_LAUNCH_TEMPLATE_ID") or "")
    template_name = str(
        payload.get("template_name") or os.environ.get("FLOW_LAUNCH_TEMPLATE_NAME") or "flow-spot-sandbox-template"
    )

    if ec2_client is None:
        import boto3  # type: ignore[import-untyped]

        region = os.environ.get("AWS_REGION", "us-west-2")
        ec2_client = boto3.client("ec2", region_name=region)

    base_user_data = str(payload.get("base_user_data") or "")
    if not base_user_data and hasattr(ec2_client, "describe_launch_template_versions"):
        try:
            lt_query: dict[str, Any] = {"Versions": ["$Latest"]}
            if lt_id:
                lt_query["LaunchTemplateId"] = lt_id
            else:
                lt_query["LaunchTemplateName"] = template_name
            vers = ec2_client.describe_launch_template_versions(**lt_query).get("LaunchTemplateVersions") or []
            if vers:
                raw_b64 = (vers[0].get("LaunchTemplateData") or {}).get("UserData")
                if raw_b64 and raw_b64 != "None":
                    base_user_data = base64.b64decode(raw_b64).decode("utf-8")
        except Exception:
            base_user_data = ""

    header_lines = [
        "#!/bin/bash",
        f'export FLOW_ROLE="{role}"',
        f'export FLOW_TENANT="{target_repo}"',
        f'export FLOW_PR="{pr_number}"',
        f'export FLOW_HEAD_SHA="{head_sha}"',
        f'export TARGET_REPO="{target_repo}"',
        f'export ISSUE_ID="{issue_id}"',
        f'export LABEL_NAME="{label}"',
        f'export ENGINE_REF="{engine_ref}"',
        f'export TARGET_REF="{target_ref}"',
        f'export FLOW_RUN_ID="{run_id}"',
        f'export FLOW_RUN_CREDENTIAL="{run_cred}"',
        f'export CONTROL_PANEL_URL="{cp_url}"',
        f'export FLOW_AGENT_RUNTIME="{agent_runtime}"',
        'export DISPLAY=":0"',
    ]
    metric_key = str(payload.get("metric_key") or "")
    if metric_key:
        if not METRIC_KEY_RE.fullmatch(metric_key):
            raise ValueError(f"invalid metric_key {metric_key!r}")
        intent_b64 = base64.b64encode(str(payload.get("metric_intent") or "").encode("utf-8")).decode("ascii")
        header_lines += [f'export FLOW_METRIC_KEY="{metric_key}"', f'export FLOW_METRIC_INTENT_B64="{intent_b64}"']
    if payload.get("goal_mode"):
        plan_count = int(payload.get("plan_count") or 1)
        if plan_count < 1 or plan_count > 5:
            raise ValueError(f"invalid plan_count {plan_count!r}")
        goal_obj_raw = str(payload.get("goal_objective") or payload.get("issue_text") or "")
        goal_obj_b64 = base64.b64encode(goal_obj_raw.encode("utf-8")).decode("ascii")
        header_lines += [
            'export FLOW_GOAL_MODE="1"',
            f'export FLOW_PLAN_COUNT="{plan_count}"',
            f'export FLOW_GOAL_OBJECTIVE_B64="{goal_obj_b64}"',
        ]
    header_lines.append("")
    base_lines = base_user_data.splitlines()
    if base_lines and base_lines[0].startswith("#!"):
        base_lines = base_lines[1:]
    user_data_text = "\n".join(header_lines + base_lines) + "\n"
    user_data_b64 = base64.b64encode(user_data_text.encode("utf-8")).decode("ascii")

    tags = SpotInstanceTags(
        managed=True,
        tenant=target_repo,
        issue_id=issue_id,
        pr_number=pr_number,
        run_id=run_id,
        max_minutes=max_minutes,
        role=role,
        head_sha=head_sha,
    ).to_ec2_tags()

    lt_spec: dict[str, str] = (
        {"LaunchTemplateId": lt_id, "Version": "$Latest"}
        if lt_id
        else {"LaunchTemplateName": template_name, "Version": "$Latest"}
    )
    res = ec2_client.run_instances(
        LaunchTemplate=lt_spec,
        MinCount=1,
        MaxCount=1,
        UserData=user_data_b64,
        TagSpecifications=[{"ResourceType": "instance", "Tags": tags}],
    )
    instances = res.get("Instances") or []
    instance_id = str(instances[0].get("InstanceId", "")) if instances else ""
    return {"instance_id": instance_id, "tags": {t["Key"]: t["Value"] for t in tags}}


class RealGitHubClient:
    """Production GitHub client using personal access or installation token."""

    def __init__(self, token: str, base_url: str = "https://api.github.com") -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        resp = httpx.get(f"{self.base_url}/repos/{repo}/pulls/{pr_number}", headers=self.headers, timeout=15)
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())

    def get_pr_files(self, repo: str, pr_number: int) -> list[str]:
        resp = httpx.get(
            f"{self.base_url}/repos/{repo}/pulls/{pr_number}/files",
            headers=self.headers,
            params={"per_page": 100},
            timeout=15,
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
        return [f.get("filename", "") for f in data if f.get("filename")]

    def find_pr_for_sha(self, repo: str, sha: str) -> Optional[int]:
        """The open PR whose head is sha (the flow issue number is not a PR number)."""
        resp = httpx.get(f"{self.base_url}/repos/{repo}/commits/{sha}/pulls", headers=self.headers, timeout=15)
        resp.raise_for_status()
        for pr in resp.json():
            if pr.get("state") == "open" and (pr.get("head") or {}).get("sha") == sha:
                return int(pr["number"])
        return None

    def get_commit_sha(self, repo: str, ref: str) -> str:
        resp = httpx.get(f"{self.base_url}/repos/{repo}/commits/{ref}", headers=self.headers, timeout=15)
        resp.raise_for_status()
        return str(resp.json().get("sha", ""))

    def get_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, 11):
            resp = httpx.get(
                f"{self.base_url}/repos/{repo}/issues/{pr_number}/comments",
                headers=self.headers,
                params={"per_page": 100, "page": page},
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list):
                break
            out.extend(batch)
            if len(batch) < 100:
                break
        return out

    def get_comment(self, repo: str, pr_number: int, comment_id: int) -> Optional[dict[str, Any]]:
        resp = httpx.get(
            f"{self.base_url}/repos/{repo}/issues/comments/{int(comment_id)}",
            headers=self.headers,
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            return None
        issue_url = str(data.get("issue_url") or "").rstrip("/").lower()
        expected_suffix = f"/repos/{repo.lower()}/issues/{int(pr_number)}"
        if issue_url and not issue_url.endswith(expected_suffix):
            return None
        return data

    def get_check_runs(self, repo: str, ref: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, 21):
            resp = httpx.get(
                f"{self.base_url}/repos/{repo}/commits/{ref}/check-runs",
                headers=self.headers,
                params={"per_page": 100, "page": page},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("check_runs", []) if isinstance(data, dict) else []
            if not isinstance(batch, list):
                break
            out.extend(batch)
            if len(batch) < 100:
                break
        return out

    def create_check_run(self, repo: str, name: str, head_sha: str, conclusion: str, output: dict[str, str]) -> None:
        payload = {
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": output,
        }
        resp = httpx.post(
            f"{self.base_url}/repos/{repo}/check-runs",
            headers=self.headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()

    def create_comment(self, repo: str, issue_number: int, body: str) -> Optional[dict[str, Any]]:
        resp = httpx.post(
            f"{self.base_url}/repos/{repo}/issues/{issue_number}/comments",
            headers=self.headers,
            json={"body": body},
            timeout=15,
        )
        resp.raise_for_status()
        try:
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def update_comment(self, repo: str, comment_id: int, body: str) -> Optional[dict[str, Any]]:
        resp = httpx.patch(
            f"{self.base_url}/repos/{repo}/issues/comments/{comment_id}",
            headers=self.headers,
            json={"body": body},
            timeout=15,
        )
        resp.raise_for_status()
        try:
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def upsert_comment_by_marker(
        self, repo: str, issue_number: int, marker: str, body: str
    ) -> Optional[dict[str, Any]]:
        try:
            for c in reversed(self.get_comments(repo, issue_number) or []):
                if marker in str(c.get("body") or "") and c.get("id") is not None:
                    return self.update_comment(repo, int(c["id"]), body)
        except Exception as exc:
            logger.warning("Failed to search existing marked comment on %s#%s: %s", repo, issue_number, exc)
        return self.create_comment(repo, issue_number, body)

    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> Optional[dict[str, Any]]:
        return self.create_comment(repo, pr_number, body)

    def add_comment_reaction(
        self,
        repo: str,
        comment_id: int,
        content: str = "eyes",
        *,
        is_review_comment: bool = False,
    ) -> Optional[dict[str, Any]]:
        endpoint = (
            f"{self.base_url}/repos/{repo}/pulls/comments/{int(comment_id)}/reactions"
            if is_review_comment
            else f"{self.base_url}/repos/{repo}/issues/comments/{int(comment_id)}/reactions"
        )
        try:
            resp = httpx.post(
                endpoint,
                headers=self.headers,
                json={"content": content},
                timeout=10,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Failed to add reaction %s on %s comment %s (status %s)",
                    content,
                    repo,
                    comment_id,
                    resp.status_code,
                )
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("Failed to add reaction %s on %s comment %s: %s", content, repo, comment_id, exc)
            return None

    def get_pr_review(self, repo: str, pr_number: int, review_id: int) -> Optional[dict[str, Any]]:
        resp = httpx.get(
            f"{self.base_url}/repos/{repo}/pulls/{int(pr_number)}/reviews/{int(review_id)}",
            headers=self.headers,
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            return None
        pr_url = str(data.get("pull_request_url") or "").rstrip("/").lower()
        expected_suffix = f"/repos/{repo.lower()}/pulls/{int(pr_number)}"
        if pr_url and not pr_url.endswith(expected_suffix):
            return None
        return data

    def get_pr_review_comment(self, repo: str, pr_number: int, comment_id: int) -> Optional[dict[str, Any]]:
        resp = httpx.get(
            f"{self.base_url}/repos/{repo}/pulls/comments/{int(comment_id)}",
            headers=self.headers,
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            return None
        pr_url = str(data.get("pull_request_url") or "").rstrip("/").lower()
        expected_suffix = f"/repos/{repo.lower()}/pulls/{int(pr_number)}"
        if pr_url and not pr_url.endswith(expected_suffix):
            return None
        return data

    def get_pr_reviews(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, 11):
            resp = httpx.get(
                f"{self.base_url}/repos/{repo}/pulls/{pr_number}/reviews",
                headers=self.headers,
                params={"per_page": 100, "page": page},
                timeout=15,
            )
            if resp.status_code == 404:
                return out
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            out.extend(cast(list[dict[str, Any]], batch))
            if len(batch) < 100:
                break
        return out

    def get_pr_review_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, 11):
            resp = httpx.get(
                f"{self.base_url}/repos/{repo}/pulls/{pr_number}/comments",
                headers=self.headers,
                params={"per_page": 100, "page": page},
                timeout=15,
            )
            if resp.status_code == 404:
                return out
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            out.extend(cast(list[dict[str, Any]], batch))
            if len(batch) < 100:
                break
        return out

    def list_open_prs(self, repo: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, 11):
            resp = httpx.get(
                f"{self.base_url}/repos/{repo}/pulls",
                headers=self.headers,
                params={"state": "open", "per_page": 100, "page": page},
                timeout=15,
            )
            if resp.status_code == 404:
                return out
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            out.extend(cast(list[dict[str, Any]], batch))
            if len(batch) < 100:
                break
        return out

    def add_label(self, repo: str, issue_number: int, label: str) -> None:
        resp = httpx.post(
            f"{self.base_url}/repos/{repo}/issues/{issue_number}/labels",
            headers=self.headers,
            json={"labels": [label]},
            timeout=15,
        )
        resp.raise_for_status()

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        try:
            resp = httpx.delete(
                f"{self.base_url}/repos/{repo}/issues/{issue_number}/labels/{label}",
                headers=self.headers,
                timeout=15,
            )
            if resp.status_code != 404:
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("Failed to remove label %s on %s#%s: %s", label, repo, issue_number, exc)

    def seed_repository_labels(
        self, repo: str, labels: Iterable[dict[str, str]] = FLOW_LABEL_CATALOG
    ) -> None:
        for entry in labels:
            name = str(entry.get("name") or "").strip()
            color = str(entry.get("color") or "0e8a16").strip().lstrip("#")
            desc = str(entry.get("description") or "")[:100]
            if not name:
                continue
            try:
                resp = httpx.post(
                    f"{self.base_url}/repos/{repo}/labels",
                    headers=self.headers,
                    json={"name": name, "color": color, "description": desc},
                    timeout=15,
                )
                if resp.status_code == 422:
                    patch_resp = httpx.patch(
                        f"{self.base_url}/repos/{repo}/labels/{quote(name, safe='')}",
                        headers=self.headers,
                        json={"color": color, "description": desc},
                        timeout=15,
                    )
                    if patch_resp.status_code >= 400:
                        logger.warning(
                            "Failed to update label %s on %s (status %s)", name, repo, patch_resp.status_code
                        )
                elif resp.status_code >= 400:
                    logger.warning("Failed to seed label %s on %s (status %s)", name, repo, resp.status_code)
            except Exception as exc:
                logger.warning("Failed to seed label %s on %s: %s", name, repo, exc)

    def is_valid_assignee(self, repo: str, login: str) -> bool:
        clean = (login or "").strip()
        if not clean or clean.endswith("[bot]"):
            return False
        try:
            resp = httpx.get(
                f"{self.base_url}/repos/{repo}/assignees/{quote(clean, safe='')}",
                headers=self.headers,
                timeout=10,
            )
            return resp.status_code == 204
        except Exception as exc:
            logger.warning("Failed to verify assignee %s on %s: %s", clean, repo, exc)
            return False

    def list_collaborators(self, repo: str) -> list[str]:
        now = time.monotonic()
        cached = _COLLAB_CACHE.get(repo)
        if cached is not None and (now - cached[0]) < _COLLAB_CACHE_TTL_S:
            return list(cached[1])
        owner = repo.split("/", 1)[0]
        logins: list[str] = []
        try:
            for page in range(1, 6):
                resp = httpx.get(
                    f"{self.base_url}/repos/{repo}/collaborators",
                    headers=self.headers,
                    params={"per_page": 100, "page": page},
                    timeout=10,
                )
                if resp.status_code != 200 or not isinstance(resp.json(), list):
                    break
                batch = resp.json()
                for u in batch:
                    if isinstance(u, dict) and u.get("login"):
                        login_str = str(u["login"]).strip()
                        if login_str and not login_str.endswith("[bot]") and login_str not in logins:
                            logins.append(login_str)
                if len(batch) < 100:
                    break
            if logins:
                _COLLAB_CACHE[repo] = (now, list(logins))
                return logins
        except Exception as exc:
            logger.warning("Failed to list collaborators for %s: %s", repo, exc)
        return [owner] if owner else []

    def add_assignees(self, repo: str, issue_number: int, assignees: list[str]) -> None:
        clean = [a.strip() for a in assignees if a and a.strip()]
        if not clean or issue_number <= 0:
            return
        try:
            resp = httpx.post(
                f"{self.base_url}/repos/{repo}/issues/{issue_number}/assignees",
                headers=self.headers,
                json={"assignees": clean},
                timeout=15,
            )
            if resp.status_code in (404, 422):
                logger.warning(
                    "GitHub rejected add_assignees %s on %s#%s (status %s)",
                    clean,
                    repo,
                    issue_number,
                    resp.status_code,
                )
            else:
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Failed to add assignees %s on %s#%s: %s", clean, repo, issue_number, exc)

    def remove_assignees(self, repo: str, issue_number: int, assignees: list[str]) -> None:
        clean = [a.strip() for a in assignees if a and a.strip()]
        if not clean or issue_number <= 0:
            return
        try:
            resp = httpx.request(
                "DELETE",
                f"{self.base_url}/repos/{repo}/issues/{issue_number}/assignees",
                headers=self.headers,
                json={"assignees": clean},
                timeout=15,
            )
            if resp.status_code not in (404, 422):
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Failed to remove assignees %s on %s#%s: %s", clean, repo, issue_number, exc)

    def request_reviewers(self, repo: str, pr_number: int, reviewers: list[str]) -> None:
        clean = [r.strip() for r in reviewers if r and r.strip()]
        if not clean or pr_number <= 0:
            return
        try:
            resp = httpx.post(
                f"{self.base_url}/repos/{repo}/pulls/{pr_number}/requested_reviewers",
                headers=self.headers,
                json={"reviewers": clean},
                timeout=15,
            )
            if resp.status_code in (404, 422):
                logger.warning(
                    "GitHub rejected request_reviewers %s on %s PR #%s (status %s)",
                    clean,
                    repo,
                    pr_number,
                    resp.status_code,
                )
            else:
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Failed to request reviewers %s on %s PR #%s: %s", clean, repo, pr_number, exc)

    def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        resp = httpx.get(f"{self.base_url}/repos/{repo}/issues/{issue_number}", headers=self.headers, timeout=15)
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())

    def create_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        resp = httpx.post(
            f"{self.base_url}/repos/{repo}/issues",
            headers=self.headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())

    def list_open_issues(self, repo: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = httpx.get(
                f"{self.base_url}/repos/{repo}/issues",
                headers=self.headers,
                params={"state": "open", "per_page": 100, "page": page},
                timeout=15,
            )
            if resp.status_code == 404:
                return out
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            for item in data:
                if isinstance(item, dict) and "pull_request" not in item:
                    out.append(cast(dict[str, Any], item))
            if len(data) < 100:
                break
            page += 1
        return out

    def get_default_branch(self, repo: str) -> str:
        resp = httpx.get(f"{self.base_url}/repos/{repo}", headers=self.headers, timeout=15)
        resp.raise_for_status()
        default_branch = str((resp.json() or {}).get("default_branch") or "").strip()
        if not default_branch:
            raise RuntimeError(f"Repository {repo} response missing default_branch")
        return default_branch

    def list_branches(self, repo: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = httpx.get(
                f"{self.base_url}/repos/{repo}/branches",
                headers=self.headers,
                params={"per_page": 100, "page": page},
                timeout=15,
            )
            if resp.status_code == 404:
                return out
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            out.extend(cast(list[dict[str, Any]], data))
            if len(data) < 100:
                break
            page += 1
        return out

    def create_pull(self, repo: str, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        """Opens a PR; an already-open PR for the same head (422) is returned instead of failing."""
        resp = httpx.post(
            f"{self.base_url}/repos/{repo}/pulls",
            headers=self.headers,
            json={"title": title, "head": head, "base": base, "body": body},
            timeout=15,
        )
        if resp.status_code == 422:
            existing = [p for p in self.list_prs_for_branch(repo, head) if p.get("state") == "open"]
            if existing:
                return existing[0]
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())

    def list_prs_for_branch(self, repo: str, branch: str) -> list[dict[str, Any]]:
        owner = repo.split("/", 1)[0]
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = httpx.get(
                f"{self.base_url}/repos/{repo}/pulls",
                headers=self.headers,
                params={"state": "all", "head": f"{owner}:{branch}", "per_page": 100, "page": page},
                timeout=15,
            )
            if resp.status_code == 404:
                return out
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            out.extend(cast(list[dict[str, Any]], data))
            if len(data) < 100:
                break
            page += 1
        return out

    def compare_commits(self, repo: str, base: str, head: str) -> list[dict[str, Any]]:
        safe_base = quote(base, safe="/")
        safe_head = quote(head, safe="/")
        out: list[dict[str, Any]] = []
        total_commits = 0
        page = 1
        while True:
            resp = httpx.get(
                f"{self.base_url}/repos/{repo}/compare/{safe_base}...{safe_head}",
                headers=self.headers,
                params={"per_page": 100, "page": page},
                timeout=15,
            )
            if resp.status_code == 404:
                return out
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                break
            if page == 1:
                total_commits = int(data.get("total_commits") or 0)
            commits = data.get("commits", [])
            if not isinstance(commits, list) or not commits:
                break
            out.extend(cast(list[dict[str, Any]], commits))
            if len(commits) < 100 or (total_commits and len(out) >= total_commits):
                break
            page += 1
        if total_commits > len(out):
            raise RuntimeError(
                f"Commit comparison for {repo} {base}...{head} is truncated ({len(out)} < {total_commits})"
            )
        return out

    def delete_branch(self, repo: str, branch: str) -> None:
        safe_branch = quote(branch, safe="/")
        resp = httpx.delete(
            f"{self.base_url}/repos/{repo}/git/refs/heads/{safe_branch}",
            headers=self.headers,
            timeout=15,
        )
        if resp.status_code not in (204, 404, 422):
            resp.raise_for_status()

    def dispatch_flow_run(self, repo: str, payload: dict[str, Any]) -> None:
        event_type = payload.get("event_type", "flow_run") if isinstance(payload, dict) else "flow_run"
        fe_url = os.environ.get("FE_URL") or os.environ.get("FLOW_ENGINE_URL")
        if fe_url:
            fe_token = os.environ.get("FE_INTERNAL_TOKEN", "")
            resp = httpx.post(
                f"{fe_url.rstrip('/')}/internal/v1/events",
                headers={"Authorization": f"Bearer {fe_token}"},
                json={"tenant": repo, "event_type": event_type, "event": payload},
                timeout=15,
            )
            resp.raise_for_status()
            return

        if event_type == "flow_event":
            cp_url = os.environ.get("CONTROL_PANEL_URL") or os.environ.get("CP_PUBLIC_URL")
            run_id = os.environ.get("FLOW_RUN_ID")
            run_cred = os.environ.get("FLOW_RUN_CREDENTIAL")
            raw_evt = payload.get("event") if isinstance(payload, dict) else None
            evt: dict[str, Any] = raw_evt if isinstance(raw_evt, dict) else {}
            if cp_url and run_id and run_cred and evt.get("comment_body"):
                resp = httpx.post(
                    f"{cp_url.rstrip('/')}/v1/runs/{run_id}/turn-complete",
                    headers={"Authorization": f"Bearer {run_cred}"},
                    json={
                        "comment_body": evt.get("comment_body", ""),
                        "issue_number": evt.get("issue_number"),
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                return

        if event_type == "flow_run" and os.environ.get("FLOW_LAUNCH_TEMPLATE_ID"):
            launch_spot_instance(payload)
            return

        flow_repo = os.environ.get("FLOW_ENGINE_REPO") or os.environ.get("FLOW_FACTORY_REPO") or "atobar-flow/atobar-flow"
        resp = httpx.post(
            f"{self.base_url}/repos/{flow_repo}/dispatches",
            headers=self.headers,
            json={"event_type": event_type, "client_payload": payload},
            timeout=15,
        )
        resp.raise_for_status()
