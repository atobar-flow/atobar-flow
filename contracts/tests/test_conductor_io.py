"""Unit & integration tests for Conductor PR-to-Issue resolution, @atobar-flow-agent steering, and serverless arbitrator claiming (J49 #365 WP-1/WP-2)."""

from typing import Any, Optional

from atobar_contracts.conductor_io import (
    _run_serverless_arbitrator,
    parse_human_comment_directive,
    process_flow_event,
    rebuild_loop_state,
    resolve_delegated_issue_number,
)
from atobar_contracts.constants import LOOP_EPOCH_MARKER
from atobar_contracts.envelopes import Verdict, render
from atobar_contracts.store import Tenant

SHA_PR = "9261fc09261fc09261fc09261fc09261fc09261f"

BOT_LOGINS = {
    "planner": "atobar-flow-agent[bot]",
    "developer": "atobar-flow-agent[bot]",
    "evaluator": "atobar-flow-agent[bot]",
    "arbitrator": "atobar-flow-agent[bot]",
    "conductor": "atobar-flow[bot]",
}


class FakeGitHub:
    def __init__(self) -> None:
        self.prs: dict[tuple[str, int], dict[str, Any]] = {}
        self.issues: dict[tuple[str, int], dict[str, Any]] = {}
        self.comments: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.reviews: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.review_comments: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.check_runs: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.dispatched_runs: list[dict[str, Any]] = []
        self.created_comments: list[dict[str, Any]] = []
        self.labels_added: list[dict[str, Any]] = []
        self.labels_removed: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []
        self._next_cid = 7000

    def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        return self.prs.get(
            (repo, pr_number),
            {"number": pr_number, "head": {"sha": ""}, "labels": []},
        )

    def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        return self.issues.get((repo, issue_number), self.get_pr(repo, issue_number))

    def get_pr_files(self, repo: str, pr_number: int) -> list[str]:
        return ["src/monitor_tui.py"]

    def find_pr_for_sha(self, repo: str, sha: str) -> Optional[int]:
        for (r, num), pr_data in self.prs.items():
            if r == repo and (pr_data.get("head") or {}).get("sha") == sha:
                return int(num)
        return None

    def list_open_prs(self, repo: str) -> list[dict[str, Any]]:
        return [p for (r, _), p in self.prs.items() if r == repo and p.get("state", "open") == "open"]

    def get_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return list(self.comments.get((repo, pr_number), []))

    def get_comment(self, repo: str, pr_number: int, comment_id: int) -> Optional[dict[str, Any]]:
        return next(
            (c for c in self.comments.get((repo, pr_number), []) if str(c.get("id")) == str(comment_id)),
            None,
        )

    def get_pr_review(self, repo: str, pr_number: int, review_id: int) -> Optional[dict[str, Any]]:
        return next(
            (r for r in self.reviews.get((repo, pr_number), []) if str(r.get("id")) == str(review_id)),
            None,
        )

    def get_pr_review_comment(self, repo: str, pr_number: int, comment_id: int) -> Optional[dict[str, Any]]:
        return next(
            (rc for rc in self.review_comments.get((repo, pr_number), []) if str(rc.get("id")) == str(comment_id)),
            None,
        )

    def get_pr_reviews(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return list(self.reviews.get((repo, pr_number), []))

    def get_pr_review_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return list(self.review_comments.get((repo, pr_number), []))

    def get_check_runs(self, repo: str, ref: str) -> list[dict[str, Any]]:
        return list(self.check_runs.get((repo, ref), []))

    def create_check_run(self, repo: str, name: str, head_sha: str, conclusion: str, output: dict[str, str]) -> None:
        self.check_runs.setdefault((repo, head_sha), []).append({"name": name, "conclusion": conclusion})

    def create_comment(self, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        self._next_cid += 1
        entry = {
            "id": self._next_cid,
            "repo": repo,
            "issue_number": issue_number,
            "body": body,
            "user": {"login": "atobar-flow-agent[bot]"},
            "created_at": "2026-11-01T12:00:00Z",
        }
        self.created_comments.append(entry)
        self.comments.setdefault((repo, issue_number), []).append(entry)
        return entry

    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> dict[str, Any]:
        return self.create_comment(repo, pr_number, body)

    def add_comment_reaction(
        self, repo: str, comment_id: int, content: str = "eyes", *, is_review_comment: bool = False
    ) -> dict[str, Any]:
        rec = {"repo": repo, "comment_id": comment_id, "content": content, "is_review_comment": is_review_comment}
        self.reactions.append(rec)
        return rec

    def add_label(self, repo: str, issue_number: int, label: str) -> None:
        self.labels_added.append({"repo": repo, "issue_number": issue_number, "label": label})

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        self.labels_removed.append({"repo": repo, "issue_number": issue_number, "label": label})

    def dispatch_flow_run(self, repo: str, payload: dict[str, Any]) -> None:
        self.dispatched_runs.append({"repo": repo, "payload": payload})


class FakeStore:
    def __init__(self, tenant: Tenant) -> None:
        self.tenant = tenant
        self.rounds: list[dict[str, Any]] = []
        self.processed_events: set[str] = set()
        self.run_creds: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.active_issues: set[int] = set()

    def tenant_by_repo(self, repo: str) -> Optional[Tenant]:
        return self.tenant if repo == self.tenant.repo else None

    def upsert_session(self, row: dict[str, Any]) -> None:
        inst_id = str(row.get("instance_id") or "")
        if inst_id:
            self.sessions[inst_id] = dict(row)

    def get_session_by_run(self, run_id: str) -> Optional[dict[str, Any]]:
        for s in self.sessions.values():
            if s.get("run_id") == run_id:
                return dict(s)
        return None

    def issue_session_active(self, tenant_id: str, issue_id: int, now: Any = None) -> bool:
        return int(issue_id) in self.active_issues

    def create_run_credential(
        self,
        run_id: str,
        tenant_id: str,
        role: str,
        cred_hash: str,
        expires_at: str,
        *,
        goal_mode: bool = False,
        plan_count: Optional[int] = None,
        metric_key: Optional[str] = None,
        head_sha: Optional[str] = None,
    ) -> None:
        self.run_creds[run_id] = {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "role": role,
            "cred_hash": cred_hash,
            "expires_at": expires_at,
            "head_sha": head_sha,
            "envelope_comment_id": None,
            "created_at": "2026-10-25T00:00:00+00:00",
        }

    def get_run_credential(self, run_id: str) -> Optional[dict[str, Any]]:
        rec = self.run_creds.get(run_id)
        return dict(rec) if rec is not None else None

    def claim_run_credential_envelope(self, run_id: str, comment_id: int) -> bool:
        rec = self.run_creds.get(run_id)
        if not rec or comment_id <= 0:
            return False
        existing = rec.get("envelope_comment_id")
        if existing is not None and int(existing) != int(comment_id):
            return False
        rec["envelope_comment_id"] = int(comment_id)
        return True

    def record_loop_round(self, row: dict[str, Any]) -> None:
        self.rounds.append(dict(row))

    def loop_rounds_for_pr(self, tenant_id: str, pr_number: int) -> list[dict[str, Any]]:
        return [r for r in self.rounds if r.get("tenant_id") == tenant_id and r.get("pr_number") == pr_number]

    def loop_rounds_for_tenant(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return [r for r in self.rounds if r.get("tenant_id") == tenant_id][:limit]

    def has_processed_event(self, tenant_id: str, event_id: str) -> bool:
        return f"{tenant_id}:{event_id}" in self.processed_events

    def record_processed_event(self, tenant_id: str, event_id: str, **kwargs: Any) -> None:
        self.processed_events.add(f"{tenant_id}:{event_id}")

    def record_goal_spend(self, tenant_id: str, delta_usd: float) -> None:
        pass

    def record_goal_merge(self, tenant_id: str, progress: bool) -> None:
        pass


def _make_setup() -> tuple[FakeGitHub, FakeStore, Tenant]:
    tenant = Tenant(
        id="t-ollama",
        repo="arsenyspb/ollama-monitor",
        status="active",
        arbitration_mode="recommend",
        manifest={
            "repo": "arsenyspb/ollama-monitor",
            "owner_github_login": "arsenyspb",
            "loop": {"max_review_rounds": 4, "max_pr_wall_clock_minutes": 30},
        },
    )
    return FakeGitHub(), FakeStore(tenant), tenant


def test_parse_human_comment_directive_variants() -> None:
    assert parse_human_comment_directive("@atobar-flow-agent refine the scope to TUI only", "issue") == "plan"
    assert parse_human_comment_directive("@atobar-flow-agent approve", "issue") == "approve"
    assert parse_human_comment_directive("@atobar-flow-agent approved", "issue") == "approve"
    # Common phrasing starting with "code" or "approve ..." must NOT misroute to approve:
    assert parse_human_comment_directive("@atobar-flow-agent code should use snake_case", "issue") == "plan"
    assert parse_human_comment_directive("@atobar-flow-agent approve only after adding tests", "issue") == "plan"
    # Broad triggers (/flow, @atobar) are ignored — only @atobar-flow-agent triggers:
    assert parse_human_comment_directive("@ATOBAR LGTM let's implement this", "issue") is None
    assert parse_human_comment_directive("/flow fix the sparkline scaling", "pr") is None
    assert parse_human_comment_directive("See flow-agent/src/flow_agent/main.py", "pr") is None
    # On PR: explicit eval / evaluate vs free text (e.g., "check the header alignment"):
    assert parse_human_comment_directive("@atobar-flow-agent eval please verify 0-TPS period", "pr") == "evaluate"
    assert parse_human_comment_directive("@atobar-flow-agent evaluate please verify 0-TPS period", "pr") == "evaluate"
    assert parse_human_comment_directive("@atobar-flow-agent check the header alignment", "pr") == "correct"
    assert parse_human_comment_directive("Plain comment without mention", "pr") is None


def test_issue_comments_dispatch_planner_and_developer() -> None:
    gh, store, tenant = _make_setup()

    # 1. Comment @atobar-flow-agent <plan adjustment> on Issue #53 -> dispatches planner
    gh.comments[(tenant.repo, 53)] = [
        {
            "id": 501,
            "body": "@atobar-flow-agent please include a --refresh-ms flag in the plan",
            "user": {"login": "arsenyspb"},
            "author_association": "OWNER",
        }
    ]
    res1 = process_flow_event(
        {
            "tenant": tenant.repo,
            "event_id": "evt-issue-plan-501",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 53,
                "comment_id": 501,
                "sender": "arsenyspb",
                "author_association": "OWNER",
                "is_pull_request": False,
            },
        },
        gh=gh,
        store=store,
        bot_logins=BOT_LOGINS,
    )
    assert res1["status"] == "success"
    assert res1["new_status"] == "in_plan"
    assert len(gh.dispatched_runs) == 1
    assert gh.dispatched_runs[-1]["payload"]["role"] == "planner"
    assert gh.dispatched_runs[-1]["payload"]["pr"] == 53
    assert any(r["comment_id"] == 501 and r["content"] == "eyes" for r in gh.reactions)

    # 2. Comment @atobar-flow-agent approved on Issue #53 -> dispatches developer
    gh.comments[(tenant.repo, 53)].append(
        {
            "id": 502,
            "body": "@atobar-flow-agent approved",
            "user": {"login": "arsenyspb"},
            "author_association": "OWNER",
        }
    )
    res2 = process_flow_event(
        {
            "tenant": tenant.repo,
            "event_id": "evt-issue-approve-502",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 53,
                "comment_id": 502,
                "sender": "arsenyspb",
                "author_association": "OWNER",
                "is_pull_request": False,
            },
        },
        gh=gh,
        store=store,
        bot_logins=BOT_LOGINS,
    )
    assert res2["status"] == "success"
    assert res2["new_status"] == "in_dev"
    assert gh.dispatched_runs[-1]["payload"]["role"] == "developer"
    assert gh.dispatched_runs[-1]["payload"]["pr"] == 53


def test_pr_comments_resolve_to_delegated_issue_and_dispatch_developer_or_evaluator() -> None:
    gh, store, tenant = _make_setup()
    # PR #54 is linked to Issue #53 via branch flow/issue-53 and body Fixes #53
    gh.prs[(tenant.repo, 54)] = {
        "number": 54,
        "head": {"ref": "flow/issue-53", "sha": SHA_PR},
        "body": "Automated PR.\n\nFixes #53",
        "labels": [],
    }
    assert resolve_delegated_issue_number(tenant.repo, {"pr_number": 54}, gh, store) == 53

    # 1. Comment @atobar-flow-agent <fix request> on PR #54 -> resolves to Issue #53, dispatches developer on SHA_PR
    gh.comments[(tenant.repo, 54)] = [
        {
            "id": 601,
            "body": "@atobar-flow-agent please replace the sinewave demo CSV with a multi-regime trace",
            "user": {"login": "arsenyspb"},
            "author_association": "OWNER",
        }
    ]
    res_fix = process_flow_event(
        {
            "tenant": tenant.repo,
            "event_id": "evt-pr-fix-601",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 54,
                "comment_id": 601,
                "sender": "arsenyspb",
                "author_association": "OWNER",
                "is_pull_request": True,
            },
        },
        gh=gh,
        store=store,
        bot_logins=BOT_LOGINS,
    )
    assert res_fix["status"] == "success"
    assert res_fix["new_status"] == "in_dev"
    assert gh.dispatched_runs[-1]["payload"]["role"] == "developer"
    assert gh.dispatched_runs[-1]["payload"]["pr"] == 53
    assert gh.dispatched_runs[-1]["payload"]["head_sha"] == SHA_PR

    # 2. Comment @atobar-flow-agent evaluate <check> on PR #54 -> dispatches evaluator on SHA_PR
    gh.comments[(tenant.repo, 54)].append(
        {
            "id": 602,
            "body": "@atobar-flow-agent evaluate please verify the TUI with an idle 0-TPS period and step burst",
            "user": {"login": "arsenyspb"},
            "author_association": "OWNER",
        }
    )
    res_eval = process_flow_event(
        {
            "tenant": tenant.repo,
            "event_id": "evt-pr-eval-602",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 54,
                "comment_id": 602,
                "sender": "arsenyspb",
                "author_association": "OWNER",
                "is_pull_request": True,
            },
        },
        gh=gh,
        store=store,
        bot_logins=BOT_LOGINS,
    )
    assert res_eval["status"] == "success"
    assert res_eval["new_status"] == "in_eval"
    assert gh.dispatched_runs[-1]["payload"]["role"] == "evaluator"
    assert gh.dispatched_runs[-1]["payload"]["pr"] == 53
    assert gh.dispatched_runs[-1]["payload"]["head_sha"] == SHA_PR


def test_comment_does_not_reset_budget_or_bypass_escalated_and_review_lookup_fails_closed() -> None:
    gh, store, tenant = _make_setup()
    # 1. Escalated issue #53 is NOT restarted or budget-zeroed by a comment (only flow:resume resets budget)
    gh.issues[(tenant.repo, 53)] = {
        "number": 53,
        "labels": [{"name": "flow:escalated"}],
    }
    gh.prs[(tenant.repo, 54)] = {
        "number": 54,
        "head": {"ref": "flow-53-sparkline", "sha": SHA_PR},
        "body": "Fixes #53",
        "labels": [],
    }
    store.record_loop_round(
        {
            "tenant_id": tenant.id,
            "pr_number": 53,
            "round": 1,
            "head_sha": SHA_PR,
            "role": "developer",
            "tokens": 1000,
            "cost_usd": 0.45,
            "wall_clock_minutes": 33.61,
            "created_at": "2026-11-01T10:00:00Z",
        }
    )
    gh.comments[(tenant.repo, 54)] = [
        {
            "id": 701,
            "body": "@atobar-flow-agent thanks!",
            "user": {"login": "arsenyspb"},
            "author_association": "OWNER",
        }
    ]

    res = process_flow_event(
        {
            "tenant": tenant.repo,
            "event_id": "evt-escalated-701",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 54,
                "comment_id": 701,
                "sender": "arsenyspb",
                "author_association": "OWNER",
                "is_pull_request": True,
                "delegated_issue_number": 53,
            },
        },
        gh=gh,
        store=store,
        bot_logins=BOT_LOGINS,
    )
    assert res["status"] == "success"
    assert res["new_status"] == "escalated"
    assert len(gh.dispatched_runs) == 0
    assert not any(LOOP_EPOCH_MARKER in c["body"] for c in gh.created_comments)

    # 2. Missing review on GitHub API lookup fails closed (never trusts webhook payload sender/author_association)
    res_missing_review = process_flow_event(
        {
            "tenant": tenant.repo,
            "event_id": "evt-missing-review-702",
            "event": {
                "kind": "pull_request_review.submitted",
                "issue_number": 54,
                "pr_number": 54,
                "comment_id": 99999,
                "comment_body": "@atobar-flow-agent fix this",
                "sender": "arsenyspb",
                "author_association": "OWNER",
                "is_pull_request": True,
                "delegated_issue_number": 53,
            },
        },
        gh=gh,
        store=store,
        bot_logins=BOT_LOGINS,
    )
    assert res_missing_review["status"] == "ignored"
    assert "not found on GitHub" in res_missing_review["reason"]
    assert len(gh.dispatched_runs) == 0


def test_queued_comment_posts_hourglass_reply_and_is_rechecked_on_convergence() -> None:
    gh, store, tenant = _make_setup()
    gh.prs[(tenant.repo, 53)] = {
        "number": 53,
        "head": {"sha": SHA_PR},
        "labels": [{"name": "flow:in-review"}],
    }
    gh.prs[(tenant.repo, 54)] = {
        "number": 54,
        "head": {"ref": "flow/issue-53", "sha": SHA_PR},
        "body": "Fixes #53",
        "labels": [],
    }
    # Active session on Issue #53
    store.active_issues.add(53)
    gh.comments[(tenant.repo, 54)] = [
        {
            "id": 750,
            "body": "@atobar-flow-agent please tighten the header padding",
            "user": {"login": "arsenyspb"},
            "author_association": "OWNER",
        }
    ]
    res_q = process_flow_event(
        {
            "tenant": tenant.repo,
            "event_id": "evt-queued-750",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 54,
                "comment_id": 750,
                "sender": "arsenyspb",
                "author_association": "OWNER",
                "is_pull_request": True,
                "delegated_issue_number": 53,
            },
        },
        gh=gh,
        store=store,
        bot_logins=BOT_LOGINS,
    )
    assert res_q["status"] == "queued"
    assert res_q["dispatched"] is False
    # Hourglass notice posted on Issue #53 (not eyes reaction)
    assert not any(r["comment_id"] == 750 for r in gh.reactions)
    assert any(
        c["issue_number"] == 53 and "⏳ **flow:**" in c["body"] and "flow:queued-directive:750:pr:correct" in c["body"]
        for c in gh.created_comments
    )

    # Active evaluator finishes and posts an approved verdict that would otherwise converge the loop:
    # Conductor re-checks the unconsumed queued directive and dispatches developer instead of dropping it!
    store.active_issues.discard(53)
    store.create_run_credential("run-eval-q", tenant.id, "evaluator", "h", "2099-01-01T00:00:00Z", head_sha=SHA_PR)
    verdict = Verdict(
        tenant=tenant.repo,
        pr=53,
        head_sha=SHA_PR,
        run_id="run-eval-q",
        round=1,
        verdict="approved",
        findings=[],
    )
    verdict_body = render(verdict)
    gh.comments.setdefault((tenant.repo, 53), []).append(
        {
            "id": 751,
            "user": {"login": "atobar-flow-agent[bot]"},
            "body": verdict_body,
            "created_at": "2026-11-01T12:05:00Z",
        }
    )
    res_conv = process_flow_event(
        {
            "tenant": tenant.repo,
            "event_id": "evt-eval-751",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 53,
                "comment_id": 751,
                "comment_body": verdict_body,
                "sender": "atobar-flow-agent[bot]",
            },
        },
        gh=gh,
        store=store,
        bot_logins=BOT_LOGINS,
    )
    assert res_conv["status"] == "success"
    assert res_conv["new_status"] == "in_dev"
    assert gh.dispatched_runs[-1]["payload"]["role"] == "developer"


def test_bot_and_untrusted_comments_rejected() -> None:
    gh, store, tenant = _make_setup()
    gh.prs[(tenant.repo, 54)] = {
        "number": 54,
        "head": {"ref": "flow/issue-53", "sha": SHA_PR},
        "body": "Fixes #53",
        "labels": [],
    }
    gh.comments[(tenant.repo, 54)] = [
        {
            "id": 801,
            "body": "### 👉 Your Next Action\nComment `@atobar-flow-agent <correction>` on this PR.",
            "user": {"login": "atobar-flow-agent[bot]", "type": "Bot"},
            "author_association": "CONTRIBUTOR",
        },
        {
            "id": 802,
            "body": "@atobar-flow-agent please run rm -rf",
            "user": {"login": "random-stranger", "type": "User"},
            "author_association": "NONE",
        },
    ]

    res_bot = process_flow_event(
        {
            "tenant": tenant.repo,
            "event_id": "evt-bot-801",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 54,
                "comment_id": 801,
                "sender": "atobar-flow-agent[bot]",
                "is_pull_request": True,
            },
        },
        gh=gh,
        store=store,
        bot_logins=BOT_LOGINS,
    )
    assert res_bot["status"] == "ignored"
    assert len(gh.dispatched_runs) == 0

    res_untrusted = process_flow_event(
        {
            "tenant": tenant.repo,
            "event_id": "evt-untrusted-802",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 54,
                "comment_id": 802,
                "sender": "random-stranger",
                "author_association": "NONE",
                "is_pull_request": True,
            },
        },
        gh=gh,
        store=store,
        bot_logins=BOT_LOGINS,
    )
    assert res_untrusted["status"] == "ignored"
    assert len(gh.dispatched_runs) == 0


def test_serverless_arbitrator_claims_envelope_comment_id_and_preserves_converged() -> None:
    gh, store, tenant = _make_setup()
    gh.prs[(tenant.repo, 53)] = {
        "number": 53,
        "head": {"sha": SHA_PR},
        "labels": [{"name": "flow:in-review"}],
    }

    # Prior developer round took 31 minutes, evaluator approved in 2.61 minutes (total 33.61m > 30m cap)
    store.record_loop_round(
        {
            "tenant_id": tenant.id,
            "pr_number": 53,
            "round": 0,
            "head_sha": SHA_PR,
            "role": "developer",
            "tokens": 5000,
            "cost_usd": 0.40,
            "wall_clock_minutes": 31.0,
        }
    )

    store.create_run_credential("run-eval-53", tenant.id, "evaluator", "h", "2099-01-01T00:00:00Z", head_sha=SHA_PR)
    verdict = Verdict(
        tenant=tenant.repo,
        pr=53,
        head_sha=SHA_PR,
        run_id="run-eval-53",
        round=1,
        verdict="approved",
        findings=[],
    )
    verdict_body = render(verdict)
    gh.comments[(tenant.repo, 53)] = [
        {
            "id": 901,
            "user": {"login": "atobar-flow-agent[bot]"},
            "body": verdict_body,
            "created_at": "2026-11-01T11:11:58Z",
        }
    ]

    # Evaluator verdict arrives -> triggers serverless arbitrator in-process
    res_eval = process_flow_event(
        {
            "tenant": tenant.repo,
            "event_id": "evt-eval-901",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 53,
                "comment_id": 901,
                "comment_body": verdict_body,
                "sender": "atobar-flow-agent[bot]",
                "cost_usd": 0.10,
                "wall_clock_minutes": 2.61,
            },
        },
        gh=gh,
        store=store,
        bot_logins=BOT_LOGINS,
    )
    assert res_eval["status"] == "success"
    assert res_eval["new_status"] == "converged"

    # Verify serverless arbitrator claimed its run_credential envelope_comment_id
    arb_creds = [c for c in store.run_creds.values() if c["role"] == "arbitrator"]
    assert len(arb_creds) == 1
    arb_comment = gh.created_comments[-1]
    assert arb_creds[0]["envelope_comment_id"] == arb_comment["id"]

    # Subsequent webhook for the flow-arbitration comment (or a check_run) arrives 19s later:
    # rebuild_loop_state MUST recognize latest_arbitration, keep status='converged', and NOT trip max_pr_wall_clock_minutes!
    rebuilt = rebuild_loop_state(
        tenant_repo=tenant.repo,
        pr_number=53,
        gh=gh,
        store=store,
        bot_logins=BOT_LOGINS,
        manifest=tenant.manifest,
    )
    assert rebuilt.latest_arbitration is not None
    assert rebuilt.status == "converged"

    res_arb_webhook = process_flow_event(
        {
            "tenant": tenant.repo,
            "event_id": "evt-arb-webhook",
            "event": {
                "kind": "issue_comment.created",
                "issue_number": 53,
                "comment_id": arb_comment["id"],
                "comment_body": arb_comment["body"],
                "sender": "atobar-flow-agent[bot]",
            },
        },
        gh=gh,
        store=store,
        bot_logins=BOT_LOGINS,
    )
    assert res_arb_webhook["status"] == "success"
    assert res_arb_webhook["new_status"] == "converged"
    assert not any(r["label"] == "flow:escalated" for r in gh.labels_added)
