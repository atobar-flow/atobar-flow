from types import SimpleNamespace
from typing import Any

from atobar_contracts.conductor_io import _human_assignee_for_tenant, resolve_human_assignee


class _Store:
    def __init__(self, manifest: dict[str, Any]) -> None:
        self._tenant = SimpleNamespace(manifest=manifest)

    def tenant_by_repo(self, repo: str) -> Any:
        return self._tenant


class _GH:
    def __init__(self, valid: set[str]) -> None:
        self.valid = valid
        self.checked: list[tuple[str, str]] = []

    def is_valid_assignee(self, repo: str, login: str) -> bool:
        self.checked.append((repo, login))
        return login in self.valid


def test_resolve_prefers_manifest_login_then_creator_then_repo_owner() -> None:
    assert resolve_human_assignee({"owner_github_login": "jane", "created_by": "bob"}, "acme/app") == "jane"
    assert resolve_human_assignee({"owner_github_login": "admin", "created_by": "bob"}, "acme/app") == "bob"
    assert resolve_human_assignee({}, "acme/app") == "acme"
    assert resolve_human_assignee(None, "no-slash") == ""


def test_org_owner_default_is_rejected_instead_of_silently_failing() -> None:
    gh = _GH(valid={"jane"})
    store = _Store({"owner_github_login": "acme-org"})  # org-owned repo: GitHub answers 404 for an org
    assert _human_assignee_for_tenant("acme-org/app", store, gh) == ""  # type: ignore[arg-type]
    assert gh.checked == [("acme-org/app", "acme-org")]


def test_valid_assignee_is_returned_and_unverifiable_gh_does_not_block() -> None:
    gh = _GH(valid={"jane"})
    assert _human_assignee_for_tenant("acme-org/app", _Store({"owner_github_login": "jane"}), gh) == "jane"  # type: ignore[arg-type]
    # clients without is_valid_assignee (older fakes) keep the resolved login
    assert _human_assignee_for_tenant("acme-org/app", _Store({"owner_github_login": "jane"}), object()) == "jane"  # type: ignore[arg-type]


def test_verification_error_keeps_login_so_transient_failures_do_not_drop_notifications() -> None:
    class _Boom:
        def is_valid_assignee(self, repo: str, login: str) -> bool:
            raise RuntimeError("network")

    assert _human_assignee_for_tenant("o/r", _Store({"owner_github_login": "jane"}), _Boom()) == "jane"  # type: ignore[arg-type]
