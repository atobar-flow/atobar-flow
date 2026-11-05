"""Credential loading and output redaction for atobar-ops.

Resolution order for Supabase credentials:
1. Environment variables (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY` / `SUPABASE_KEY`, `SUPABASE_DB_PASSWORD`)
2. `.env` in current working directory or repo root
3. AWS Secrets Manager (`SUPABASE_SECRET_NAME`, default `flow/supabase/credentials`, region `AWS_REGION` or `us-west-2`)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REGION = "us-west-2"
SUPABASE_SECRET_NAME = "flow/supabase/credentials"
KEK_SECRET_NAME = "flow/control-panel/llm_kek"
DEFAULT_POOLER_HOST = "aws-0-us-west-2.pooler.supabase.com"
DEFAULT_FACTORY_REPO = "atobar-flow/atobar-flow"
DEFAULT_CP_HOSTNAME = "cp.example.invalid"
DEFAULT_CP_URL = f"https://{DEFAULT_CP_HOSTNAME}"

_REDACT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"atb_[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{10,}"),
    re.compile(r"ghp_[A-Za-z0-9]{10,}"),
    re.compile(r"ghs_[A-Za-z0-9]{10,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{10,}"),
    re.compile(r"gho_[A-Za-z0-9]{10,}"),
    re.compile(r"xai-[A-Za-z0-9]{10,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]


class CredsError(RuntimeError):
    """Raised when required credentials cannot be resolved."""


def redact(text: str, extra_secrets: list[str] | None = None) -> str:
    if not text:
        return text
    out = text
    if extra_secrets:
        for secret in extra_secrets:
            if secret and len(secret) >= 6:
                out = out.replace(secret, "[REDACTED]")
    for pat in _REDACT_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def _load_dotenv() -> None:
    cur = Path.cwd().resolve()
    for directory in [cur, *cur.parents]:
        env_path = directory / ".env"
        if env_path.is_file():
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip("'").strip('"')
                if k and k not in os.environ:
                    os.environ[k] = v
            break


def factory_repo() -> str:
    _load_dotenv()
    return os.environ.get("FLOW_FACTORY_REPO", DEFAULT_FACTORY_REPO).strip() or DEFAULT_FACTORY_REPO


def cp_base_url() -> str:
    _load_dotenv()
    for key in ("CP_PUBLIC_URL", "ATOBAR_CP_URL", "CONTROL_PANEL_URL"):
        val = os.environ.get(key, "").strip().rstrip("/")
        if val:
            return val if val.startswith(("http://", "https://")) else f"https://{val}"
    host = os.environ.get("CP_HOSTNAME", "").strip().rstrip("/")
    if host:
        return host if host.startswith(("http://", "https://")) else f"https://{host}"
    return DEFAULT_CP_URL


def cp_ssh_host() -> str:
    _load_dotenv()
    explicit = os.environ.get("CP_SSH_HOST", "").strip()
    if explicit:
        return explicit
    hostname = os.environ.get("CP_HOSTNAME", "").strip()
    if not hostname:
        url = os.environ.get("CP_PUBLIC_URL", "").strip()
        if url:
            hostname = url.split("://", 1)[-1].split("/", 1)[0].strip()
    hostname = hostname or DEFAULT_CP_HOSTNAME
    return f"root@{hostname}"


def _fetch_aws_secret(secret_id: str, region: str | None = None) -> str | None:
    region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION
    try:
        import boto3  # type: ignore[import-untyped]

        client = boto3.client("secretsmanager", region_name=region)
        resp = client.get_secret_value(SecretId=secret_id)
        val = resp.get("SecretString")
        return str(val).strip() if val else None
    except Exception:
        pass
    try:
        res = subprocess.run(
            [
                "aws",
                "secretsmanager",
                "get-secret-value",
                "--secret-id",
                secret_id,
                "--region",
                region,
                "--query",
                "SecretString",
                "--output",
                "text",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return None


@dataclass(frozen=True)
class SupabaseCreds:
    url: str
    service_key: str
    db_password: str | None
    project_ref: str


def supabase_creds() -> SupabaseCreds:
    _load_dotenv()
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip()
    db_password = os.environ.get("SUPABASE_DB_PASSWORD", "").strip() or None

    if not url or not key or not db_password:
        raw = _fetch_aws_secret(os.environ.get("SUPABASE_SECRET_NAME", SUPABASE_SECRET_NAME))
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {}
            url = url or str(data.get("api_url") or data.get("SUPABASE_URL") or "")
            key = key or str(data.get("service_key") or data.get("SUPABASE_SERVICE_KEY") or "")
            db_password = db_password or data.get("db_password")
            if data.get("pghost") and "SUPABASE_PGHOST" not in os.environ:
                os.environ["SUPABASE_PGHOST"] = str(data["pghost"])
            if data.get("pgport") and "SUPABASE_PGPORT" not in os.environ:
                os.environ["SUPABASE_PGPORT"] = str(data["pgport"])
            if data.get("pguser") and "SUPABASE_PGUSER" not in os.environ:
                os.environ["SUPABASE_PGUSER"] = str(data["pguser"])

    if not url or not key:
        raise CredsError(
            "Missing Supabase credentials (checked env, .env, and AWS Secrets Manager "
            f"'{SUPABASE_SECRET_NAME}')"
        )

    m = re.match(r"https://([a-z0-9]+)\.supabase\.co", url)
    project_ref = m.group(1) if m else ""
    return SupabaseCreds(url=url, service_key=key, db_password=db_password, project_ref=project_ref)


def kek_bytes() -> bytes | None:
    if not os.environ.get("CP_LLM_KEK"):
        raw = _fetch_aws_secret(KEK_SECRET_NAME)
        if raw:
            os.environ["CP_LLM_KEK"] = raw
    try:
        import base64

        raw_kek = os.environ.get("CP_LLM_KEK", "")
        if not raw_kek:
            return None
        key = base64.b64decode(raw_kek)
        return key if len(key) == 32 else None
    except Exception:
        return None


def decrypt_local_kek(kek: bytes, tenant_id: str, provider: str, ciphertext: str) -> str:
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    blob = base64.b64decode(ciphertext)
    aad = f"atobar-flow/llm-key/v1/{tenant_id}/{provider}".encode()
    return AESGCM(kek).decrypt(blob[:12], blob[12:], aad).decode()


def pooler_dsn(creds: SupabaseCreds) -> str:
    """Builds a Postgres connection string to the Supabase pooler (session mode, port 5432)."""
    host = (
        os.environ.get("SUPABASE_PGHOST")
        or os.environ.get("SUPABASE_POOLER_HOST")
        or DEFAULT_POOLER_HOST
    )
    port = os.environ.get("SUPABASE_PGPORT", "5432").strip() or "5432"
    user = os.environ.get("SUPABASE_PGUSER", "").strip() or f"postgres.{creds.project_ref}"
    password = creds.db_password or ""
    return f"postgresql://{user}:{password}@{host}:{port}/postgres"
