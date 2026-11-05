"""Control Panel persistence boundary: writes ONLY CP-owned tables (#301 single-writer rule)."""

import copy
import dataclasses
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Optional

from atobar_contracts.goal import TenantGoal
from atobar_contracts.manifest import TenantManifest, canonical_sha
from atobar_contracts.store import (
    pick_metric_compiler_session,
    SESSION_STALE_AFTER_MINUTES,
    Store,
    Tenant,
    default_manifest,
)
from atobar_contracts.wiki import normalize_wiki_row

__all__ = [
    "MemoryStore",
    "SESSION_STALE_AFTER_MINUTES",
    "Store",
    "SupabaseStore",
    "Tenant",
    "default_manifest",
    "get_store",
    "session_is_live",
]

_TERMINAL_STATUS_WORDS = ("complete", "halt", "fail", "done", "stop", "error", "dispatched", "terminat")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_uuid(val: str) -> bool:
    if not val:
        return False
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _tenant(row: dict[str, Any]) -> Tenant:
    manifest = dict(row.get("manifest") or {})
    if row.get("spot_max_instance_minutes") is not None and "spot_max_instance_minutes" not in manifest:
        manifest["spot_max_instance_minutes"] = int(row["spot_max_instance_minutes"])
    return Tenant(
        id=row["id"],
        repo=row["repo"],
        status=row["status"],
        arbitration_mode=row["arbitration_mode"],
        manifest=manifest,
        repository_id=row.get("repository_id") or manifest.get("repository_id"),
    )


def _validate_and_merge_goal_manifest(
    store: Any,
    tenant: Optional[Tenant],
    manifest: dict[str, Any],
    goal_dict: dict[str, Any],
) -> list[dict[str, Any]]:
    repo = str(manifest.get("repo") or (tenant.repo if tenant else "owner/repo"))
    owner = str(manifest.get("owner_github_login") or repo.split("/", 1)[0])
    default_branch = str(manifest.get("default_branch") or "main")
    metrics_list = list(manifest.get("metrics") or [])
    existing_keys = {str(m.get("key")) for m in metrics_list if isinstance(m, dict) and m.get("key")}
    if tenant is not None:
        for d in store.metric_definitions(tenant.id):
            k = str(d.get("key") or "")
            if k and d.get("status", "active") == "active" and k not in existing_keys:
                metrics_list.append(
                    {
                        "key": k,
                        "comparator": d.get("comparator") or ">=",
                        "threshold": float(d.get("threshold") if d.get("threshold") is not None else 1.0),
                        "baseline_mode": d.get("baseline_mode") or "absolute",
                        "min_samples": int(d.get("min_samples") or 1),
                    }
                )
                existing_keys.add(k)
    candidate = {
        "repo": repo,
        "default_branch": default_branch,
        "owner_github_login": owner,
        "metrics": metrics_list,
        "goal": goal_dict,
    }
    TenantManifest.model_validate(candidate)
    return metrics_list


class SupabaseStore:
    """Control Panel Supabase store: writes ONLY CP-owned tables; reads FE tables for auth/UI."""

    TENANT_COLS = "id,repo,status,arbitration_mode,manifest,repository_id,spot_max_instance_minutes"

    def __init__(self, url: str, service_key: str) -> None:
        self._url = url
        self._key = service_key
        self._client: Any = None

    @property
    def _db(self) -> Any:
        if self._client is None:
            from supabase import create_client

            self._client = create_client(self._url, self._key)
        return self._client

    def tenant_for_token_hash(self, token_hash: str) -> Optional[Tenant]:
        res = (
            self._db.table("tenant_tokens")
            .select("tenant_id")
            .eq("token_hash", token_hash)
            .is_("revoked_at", "null")
            .limit(1)
            .execute()
        )
        if not res.data or not res.data[0].get("tenant_id"):
            return None
        tenant_id = res.data[0]["tenant_id"]
        self._db.table("tenant_tokens").update({"last_used_at": _now()}).eq("token_hash", token_hash).execute()
        rows = self._db.table("tenants").select(self.TENANT_COLS).eq("id", tenant_id).limit(1).execute().data
        return _tenant(rows[0]) if rows else None

    def tenant_by_repo(self, repo: str) -> Optional[Tenant]:
        rows = self._db.table("tenants").select(self.TENANT_COLS).eq("repo", repo).limit(1).execute().data
        return _tenant(rows[0]) if rows else None

    def tenant_by_id(self, tenant_id: str) -> Optional[Tenant]:
        if not _is_uuid(tenant_id):
            return None
        rows = self._db.table("tenants").select(self.TENANT_COLS).eq("id", tenant_id).limit(1).execute().data
        return _tenant(rows[0]) if rows else None

    def tenant_for_repository_id(self, repository_id: str) -> Optional[Tenant]:
        if not repository_id:
            return None
        try:
            res = (
                self._db.table("tenants")
                .select(self.TENANT_COLS)
                .eq("repository_id", repository_id)
                .limit(1)
                .execute()
            )
            if res.data:
                return _tenant(res.data[0])
        except Exception:
            pass

        try:
            res = (
                self._db.table("tenants")
                .select(self.TENANT_COLS)
                .eq("manifest->>repository_id", repository_id)
                .limit(1)
                .execute()
            )
            if res.data:
                return _tenant(res.data[0])
        except Exception:
            pass
        return None

    def get_spot_max_minutes(self, tenant_id: str) -> int:
        if not _is_uuid(tenant_id):
            return 60
        try:
            rows = (
                self._db.table("tenants")
                .select("spot_max_instance_minutes,manifest")
                .eq("id", tenant_id)
                .limit(1)
                .execute()
                .data
            )
            if rows:
                val = rows[0].get("spot_max_instance_minutes")
                if val is not None:
                    return int(val)
                m = rows[0].get("manifest")
                if isinstance(m, dict) and m.get("spot_max_instance_minutes"):
                    return int(m["spot_max_instance_minutes"])
        except Exception:
            pass
        return 60

    def get_tenant_goal(self, tenant_id: str) -> Optional[dict[str, Any]]:
        if not _is_uuid(tenant_id):
            return None
        try:
            rows = (
                self._db.table("tenant_goals")
                .select("*")
                .eq("tenant_id", tenant_id)
                .limit(1)
                .execute()
                .data
            )
            return dict(rows[0]) if rows else None
        except Exception:
            return None

    def update_tenant_goal(self, tenant_id: str, patch: dict[str, Any]) -> Optional[dict[str, Any]]:
        raise NotImplementedError("Control Panel SupabaseStore is read-only for FE-owned tenant_goals")

    def set_tenant_goal(self, tenant_id: str, goal: Optional[dict[str, Any]]) -> dict[str, Any]:
        raise NotImplementedError("Control Panel SupabaseStore is read-only for FE-owned tenant_goals")

    def record_goal_spend(self, tenant_id: str, delta_usd: float) -> None:
        raise NotImplementedError("Control Panel SupabaseStore is read-only for FE-owned tenant_goals")

    def record_goal_merge(self, tenant_id: str, progress: bool) -> None:
        raise NotImplementedError("Control Panel SupabaseStore is read-only for FE-owned tenant_goals")

    def claim_goal_planning(
        self, tenant_id: str, run_id: str, now: Optional[datetime] = None, ttl_minutes: int = 15
    ) -> bool:
        raise NotImplementedError("Control Panel SupabaseStore is read-only for FE-owned tenant_goals")

    def clear_goal_planning(self, tenant_id: str, run_id: Optional[str] = None) -> None:
        raise NotImplementedError("Control Panel SupabaseStore is read-only for FE-owned tenant_goals")

    def wiki_for_tenant(self, tenant_id: str) -> Optional[dict[str, Any]]:
        rows = (
            self._db.table("repository_wikis")
            .select("*")
            .eq("tenant_id", tenant_id)
            .limit(1)
            .execute()
            .data
        )
        return normalize_wiki_row(dict(rows[0]), tenant_id=tenant_id) if rows else None

    def upsert_wiki(self, tenant_id: str, repo_name: str, wiki_row: dict[str, Any]) -> None:
        raise NotImplementedError("Control Panel SupabaseStore is read-only for FE-owned repository_wikis")

    def update_wiki_state(self, tenant_id: str, repo_name: str, patch: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Control Panel SupabaseStore is read-only for FE-owned repository_wikis")

    def record_wiki_default_branch_merge(self, tenant_id: str, repo_name: str, commit_sha: str) -> dict[str, Any]:
        raise NotImplementedError("Control Panel SupabaseStore is read-only for FE-owned repository_wikis")

    def claim_wiki_refresh(
        self,
        tenant_id: str,
        repo_name: str,
        run_id: str,
        *,
        now: Optional[datetime] = None,
        ttl_minutes: int = 15,
    ) -> dict[str, Any]:
        raise NotImplementedError("Control Panel SupabaseStore is read-only for FE-owned repository_wikis")

    def complete_wiki_refresh_run(
        self,
        tenant_id: str,
        repo_name: str,
        run_id: str,
        *,
        halted_error: Optional[str] = None,
    ) -> dict[str, Any]:
        raise NotImplementedError("Control Panel SupabaseStore is read-only for FE-owned repository_wikis")

    def add_token(self, tenant_id: str, token_hash: str, owner: str, label: str) -> None:
        self._db.table("tenant_tokens").insert(
            {"token_hash": token_hash, "tenant_id": tenant_id, "owner_github_login": owner, "label": label}
        ).execute()

    def revoke_tokens(self, tenant_id: str) -> int:
        res = (
            self._db.table("tenant_tokens")
            .update({"revoked_at": _now()})
            .eq("tenant_id", tenant_id)
            .is_("revoked_at", "null")
            .execute()
        )
        return len(res.data or [])

    def metric_keys(self, tenant_id: str) -> set[str]:
        rows = self._db.table("metric_definitions").select("key,status").eq("tenant_id", tenant_id).execute().data
        return {r["key"] for r in rows or [] if r.get("status", "active") == "active"}

    def observed_metric_keys(self, tenant_id: str) -> set[str]:
        rows = (
            self._db.table("metric_observations")
            .select("key")
            .eq("tenant_id", tenant_id)
            .is_("pr_number", "null")
            .execute()
            .data
        )
        return {r["key"] for r in rows or []}

    def list_tenants(self) -> list[Tenant]:
        rows = self._db.table("tenants").select(self.TENANT_COLS).order("repo").execute().data
        return [_tenant(r) for r in rows or []]

    def metric_definitions(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = self._db.table("metric_definitions").select("*").eq("tenant_id", tenant_id).order("key").execute().data
        return list(rows or [])

    def get_metric_definition(self, tenant_id: str, key: str) -> Optional[dict[str, Any]]:
        rows = (
            self._db.table("metric_definitions")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("key", key)
            .limit(1)
            .execute()
            .data
        )
        return dict(rows[0]) if rows else None

    def recent_observations(self, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = (
            self._db.table("metric_observations")
            .select(
                "key,value,commit_sha,pr_number,check_run_id,run_attempt,workflow_path,artifact_sha256,manifest_sha,observed_at"
            )
            .eq("tenant_id", tenant_id)
            .order("observed_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
        out: list[dict[str, Any]] = []
        for r in reversed(rows or []):
            item = dict(r)
            item.setdefault("run_id", item.get("check_run_id"))
            out.append(item)
        return out

    def default_branch_observations(self, tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = (
            self._db.table("metric_observations")
            .select(
                "key,value,commit_sha,pr_number,check_run_id,run_attempt,workflow_path,artifact_sha256,manifest_sha,observed_at"
            )
            .eq("tenant_id", tenant_id)
            .is_("pr_number", "null")
            .order("observed_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
        out: list[dict[str, Any]] = []
        for r in reversed(rows or []):
            item = dict(r)
            item.setdefault("run_id", item.get("check_run_id"))
            out.append(item)
        return out

    def active_tokens(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = (
            self._db.table("tenant_tokens")
            .select("label,owner_github_login,created_at,last_used_at")
            .eq("tenant_id", tenant_id)
            .is_("revoked_at", "null")
            .order("created_at", desc=True)
            .execute()
            .data
        )
        return list(rows or [])

    def llm_keys(self, tenant_id: str) -> list[dict[str, Any]]:
        """Metadata only; ciphertext is never selected for display."""
        rows = (
            self._db.table("tenant_llm_keys")
            .select("provider,key_last4,available_models,validated_at,created_by,updated_at")
            .eq("tenant_id", tenant_id)
            .order("provider")
            .execute()
            .data
        )
        return list(rows or [])

    def role_llm_prefs(self, tenant_id: str) -> dict[str, dict[str, Any]]:
        rows = (
            self._db.table("tenant_role_llm")
            .select("role,provider,model,reasoning_effort")
            .eq("tenant_id", tenant_id)
            .execute()
            .data
        )
        return {r["role"]: r for r in rows or []}

    def set_role_llm_pref(
        self, tenant_id: str, role: str, provider: str, model: str, reasoning_effort: str
    ) -> None:
        self._db.table("tenant_role_llm").upsert(
            {
                "tenant_id": tenant_id,
                "role": role,
                "provider": provider,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "updated_at": _now(),
            }
        ).execute()

    def put_llm_key(self, tenant_id: str, provider: str, row: dict[str, Any]) -> None:
        self._db.table("tenant_llm_keys").upsert(
            {**row, "tenant_id": tenant_id, "provider": provider, "updated_at": _now()}
        ).execute()

    def delete_llm_key(self, tenant_id: str, provider: str) -> None:
        self._db.table("tenant_llm_keys").delete().eq("tenant_id", tenant_id).eq("provider", provider).execute()

    def loop_rounds_for_pr(self, tenant_id: str, pr_number: int) -> list[dict[str, Any]]:
        rows = (
            self._db.table("loop_rounds")
            .select(
                "tenant_id,pr_number,round,head_sha,role,envelope_digest,tokens,cost_usd,usage_detail,wall_clock_minutes,created_at"
            )
            .eq("tenant_id", tenant_id)
            .eq("pr_number", pr_number)
            .order("created_at")
            .execute()
            .data
        )
        return list(rows or [])

    def loop_rounds_for_tenant(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = (
            self._db.table("loop_rounds")
            .select(
                "tenant_id,pr_number,round,head_sha,role,envelope_digest,tokens,cost_usd,usage_detail,wall_clock_minutes,created_at"
            )
            .eq("tenant_id", tenant_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
        return list(rows or [])

    def has_processed_event(self, tenant_id: str, event_id: str) -> bool:
        res = (
            self._db.table("conductor_events")
            .select("event_id")
            .eq("tenant_id", tenant_id)
            .eq("event_id", event_id)
            .limit(1)
            .execute()
        )
        return bool(res.data)

    def get_tenant_llm_ciphertext(self, tenant_id: str, provider: str) -> Optional[str]:
        rows = (
            self._db.table("tenant_llm_keys")
            .select("ciphertext")
            .eq("tenant_id", tenant_id)
            .eq("provider", provider)
            .limit(1)
            .execute()
            .data
        )
        return rows[0]["ciphertext"] if rows else None

    def get_tenant_llm_key_record(self, tenant_id: str, provider: str) -> Optional[dict[str, Any]]:
        try:
            rows = (
                self._db.table("tenant_llm_keys")
                .select("ciphertext,wrapped_dek,kek_version,kms_key_id,encryption_mode")
                .eq("tenant_id", tenant_id)
                .eq("provider", provider)
                .limit(1)
                .execute()
                .data
            )
            return dict(rows[0]) if rows else None
        except Exception:
            ct = self.get_tenant_llm_ciphertext(tenant_id, provider)
            return {"ciphertext": ct, "kek_version": 1} if ct else None

    def get_run_credential(self, run_id: str) -> Optional[dict[str, Any]]:
        rows = self._db.table("run_credentials").select("*").eq("run_id", run_id).limit(1).execute().data
        return rows[0] if rows else None

    def record_security_audit(
        self,
        event_type: str,
        *,
        tenant_id: Optional[str] = None,
        actor: str = "system",
        run_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        try:
            self._db.table("security_audit_log").insert(
                {
                    "tenant_id": tenant_id,
                    "event_type": event_type,
                    "actor": actor,
                    "run_id": run_id,
                    "metadata": metadata or {},
                }
            ).execute()
        except Exception:
            pass

    def active_sessions(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        current_time = now or datetime.now(timezone.utc)
        cutoff = (current_time - timedelta(minutes=15)).isoformat()
        res = (
            self._db.table("sessions")
            .select("*")
            .in_("status", ["Running", "running", "active", "pending"])
            .lt("heartbeat_at", cutoff)
            .execute()
        )
        return list(res.data or [])

    def list_open_sessions(self) -> list[dict[str, Any]]:
        res = (
            self._db.table("sessions")
            .select("*")
            .in_("status", ["Running", "running", "active", "pending"])
            .execute()
        )
        return list(res.data or [])

    def get_session_by_instance(self, instance_id: str) -> Optional[dict[str, Any]]:
        if not instance_id:
            return None
        rows = (
            self._db.table("sessions")
            .select("*")
            .eq("instance_id", instance_id)
            .limit(1)
            .execute()
            .data
        )
        return rows[0] if rows else None

    def run_events_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = (
            self._db.table("run_events")
            .select("run_id,seq,kind,ts,payload")
            .eq("run_id", run_id)
            .order("seq")
            .execute()
            .data
        )
        return list(rows or [])

    def get_session_by_run(self, run_id: str) -> Optional[dict[str, Any]]:
        rows = (
            self._db.table("sessions")
            .select("*")
            .eq("run_id", run_id)
            .order("heartbeat_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        return rows[0] if rows else None

    def latest_metric_compiler_session(self, tenant_id: str, key: str) -> Optional[dict[str, Any]]:
        rows = (
            self._db.table("sessions")
            .select("*")
            .eq("tenant_id", tenant_id)
            .or_(f"current_todo.like.compile:{key}*,run_id.like.*__{key}")
            .order("heartbeat_at", desc=True)
            .limit(20)
            .execute()
            .data
        )
        return pick_metric_compiler_session(rows or [], tenant_id, key)

    def issue_session_active(self, tenant_id: str, issue_id: int, now: Optional[datetime] = None) -> bool:
        rows = (
            self._db.table("sessions")
            .select("status,heartbeat_at,started_at")
            .eq("tenant_id", tenant_id)
            .eq("issue_id", issue_id)
            .execute()
            .data
        )
        current = now or datetime.now(timezone.utc)
        return any(session_is_live(r, current) for r in rows or [])

    def active_session_for_role(
        self,
        tenant_id: str,
        role: str,
        *,
        issue_id: Optional[int] = None,
        metric_key: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[dict[str, Any]]:
        q = self._db.table("sessions").select("*").eq("tenant_id", tenant_id).eq("role", role)
        if issue_id is not None:
            q = q.eq("issue_id", int(issue_id))
        rows = list(q.order("heartbeat_at", desc=True).execute().data or [])
        current = now or datetime.now(timezone.utc)
        for r in rows:
            if not session_is_live(r, current):
                continue
            if metric_key is not None:
                rid = str(r.get("run_id") or "")
                todo = str(r.get("current_todo") or "")
                bound = str(
                    r.get("metric_key")
                    or (rid.split("__", 1)[1] if "__" in rid else "")
                    or (todo.partition(":")[2].strip() if todo.startswith("compile:") else "")
                ).strip()
                if bound != metric_key:
                    continue
            return dict(r)
        return None


def session_is_live(row: dict[str, Any], now: datetime) -> bool:
    """A run on an issue is live while its status is not terminal and its heartbeat is fresh."""
    status = str(row.get("status") or "").lower()
    if any(w in status for w in _TERMINAL_STATUS_WORDS):
        return False
    hb = row.get("heartbeat_at") or row.get("started_at")
    if not hb:
        return False
    try:
        hb_dt = hb if isinstance(hb, datetime) else datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
    except ValueError:
        return False
    if hb_dt.tzinfo is None:
        hb_dt = hb_dt.replace(tzinfo=timezone.utc)
    return hb_dt >= now - timedelta(minutes=SESSION_STALE_AFTER_MINUTES)


class MemoryStore:
    """In-memory store for tests and hermetic local runtime."""

    def __init__(self) -> None:
        self.tenants: dict[str, Tenant] = {}
        self.spot_max_minutes: dict[str, int] = {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.metric_defs: dict[str, set[str]] = {}
        self.metric_def_rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.metric_definitions_history: list[dict[str, Any]] = []
        self.tenants_manifest_history: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []
        self.wikis: dict[str, dict[str, Any]] = {}
        self.llm_key_data: dict[tuple[str, str], dict[str, Any]] = {}
        self.role_llm: dict[tuple[str, str], dict[str, Any]] = {}
        self.run_creds: dict[str, dict[str, Any]] = {}
        self.events: set[tuple[str, str]] = set()
        self.loop_rounds: list[dict[str, Any]] = []
        self.sessions: dict[str, dict[str, Any]] = {}
        self.security_audit_log: list[dict[str, Any]] = []
        self.run_events: list[dict[str, Any]] = []
        self.arbitration_decisions: list[dict[str, Any]] = []
        self.tenant_goals: dict[str, dict[str, Any]] = {}
        self._claim_lock = threading.Lock()

    def tenant_for_token_hash(self, token_hash: str) -> Optional[Tenant]:
        row = self.tokens.get(token_hash)
        t = self.tenants.get(row["tenant_id"]) if row and not row.get("revoked") else None
        return copy.deepcopy(t) if t is not None else None

    def tenant_by_repo(self, repo: str) -> Optional[Tenant]:
        t = next((t for t in self.tenants.values() if t.repo == repo), None)
        return copy.deepcopy(t) if t is not None else None

    def tenant_by_id(self, tenant_id: str) -> Optional[Tenant]:
        t = self.tenants.get(tenant_id)
        return copy.deepcopy(t) if t is not None else None

    def tenant_for_repository_id(self, repository_id: str) -> Optional[Tenant]:
        if not repository_id:
            return None
        for t in self.tenants.values():
            if t.repository_id == repository_id:
                return copy.deepcopy(t)
            if str(t.manifest.get("repository_id", "")) == repository_id:
                return copy.deepcopy(t)
        return None

    def wiki_for_tenant(self, tenant_id: str) -> Optional[dict[str, Any]]:
        w = self.wikis.get(tenant_id)
        if w is None:
            return None
        return copy.deepcopy(normalize_wiki_row(w, tenant_id=tenant_id))

    def upsert_wiki(self, tenant_id: str, repo_name: str, wiki_row: dict[str, Any]) -> None:
        with self._claim_lock:
            existing = self.wikis.get(tenant_id) or {}
            queued = bool(existing.get("refresh_queued", False))
            active_run = existing.get("refresh_run_id")
            active_started = existing.get("refresh_started_at")
            interval = int(existing.get("refresh_interval_merges", 5))
            commit_sha = str(wiki_row.get("commit_sha") or existing.get("commit_sha") or "")
            prev_obs = str(existing.get("last_observed_sha") or "")
            if not prev_obs or prev_obs == commit_sha or not commit_sha:
                new_obs = commit_sha or prev_obs
                new_merges = 0
            else:
                new_obs = prev_obs
                new_merges = int(existing.get("merges_since_refresh") or 0)
            status_val = (
                "queued"
                if queued
                else (str(existing.get("refresh_status") or "running") if active_run else str(wiki_row.get("refresh_status") or "idle"))
            )
            self.wikis[tenant_id] = normalize_wiki_row(
                {
                    "tenant_id": tenant_id,
                    "repo_name": repo_name,
                    "purpose": wiki_row.get("purpose", ""),
                    "technologies": wiki_row.get("technologies") or wiki_row.get("key_technologies") or [],
                    "structure": wiki_row.get("structure") or wiki_row.get("top_level_structure") or "",
                    "concepts": wiki_row.get("concepts") or wiki_row.get("key_concepts") or [],
                    "architecture_mermaid": str(
                        wiki_row.get("architecture_mermaid") or existing.get("architecture_mermaid") or ""
                    ),
                    "commit_sha": commit_sha,
                    "last_observed_sha": new_obs,
                    "refresh_status": status_val,
                    "refresh_queued": queued,
                    "refresh_run_id": active_run,
                    "refresh_started_at": active_started,
                    "last_error": None,
                    "merges_since_refresh": new_merges,
                    "refresh_interval_merges": interval,
                    "last_updated": _now(),
                },
                tenant_id=tenant_id,
                repo_name=repo_name,
            )

    def update_wiki_state(self, tenant_id: str, repo_name: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._claim_lock:
            existing = self.wikis.get(tenant_id) or {}
            base = normalize_wiki_row(existing, tenant_id=tenant_id, repo_name=repo_name)
            merged = normalize_wiki_row(
                {
                    **base,
                    **patch,
                    "tenant_id": tenant_id,
                    "repo_name": repo_name or base.get("repo_name") or "",
                    "last_updated": base.get("last_updated") or _now(),
                },
                tenant_id=tenant_id,
                repo_name=repo_name,
            )
            self.wikis[tenant_id] = merged
            return copy.deepcopy(merged)

    def record_wiki_default_branch_merge(self, tenant_id: str, repo_name: str, commit_sha: str) -> dict[str, Any]:
        with self._claim_lock:
            existing = self.wikis.get(tenant_id) or {}
            base = normalize_wiki_row(existing, tenant_id=tenant_id, repo_name=repo_name)
            prev_sha = str(base.get("last_observed_sha") or base.get("commit_sha") or "")
            merges = int(base.get("merges_since_refresh") or 0)
            if commit_sha and commit_sha != prev_sha and commit_sha != str(base.get("commit_sha") or ""):
                merges += 1
            if commit_sha:
                base["last_observed_sha"] = commit_sha
            base["merges_since_refresh"] = merges
            if not base.get("last_updated"):
                base["last_updated"] = _now()
            self.wikis[tenant_id] = base
            return copy.deepcopy(base)

    def claim_wiki_refresh(
        self,
        tenant_id: str,
        repo_name: str,
        run_id: str,
        *,
        now: Optional[datetime] = None,
        ttl_minutes: int = 15,
    ) -> dict[str, Any]:
        now_dt = now or datetime.now(timezone.utc)
        with self._claim_lock:
            existing = self.wikis.get(tenant_id) or {}
            base = normalize_wiki_row(existing, tenant_id=tenant_id, repo_name=repo_name)
            cur_run = str(base.get("refresh_run_id") or "")
            cur_started = base.get("refresh_started_at")
            active_run = ""
            if cur_run and cur_run != run_id and cur_started:
                try:
                    st_dt = datetime.fromisoformat(str(cur_started).replace("Z", "+00:00"))
                    if st_dt.tzinfo is None:
                        st_dt = st_dt.replace(tzinfo=timezone.utc)
                    if now_dt - st_dt < timedelta(minutes=max(1, ttl_minutes)):
                        active_run = cur_run
                except ValueError:
                    pass
            if not active_run:
                live_sess = self.active_session_for_role(tenant_id, "onboarder", now=now_dt)
                if live_sess is not None and str(live_sess.get("run_id") or "") != run_id:
                    active_run = str(live_sess.get("run_id") or "")

            if active_run:
                base["refresh_queued"] = True
                base["refresh_status"] = "queued"
                self.wikis[tenant_id] = base
                return {"claimed": False, "queued": True, "active_run_id": active_run}

            now_iso = now_dt.isoformat()
            base["refresh_run_id"] = run_id
            base["refresh_started_at"] = now_iso
            base["refresh_status"] = "running"
            base["refresh_queued"] = False
            base["last_error"] = None
            if not base.get("last_updated"):
                base["last_updated"] = now_iso
            self.wikis[tenant_id] = base
            return {"claimed": True, "queued": False, "active_run_id": run_id}

    def complete_wiki_refresh_run(
        self,
        tenant_id: str,
        repo_name: str,
        run_id: str,
        *,
        halted_error: Optional[str] = None,
    ) -> dict[str, Any]:
        with self._claim_lock:
            existing = self.wikis.get(tenant_id)
            if not existing or not run_id:
                return {"finalized": False, "drain_queued": False}
            base = normalize_wiki_row(existing, tenant_id=tenant_id, repo_name=repo_name)
            cur_run = str(base.get("refresh_run_id") or "")
            was_queued = bool(base.get("refresh_queued", False))
            if cur_run != run_id:
                live_sess = self.active_session_for_role(tenant_id, "onboarder")
                other_live = live_sess is not None and str(live_sess.get("run_id") or "") != run_id
                if cur_run or not was_queued or other_live:
                    return {"finalized": False, "drain_queued": False}
            base["refresh_run_id"] = None
            base["refresh_started_at"] = None
            base["refresh_queued"] = False
            if halted_error:
                base["merges_since_refresh"] = 0
                base["refresh_status"] = "queued" if was_queued else "failed"
                base["last_error"] = str(halted_error)[:500]
            else:
                base["refresh_status"] = "queued" if was_queued else "idle"
                base["last_error"] = None
            self.wikis[tenant_id] = base
            return {"finalized": True, "drain_queued": was_queued}

    def add_token(self, tenant_id: str, token_hash: str, owner: str, label: str) -> None:
        self.tokens[token_hash] = {"tenant_id": tenant_id, "owner": owner, "label": label, "revoked": False}

    def revoke_tokens(self, tenant_id: str) -> int:
        n = 0
        for row in self.tokens.values():
            if row["tenant_id"] == tenant_id and not row.get("revoked"):
                row["revoked"] = True
                n += 1
        return n

    def metric_keys(self, tenant_id: str) -> set[str]:
        active_rows = {
            k
            for (tid, k), r in self.metric_def_rows.items()
            if tid == tenant_id and r.get("status", "active") == "active"
        }
        return active_rows | self.metric_defs.get(tenant_id, set())

    def observed_metric_keys(self, tenant_id: str) -> set[str]:
        return {
            r["key"]
            for r in self.observations
            if r.get("tenant_id") == tenant_id and r.get("pr_number") is None and r.get("pr") is None
        }

    def update_tenant_status(self, tenant_id: str, status: str) -> None:
        if tenant_id in self.tenants:
            t = self.tenants[tenant_id]
            self.tenants[tenant_id] = Tenant(t.id, t.repo, status, t.arbitration_mode, t.manifest, t.repository_id)

    def update_tenant_manifest(self, tenant_id: str, manifest: dict[str, Any], sha: str) -> None:
        if tenant_id in self.tenants:
            t = self.tenants[tenant_id]
            changed = t.manifest != manifest or canonical_sha(t.manifest or {}) != sha
            self.tenants[tenant_id] = Tenant(t.id, t.repo, t.status, t.arbitration_mode, manifest, t.repository_id)
            if changed:
                self.tenants_manifest_history.append(
                    {"tenant_id": tenant_id, "manifest": dict(manifest), "manifest_sha": sha, "changed_at": _now()}
                )

    def set_tenant_guardrails(
        self,
        tenant_id: str,
        *,
        spot_max_instance_minutes: int = 60,
        max_pr_wall_clock_minutes: Optional[int] = None,
        max_pr_cost_usd: Optional[float] = None,
        max_review_rounds: Optional[int] = None,
        max_repeated_finding: Optional[int] = None,
    ) -> dict[str, Any]:
        self.spot_max_minutes[tenant_id] = int(spot_max_instance_minutes)
        tenant = self.tenant_by_id(tenant_id)
        manifest = dict(tenant.manifest if tenant else {})
        loop = dict(manifest.get("loop") or {})
        if max_pr_wall_clock_minutes is not None:
            loop["max_pr_wall_clock_minutes"] = int(max_pr_wall_clock_minutes)
        if max_pr_cost_usd is not None:
            loop["max_pr_cost_usd"] = float(max_pr_cost_usd)
        if max_review_rounds is not None:
            loop["max_review_rounds"] = int(max_review_rounds)
        if max_repeated_finding is not None:
            loop["max_repeated_finding"] = int(max_repeated_finding)
        loop.setdefault("agent_runtime", "nanobot")
        manifest["loop"] = loop
        sha = canonical_sha(manifest)
        self.update_tenant_manifest(tenant_id, manifest, sha)
        return {"manifest": manifest, "manifest_sha": sha, "spot_max_instance_minutes": int(spot_max_instance_minutes)}

    def get_tenant_goal(self, tenant_id: str) -> Optional[dict[str, Any]]:
        row = self.tenant_goals.get(tenant_id)
        return copy.deepcopy(row) if row else None

    def upsert_tenant_goal(self, tenant_id: str, row: dict[str, Any]) -> dict[str, Any]:
        now_iso = _now()
        existing = self.tenant_goals.get(tenant_id) or {}
        merged = copy.deepcopy(
            {
                "tenant_id": tenant_id,
                "goal_sha": "",
                "status": "open",
                "stop_reason": None,
                "spent_usd": 0.0,
                "prs_merged": 0,
                "merges_without_progress": 0,
                "started_at": now_iso,
                "last_progress_at": now_iso,
                **existing,
                **row,
                "updated_at": now_iso,
            }
        )
        self.tenant_goals[tenant_id] = merged
        return copy.deepcopy(merged)

    def update_tenant_goal(self, tenant_id: str, patch: dict[str, Any]) -> Optional[dict[str, Any]]:
        existing = self.tenant_goals.get(tenant_id)
        if existing is None:
            return None
        merged = copy.deepcopy({**existing, **patch, "updated_at": _now()})
        self.tenant_goals[tenant_id] = merged
        return copy.deepcopy(merged)

    def set_tenant_goal(self, tenant_id: str, goal: Optional[dict[str, Any]]) -> dict[str, Any]:
        tenant = self.tenant_by_id(tenant_id)
        manifest = dict(tenant.manifest if tenant else {})
        goal_row: Optional[dict[str, Any]] = None
        if goal is None:
            manifest.pop("goal", None)
            sha = canonical_sha(manifest)
            self.update_tenant_manifest(tenant_id, manifest, sha)
            self.tenant_goals.pop(tenant_id, None)
        else:
            parsed_goal = TenantGoal.model_validate(goal)
            goal_dict = parsed_goal.model_dump(mode="json")
            metrics_list = _validate_and_merge_goal_manifest(self, tenant, manifest, goal_dict)
            if not manifest.get("metrics") and metrics_list:
                manifest["metrics"] = metrics_list
            manifest["goal"] = goal_dict
            sha = canonical_sha(manifest)
            self.update_tenant_manifest(tenant_id, manifest, sha)
            goal_sha = parsed_goal.sha256()
            existing = self.get_tenant_goal(tenant_id)
            now_iso = _now()
            if existing is None or existing.get("goal_sha") != goal_sha:
                goal_row = self.upsert_tenant_goal(
                    tenant_id,
                    {
                        "goal_sha": goal_sha,
                        "status": "open",
                        "stop_reason": None,
                        "spent_usd": 0.0,
                        "prs_merged": 0,
                        "merges_without_progress": 0,
                        "planning_run_id": None,
                        "planning_started_at": None,
                        "started_at": now_iso,
                        "last_progress_at": now_iso,
                    },
                )
            else:
                goal_row = existing
        return {"manifest": manifest, "manifest_sha": sha, "goal_state": goal_row}

    def record_goal_spend(self, tenant_id: str, delta_usd: float) -> None:
        if delta_usd <= 0.0:
            return
        with self._claim_lock:
            row = self.tenant_goals.get(tenant_id)
            if row and row.get("status") == "open":
                row["spent_usd"] = round(float(row.get("spent_usd") or 0.0) + float(delta_usd), 4)
                row["updated_at"] = _now()

    def record_goal_merge(self, tenant_id: str, progress: bool) -> None:
        with self._claim_lock:
            row = self.tenant_goals.get(tenant_id)
            if row and row.get("status") == "open":
                row["prs_merged"] = int(row.get("prs_merged") or 0) + 1
                if progress:
                    row["merges_without_progress"] = 0
                    row["last_progress_at"] = _now()
                else:
                    row["merges_without_progress"] = int(row.get("merges_without_progress") or 0) + 1
                row["updated_at"] = _now()

    def claim_goal_planning(
        self, tenant_id: str, run_id: str, now: Optional[datetime] = None, ttl_minutes: int = 15
    ) -> bool:
        if not tenant_id or not run_id:
            return False
        now_dt = now or datetime.now(timezone.utc)
        with self._claim_lock:
            row = self.tenant_goals.get(tenant_id)
            if not row or row.get("status") != "open":
                return False
            cur_run = row.get("planning_run_id")
            cur_started = row.get("planning_started_at")
            if cur_run and cur_started:
                try:
                    st_dt = datetime.fromisoformat(str(cur_started).replace("Z", "+00:00"))
                    if st_dt.tzinfo is None:
                        st_dt = st_dt.replace(tzinfo=timezone.utc)
                    if now_dt - st_dt < timedelta(minutes=ttl_minutes):
                        return False
                except ValueError:
                    pass
            now_iso = now_dt.isoformat()
            row["planning_run_id"] = run_id
            row["planning_started_at"] = now_iso
            row["updated_at"] = now_iso
            return True

    def clear_goal_planning(self, tenant_id: str, run_id: Optional[str] = None) -> None:
        with self._claim_lock:
            row = self.tenant_goals.get(tenant_id)
            if not row:
                return
            if run_id is None or row.get("planning_run_id") == run_id:
                row["planning_run_id"] = None
                row["planning_started_at"] = None
                row["updated_at"] = _now()

    def get_spot_max_minutes(self, tenant_id: str) -> int:
        if tenant_id in self.spot_max_minutes:
            return self.spot_max_minutes[tenant_id]
        t = self.tenants.get(tenant_id)
        if t and isinstance(t.manifest, dict) and t.manifest.get("spot_max_instance_minutes"):
            return int(t.manifest["spot_max_instance_minutes"])
        return 60

    def upsert_metric_definition(self, tenant_id: str, row: dict[str, Any]) -> dict[str, Any]:
        key = str(row["key"])
        existing = self.metric_def_rows.get((tenant_id, key))
        if existing:
            self.metric_definitions_history.append(
                {"tenant_id": tenant_id, "key": key, "operation": "UPDATE", "snapshot": copy.deepcopy(existing)}
            )
        merged = copy.deepcopy({**(existing or {}), **row, "tenant_id": tenant_id, "key": key, "updated_at": _now()})
        self.metric_def_rows[(tenant_id, key)] = merged
        if merged.get("status", "active") == "active":
            self.metric_defs.setdefault(tenant_id, set()).add(key)
        return copy.deepcopy(merged)

    def get_metric_definition(self, tenant_id: str, key: str) -> Optional[dict[str, Any]]:
        r = self.metric_def_rows.get((tenant_id, key))
        return copy.deepcopy(r) if r else None

    def approve_metric_atomic(
        self,
        tenant_id: str,
        key: str,
        digest: str,
        actor: str,
        manifest: dict[str, Any],
        manifest_sha_val: str,
    ) -> None:
        row = self.metric_def_rows.get((tenant_id, key))
        if not row:
            raise LookupError("metric_not_found")
        if row.get("status") not in ("compiled", "active") or str(row.get("digest") or "") != str(digest):
            raise ValueError("digest_mismatch")
        compiled = row.get("compiled") or {}
        self.upsert_metric_definition(
            tenant_id,
            {
                **row,
                **compiled,
                "status": "active",
                "approved_by": actor,
                "approved_at": _now(),
            },
        )
        self.update_tenant_manifest(tenant_id, manifest, manifest_sha_val)

    def retire_metric(self, tenant_id: str, key: str) -> None:
        tenant = self.tenant_by_id(tenant_id)
        if tenant and isinstance(tenant.manifest, dict) and isinstance(tenant.manifest.get("goal"), dict):
            goal_cfg = tenant.manifest["goal"]
            goal_keys = set(goal_cfg.get("targets") or []) | set(goal_cfg.get("guardrails") or [])
            if key in goal_keys:
                raise ValueError(f"Cannot retire metric '{key}' while referenced by active goal")
        row = self.metric_def_rows.get((tenant_id, key))
        if row:
            self.upsert_metric_definition(tenant_id, {**row, "status": "retired"})
        if tenant_id in self.metric_defs:
            self.metric_defs[tenant_id].discard(key)

    def rebuild_tenant_manifest(self, tenant_id: str) -> tuple[dict[str, Any], str]:
        tenant = self.tenant_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"Tenant {tenant_id} not found")
        defs = [d for d in self.metric_definitions(tenant_id) if d.get("status", "active") == "active"]
        manifest = dict(tenant.manifest or {})
        manifest.pop("created_by", None)
        manifest.pop("spot_max_instance_minutes", None)
        manifest.setdefault("repo", tenant.repo)
        manifest.setdefault("owner_github_login", tenant.repo.split("/", 1)[0])
        manifest.setdefault("arbitration", {"mode": tenant.arbitration_mode})
        metrics_list = []
        for d in defs:
            src = d.get("source") or {}
            run_cmd = d.get("run") or (src.get("run") if isinstance(src, dict) else None)
            comparator = d.get("comparator")
            raw_thr = d.get("threshold")
            if not run_cmd or not comparator or raw_thr is None:
                raise ValueError(
                    f"Active metric '{d.get('key')}' is incomplete: run, comparator, and threshold are required"
                )
            raw_compiled = d.get("compiled")
            tol = d.get("tolerance_pct")
            if tol is None and isinstance(raw_compiled, dict):
                tol = raw_compiled.get("tolerance_pct")
            m_entry: dict[str, Any] = {
                "key": d["key"],
                "run": run_cmd,
                "comparator": comparator,
                "threshold": float(raw_thr),
                "baseline_mode": d.get("baseline_mode") or "absolute",
                "min_samples": int(d.get("min_samples") or 1),
            }
            if tol is not None:
                m_entry["tolerance_pct"] = float(tol)
            metrics_list.append(m_entry)
        manifest["metrics"] = metrics_list
        if metrics_list:
            TenantManifest.model_validate(manifest)
        sha = canonical_sha(manifest)
        self.update_tenant_manifest(tenant_id, manifest, sha)
        return manifest, sha

    def ready_to_activate(self, tenant_id: str) -> bool:
        defs = [d for d in self.metric_definitions(tenant_id) if d.get("status", "active") == "active"]
        if not defs:
            return False
        default_obs = self.default_branch_observations(tenant_id, limit=200)
        distinct_samples: dict[str, set[tuple[Any, ...]]] = {}
        for idx, o in enumerate(default_obs):
            k = str(o.get("key", ""))
            run_ident = o.get("run_id") if o.get("run_id") is not None else o.get("check_run_id")
            commit_sha = str(o.get("commit_sha") or "")
            attempt = int(o.get("run_attempt") or 1)
            sample_sig: tuple[Any, ...] = (commit_sha, run_ident, attempt) if run_ident is not None else ("idx", idx)
            distinct_samples.setdefault(k, set()).add(sample_sig)
        return all(len(distinct_samples.get(d["key"], set())) >= int(d.get("min_samples") or 1) for d in defs)

    def record_observations(self, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            if not r.get("tenant_id"):
                raise ValueError("Observation row must specify tenant_id")
            self.observations.append(r)

    def list_tenants(self) -> list[Tenant]:
        return copy.deepcopy(list(self.tenants.values()))

    def metric_definitions(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = [copy.deepcopy(r) for (tid, _), r in self.metric_def_rows.items() if tid == tenant_id]
        known = {r["key"] for r in rows}
        for k in self.metric_defs.get(tenant_id, set()):
            if k not in known:
                rows.append({"key": k, "status": "active", "comparator": ">=", "threshold": 1.0, "min_samples": 1})
        return sorted(rows, key=lambda r: r["key"])

    def recent_observations(self, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        obs = [r for r in self.observations if r.get("tenant_id") == tenant_id]
        return copy.deepcopy(obs[-limit:])

    def observations_for_pr_or_sha(
        self,
        tenant_id: str,
        pr_number: Optional[int] = None,
        commit_sha: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not pr_number and not commit_sha:
            return []
        obs = []
        for r in self.observations:
            if r.get("tenant_id") != tenant_id:
                continue
            r_pr = r.get("pr_number") if r.get("pr_number") is not None else r.get("pr")
            if commit_sha and str(r.get("commit_sha") or "") == commit_sha:
                obs.append(r)
            elif pr_number is not None and r_pr is not None and int(r_pr) == int(pr_number):
                obs.append(r)
        return copy.deepcopy(obs[-limit:])

    def default_branch_observations(self, tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
        obs = [
            r
            for r in self.observations
            if r.get("tenant_id") == tenant_id
            and r.get("pr_number") is None
            and r.get("pr") is None
        ]
        return copy.deepcopy(obs[-limit:])

    def create_tenant(self, repo: str, created_by: str) -> Tenant:
        t = Tenant(f"t-{len(self.tenants)}", repo, "onboarding", "shadow", default_manifest(repo, created_by))
        self.tenants[t.id] = t
        return copy.deepcopy(t)

    def active_tokens(self, tenant_id: str) -> list[dict[str, Any]]:
        return [
            {"owner": r.get("owner", ""), "label": r.get("label", "ci")}
            for r in self.tokens.values()
            if r.get("tenant_id") == tenant_id and not r.get("revoked")
        ]

    def llm_keys(self, tenant_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(v) for k, v in self.llm_key_data.items() if k[0] == tenant_id]

    def role_llm_prefs(self, tenant_id: str) -> dict[str, dict[str, Any]]:
        return {role: copy.deepcopy(v) for (t, role), v in self.role_llm.items() if t == tenant_id}

    def set_role_llm_pref(
        self, tenant_id: str, role: str, provider: str, model: str, reasoning_effort: str
    ) -> None:
        self.role_llm[(tenant_id, role)] = {
            "role": role,
            "provider": provider,
            "model": model,
            "reasoning_effort": reasoning_effort,
        }

    def put_llm_key(self, tenant_id: str, provider: str, row: dict[str, Any]) -> None:
        self.llm_key_data[(tenant_id, provider)] = {**row, "tenant_id": tenant_id, "provider": provider}

    def delete_llm_key(self, tenant_id: str, provider: str) -> None:
        self.llm_key_data.pop((tenant_id, provider), None)

    def record_loop_round(self, row: dict[str, Any]) -> None:
        self.loop_rounds.append(row)

    def loop_rounds_for_pr(self, tenant_id: str, pr_number: int) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(r)
            for r in self.loop_rounds
            if r.get("tenant_id") == tenant_id and r.get("pr_number") == pr_number
        ]

    def loop_rounds_for_tenant(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rounds = [r for r in self.loop_rounds if r.get("tenant_id") == tenant_id]
        return copy.deepcopy(rounds[-limit:])

    def has_processed_event(self, tenant_id: str, event_id: str) -> bool:
        return (tenant_id, event_id) in self.events

    def record_processed_event(
        self,
        tenant_id: str,
        event_id: str,
        payload: Optional[dict[str, Any]] = None,
        kind: Optional[str] = None,
        sender: Optional[str] = None,
    ) -> None:
        self.events.add((tenant_id, event_id))
        if not hasattr(self, "conductor_events"):
            self.conductor_events: list[dict[str, Any]] = []
        self.conductor_events.append(
            {
                "tenant_id": tenant_id,
                "event_id": event_id,
                "payload": payload,
                "kind": kind,
                "sender": sender,
            }
        )

    def get_tenant_llm_ciphertext(self, tenant_id: str, provider: str) -> Optional[str]:
        row = self.llm_key_data.get((tenant_id, provider))
        return row.get("ciphertext") if row else None

    def get_tenant_llm_key_record(self, tenant_id: str, provider: str) -> Optional[dict[str, Any]]:
        row = self.llm_key_data.get((tenant_id, provider))
        return copy.deepcopy(row) if row else None

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
        row: dict[str, Any] = {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "role": role,
            "cred_hash": cred_hash,
            "expires_at": expires_at,
            "goal_mode": bool(goal_mode),
            "created_at": _now(),
        }
        if plan_count is not None:
            row["plan_count"] = int(plan_count)
        if metric_key is not None:
            row["metric_key"] = str(metric_key)
        if head_sha is not None:
            row["head_sha"] = str(head_sha)
        self.run_creds[run_id] = row

    def get_run_credential(self, run_id: str) -> Optional[dict[str, Any]]:
        row = self.run_creds.get(run_id)
        return copy.deepcopy(row) if row is not None else None

    def mark_run_credential_used(self, run_id: str) -> None:
        if run_id in self.run_creds:
            self.run_creds[run_id]["used_at"] = _now()

    def claim_run_credential_instance(self, run_id: str, instance_id: str) -> bool:
        if not run_id or not instance_id:
            return False
        with self._claim_lock:
            rec = self.run_creds.get(run_id)
            if not rec:
                return False
            claimed = rec.get("claimed_instance_id")
            if claimed and str(claimed) != instance_id:
                return False
            rec["claimed_instance_id"] = instance_id
            return True

    def claim_run_credential_envelope(self, run_id: str, comment_id: int) -> bool:
        if not run_id or comment_id is None or int(comment_id) <= 0:
            return False
        cid = int(comment_id)
        with self._claim_lock:
            rec = self.run_creds.get(run_id)
            if not rec:
                return False
            claimed = rec.get("envelope_comment_id")
            if claimed is not None and int(claimed) != cid:
                return False
            rec["envelope_comment_id"] = cid
            return True

    def record_security_audit(
        self,
        event_type: str,
        *,
        tenant_id: Optional[str] = None,
        actor: str = "system",
        run_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self.security_audit_log.append(
            {
                "tenant_id": tenant_id,
                "event_type": event_type,
                "actor": actor,
                "run_id": run_id,
                "metadata": metadata or {},
                "created_at": _now(),
            }
        )

    def active_sessions(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        current_time = now or datetime.now(timezone.utc)
        cutoff = current_time - timedelta(minutes=15)
        res: list[dict[str, Any]] = []
        session_list = self.sessions.values() if isinstance(self.sessions, dict) else self.sessions
        for s in session_list:
            if s.get("status") not in ("Running", "running", "active", "pending"):
                continue
            hb_str = s.get("heartbeat_at") or s.get("started_at")
            if not hb_str:
                continue
            try:
                hb_dt = datetime.fromisoformat(hb_str)
                if hb_dt.tzinfo is None:
                    hb_dt = hb_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if hb_dt < cutoff:
                res.append(copy.deepcopy(s))
        return res

    def list_open_sessions(self) -> list[dict[str, Any]]:
        session_list = self.sessions.values() if isinstance(self.sessions, dict) else self.sessions
        return [
            copy.deepcopy(s)
            for s in session_list
            if s.get("status") in ("Running", "running", "active", "pending")
        ]

    def get_session_by_instance(self, instance_id: str) -> Optional[dict[str, Any]]:
        if not instance_id:
            return None
        if isinstance(self.sessions, dict):
            s = self.sessions.get(instance_id)
            return copy.deepcopy(s) if s is not None else None
        for s in self.sessions:
            if s.get("instance_id") == instance_id:
                return copy.deepcopy(s)
        return None

    def update_session(self, instance_id: str, patch: dict[str, Any]) -> None:
        if isinstance(self.sessions, dict):
            if instance_id in self.sessions:
                self.sessions[instance_id].update(patch)
        elif isinstance(self.sessions, list):
            for s in self.sessions:
                if s.get("instance_id") == instance_id:
                    s.update(patch)
                    break

    def insert_arbitration_decision(self, row: dict[str, Any]) -> None:
        self.arbitration_decisions.append(row)

    def upsert_session(self, row: dict[str, Any]) -> None:
        inst_id = row.get("instance_id", "")
        if inst_id:
            if isinstance(self.sessions, dict):
                if inst_id in self.sessions:
                    self.sessions[inst_id].update(row)
                else:
                    self.sessions[inst_id] = dict(row)
            elif isinstance(self.sessions, list):
                for s in self.sessions:
                    if s.get("instance_id") == inst_id:
                        s.update(row)
                        return
                self.sessions.append(dict(row))

    def record_run_events(self, run_id: Optional[str], rows: list[dict[str, Any]]) -> None:
        existing_keys = {(r.get("run_id"), r.get("seq")) for r in self.run_events if r.get("run_id") is not None}
        for r in rows:
            row_dict = dict(r)
            if run_id is not None:
                row_dict["run_id"] = run_id
            key = (row_dict.get("run_id"), row_dict.get("seq"))
            if key[0] is not None and key in existing_keys:
                continue
            existing_keys.add(key)
            self.run_events.append(row_dict)

    def record_run_event(self, run_id: Optional[str], kind: str, payload: dict[str, Any]) -> None:
        row = {
            "run_id": run_id,
            "seq": time.time_ns(),
            "kind": kind,
            "payload": payload,
        }
        self.record_run_events(run_id, [row])

    def run_events_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(r) for r in self.run_events if r.get("run_id") == run_id]

    def get_session_by_run(self, run_id: str) -> Optional[dict[str, Any]]:
        session_list = self.sessions.values() if isinstance(self.sessions, dict) else self.sessions
        for s in session_list:
            if s.get("run_id") == run_id:
                return copy.deepcopy(s)
        return None

    def latest_metric_compiler_session(self, tenant_id: str, key: str) -> Optional[dict[str, Any]]:
        rows = self.sessions.values() if isinstance(self.sessions, dict) else self.sessions
        picked = pick_metric_compiler_session(list(rows), tenant_id, key)
        return copy.deepcopy(picked) if picked is not None else None

    def issue_session_active(self, tenant_id: str, issue_id: int, now: Optional[datetime] = None) -> bool:
        current = now or datetime.now(timezone.utc)
        rows = self.sessions.values() if isinstance(self.sessions, dict) else self.sessions
        return any(
            r.get("tenant_id") == tenant_id and r.get("issue_id") == issue_id and session_is_live(r, current)
            for r in rows
        )

    def active_session_for_role(
        self,
        tenant_id: str,
        role: str,
        *,
        issue_id: Optional[int] = None,
        metric_key: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[dict[str, Any]]:
        current = now or datetime.now(timezone.utc)
        rows = list(self.sessions.values() if isinstance(self.sessions, dict) else self.sessions)
        for r in rows:
            if r.get("tenant_id") != tenant_id or r.get("role") != role:
                continue
            if issue_id is not None and int(r.get("issue_id") or 0) != int(issue_id):
                continue
            if not session_is_live(r, current):
                continue
            if metric_key is not None:
                rid = str(r.get("run_id") or "")
                todo = str(r.get("current_todo") or "")
                cred_key = str((self.run_creds.get(rid) or {}).get("metric_key") or "")
                bound = str(
                    r.get("metric_key")
                    or cred_key
                    or (rid.split("__", 1)[1] if "__" in rid else "")
                    or (todo.partition(":")[2].strip() if todo.startswith("compile:") else "")
                ).strip()
                if bound != metric_key:
                    continue
            return copy.deepcopy(r)
        return None

    def set_arbitration_mode(self, tenant_id: str, mode: str) -> None:
        self.tenants[tenant_id] = dataclasses.replace(self.tenants[tenant_id], arbitration_mode=mode)


@lru_cache(maxsize=1)
def get_store() -> Store:
    return SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])  # type: ignore[return-value]
