"""Server-side GitHub App client. Client repos never hold a GitHub credential for atobar-flow."""

import logging
import os
import time
from collections.abc import Iterable
from functools import lru_cache
from typing import Any, Optional, Protocol
from urllib.parse import quote

import httpx
import jwt

from atobar_contracts.issue_states import FLOW_LABEL_CATALOG
from atobar_contracts.run_credentials import ROLE_GITHUB_PERMISSIONS

API = "https://api.github.com"
logger = logging.getLogger("control_panel.github")
_CP_COLLAB_CACHE: dict[str, tuple[float, list[str]]] = {}
_CP_COLLAB_CACHE_TTL_S = 60.0


class Dispatcher(Protocol):
    def get_collaborator_permission(self, repo: str, username: str) -> Optional[str]: ...

    def get_issue_body(self, repo: str, issue_number: int) -> str: ...

    def add_issue_label(self, repo: str, issue_number: int, label: str) -> None: ...

    def remove_issue_label(self, repo: str, issue_number: int, label: str) -> None: ...

    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> None: ...

    def comment_on_open_issue_by_label(self, repo: str, label: str, comment: str) -> bool: ...

    def mint_repo_token(self, repo: str, role: Optional[str] = None) -> dict[str, str]: ...


class GitHubAppDispatcher:
    def __init__(self, app_id: str, installation_id: str, private_key: str, factory_repo: str) -> None:
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key = private_key
        self._factory_repo = factory_repo
        self._token = ""
        self._token_exp = 0.0
        self._repo_token_cache: dict[tuple[str, Optional[str]], tuple[dict[str, str], float]] = {}

    def _jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self._app_id}, self._private_key, "RS256")

    def _installation_token(self) -> str:
        if self._token and time.time() < self._token_exp - 300:
            return self._token
        now = int(time.time())
        resp = httpx.post(
            f"{API}/app/installations/{self._installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {self._jwt()}", "Accept": "application/vnd.github+json"},
            json={"repositories": [self._factory_repo.split("/", 1)[1]]},
            timeout=15,
        )
        resp.raise_for_status()
        self._token = resp.json()["token"]
        self._token_exp = now + 3600
        return self._token

    def _token_for_repo(self, repo: str) -> str:
        return self.mint_repo_token(repo)["token"]

    def mint_repo_token(self, repo: str, role: Optional[str] = None) -> dict[str, str]:
        cache_key = (repo, role)
        now = time.time()
        cached = self._repo_token_cache.get(cache_key)
        if cached is not None and now < cached[1]:
            return dict(cached[0])

        resp = httpx.get(
            f"{API}/repos/{repo}/installation",
            headers={"Authorization": f"Bearer {self._jwt()}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if resp.status_code == 404:
            raise PermissionError("GitHub App not installed on repo")
        resp.raise_for_status()
        inst_json = resp.json()
        account_obj = inst_json.get("account") if isinstance(inst_json, dict) else None
        account_login = str((account_obj or {}).get("login") or "").strip().lower()
        expected_owner = repo.split("/", 1)[0].strip().lower()
        if not account_login or account_login != expected_owner:
            raise PermissionError(
                f"GitHub App installation account '{account_login}' does not match repo owner '{expected_owner}'"
            )
        inst_id = inst_json["id"]
        inst_perms: dict[str, Any] = (
            inst_json["permissions"] if isinstance(inst_json.get("permissions"), dict) else {}
        )
        repo_name = repo.split("/", 1)[-1]
        if role is not None:
            role_key = str(role).strip().lower()
            if role_key not in ROLE_GITHUB_PERMISSIONS:
                raise PermissionError(f"Unsupported run role for GitHub token minting: {role}")
            desired_perms: dict[str, str] = dict(ROLE_GITHUB_PERMISSIONS[role_key])
        else:
            desired_perms = {
                "contents": "write",
                "pull_requests": "write",
                "issues": "write",
                "checks": "write",
                "metadata": "read",
            }
        if inst_perms:
            desired_perms = {
                k: ("read" if inst_perms.get(k) == "read" else v)
                for k, v in desired_perms.items()
                if k in inst_perms
            }
        resp_tok = httpx.post(
            f"{API}/app/installations/{inst_id}/access_tokens",
            headers={"Authorization": f"Bearer {self._jwt()}", "Accept": "application/vnd.github+json"},
            json={
                "repositories": [repo_name],
                "permissions": desired_perms,
            },
            timeout=15,
        )
        resp_tok.raise_for_status()
        data = resp_tok.json()
        entry = {
            "token": str(data["token"]),
            "expires_at": str(data.get("expires_at") or ""),
        }
        self._repo_token_cache[cache_key] = (dict(entry), now + 3000.0)
        return entry

    def get_collaborator_permission(self, repo: str, username: str) -> Optional[str]:
        token = self._token_for_repo(repo)
        resp = httpx.get(
            f"{API}/repos/{repo}/collaborators/{username}/permission",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        res: Optional[str] = resp.json().get("permission")
        return res

    def is_valid_assignee(self, repo: str, login: str) -> bool:
        clean = (login or "").strip()
        if not clean or clean.endswith("[bot]"):
            return False
        try:
            token = self._token_for_repo(repo)
            resp = httpx.get(
                f"{API}/repos/{repo}/assignees/{quote(clean, safe='')}",
                headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
                timeout=10,
            )
            return resp.status_code == 204
        except Exception as exc:
            logger.warning("Failed to verify assignee %s on %s: %s", clean, repo, exc)
            return False

    def seed_repository_labels(
        self, repo: str, labels: Iterable[dict[str, str]] = FLOW_LABEL_CATALOG
    ) -> None:
        try:
            token = self._token_for_repo(repo)
        except Exception as exc:
            logger.warning("Failed to mint token for seeding labels on %s: %s", repo, exc)
            return
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
        for entry in labels:
            name = str(entry.get("name") or "").strip()
            color = str(entry.get("color") or "0e8a16").strip().lstrip("#")
            desc = str(entry.get("description") or "")[:100]
            if not name:
                continue
            try:
                resp = httpx.post(
                    f"{API}/repos/{repo}/labels",
                    headers=headers,
                    json={"name": name, "color": color, "description": desc},
                    timeout=15,
                )
                if resp.status_code == 422:
                    httpx.patch(
                        f"{API}/repos/{repo}/labels/{quote(name, safe='')}",
                        headers=headers,
                        json={"color": color, "description": desc},
                        timeout=15,
                    )
            except Exception as exc:
                logger.warning("Failed to seed label %s on %s: %s", name, repo, exc)

    def list_collaborators(self, repo: str) -> list[str]:
        now = time.monotonic()
        cached = _CP_COLLAB_CACHE.get(repo)
        if cached is not None and (now - cached[0]) < _CP_COLLAB_CACHE_TTL_S:
            return list(cached[1])
        owner = repo.split("/", 1)[0]
        logins: list[str] = []
        try:
            token = self._token_for_repo(repo)
            for page in range(1, 6):
                resp = httpx.get(
                    f"{API}/repos/{repo}/collaborators",
                    headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
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
                _CP_COLLAB_CACHE[repo] = (now, list(logins))
                return logins
        except Exception as exc:
            logger.warning("Failed to list collaborators for %s: %s", repo, exc)
        return [owner] if owner else []

    def add_assignees(self, repo: str, issue_number: int, assignees: list[str]) -> None:
        clean = [a.strip() for a in assignees if a and a.strip()]
        if not clean or issue_number <= 0:
            return
        token = self._token_for_repo(repo)
        resp = httpx.post(
            f"{API}/repos/{repo}/issues/{issue_number}/assignees",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            json={"assignees": clean},
            timeout=15,
        )
        if resp.status_code in (404, 422):
            logger.warning(
                "GitHub rejected add_assignees %s on %s#%s (status %s)", clean, repo, issue_number, resp.status_code
            )
        else:
            resp.raise_for_status()

    def remove_assignees(self, repo: str, issue_number: int, assignees: list[str]) -> None:
        clean = [a.strip() for a in assignees if a and a.strip()]
        if not clean or issue_number <= 0:
            return
        token = self._token_for_repo(repo)
        resp = httpx.request(
            "DELETE",
            f"{API}/repos/{repo}/issues/{issue_number}/assignees",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            json={"assignees": clean},
            timeout=15,
        )
        if resp.status_code not in (404, 422):
            resp.raise_for_status()

    def request_reviewers(self, repo: str, pr_number: int, reviewers: list[str]) -> None:
        clean = [r.strip() for r in reviewers if r and r.strip()]
        if not clean or pr_number <= 0:
            return
        token = self._token_for_repo(repo)
        resp = httpx.post(
            f"{API}/repos/{repo}/pulls/{pr_number}/requested_reviewers",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
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

    def get_issue_body(self, repo: str, issue_number: int) -> str:
        token = self._token_for_repo(repo)
        resp = httpx.get(
            f"{API}/repos/{repo}/issues/{issue_number}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if resp.status_code == 404:
            return ""
        resp.raise_for_status()
        data = resp.json()
        title = str(data.get("title") or "")
        body = str(data.get("body") or "")
        return f"{title}\n\n{body}".strip()

    def add_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        token = self._token_for_repo(repo)
        resp = httpx.post(
            f"{API}/repos/{repo}/issues/{issue_number}/labels",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            json={"labels": [label]},
            timeout=15,
        )
        resp.raise_for_status()

    def remove_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        token = self._token_for_repo(repo)
        resp = httpx.delete(
            f"{API}/repos/{repo}/issues/{issue_number}/labels/{label}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if resp.status_code != 404:
            resp.raise_for_status()

    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        token = self._token_for_repo(repo)
        resp = httpx.post(
            f"{API}/repos/{repo}/issues/{issue_number}/comments",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            json={"body": body},
            timeout=15,
        )
        resp.raise_for_status()

    def comment_on_open_issue_by_label(self, repo: str, label: str, comment: str) -> bool:
        token = self._token_for_repo(repo)
        resp = httpx.get(
            f"{API}/repos/{repo}/issues?state=open&labels={label}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if resp.status_code != 200 or not resp.json():
            return False
        issue_number = resp.json()[0]["number"]
        post_resp = httpx.post(
            f"{API}/repos/{repo}/issues/{issue_number}/comments",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            json={"body": comment},
            timeout=15,
        )
        post_resp.raise_for_status()
        return True


@lru_cache(maxsize=1)
def get_dispatcher() -> Optional[Dispatcher]:
    app_id = os.environ.get("GITHUB_APP_ID", "").strip()
    inst_id = os.environ.get("GITHUB_APP_INSTALLATION_ID", "").strip()
    priv_key = os.environ.get("GITHUB_APP_PRIVATE_KEY", "").strip()
    if not (app_id and inst_id and priv_key):
        return None
    return GitHubAppDispatcher(
        app_id=app_id,
        installation_id=inst_id,
        private_key=priv_key,
        factory_repo=os.environ.get("FLOW_FACTORY_REPO", "atobar-flow/atobar-flow"),
    )
