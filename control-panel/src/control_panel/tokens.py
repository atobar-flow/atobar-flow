"""Tenant token minting and hashing. Only sha256 digests are ever persisted."""

import hashlib
import hmac
import os
import secrets
from typing import Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TOKEN_PREFIX = "atb_"
_SCREENSHOT_NONCE_BYTES = 12


def mint_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def digest_matches(token: str, expected_sha256: str) -> bool:
    return bool(expected_sha256) and hmac.compare_digest(hash_token(token), expected_sha256)


def _artifact_secret(secret: Optional[str] = None) -> str:
    if secret:
        return secret
    configured = (
        os.environ.get("CP_ARTIFACT_SIGNING_KEY")
        or os.environ.get("CP_SESSION_SECRET")
        or ""
    ).strip()
    if not configured:
        raise RuntimeError("screenshot signing not configured")
    return configured


def sign_screenshot_url(
    tenant_id: str,
    run_id: str,
    filename: str,
    exp: int,
    secret: Optional[str] = None,
) -> str:
    key = _artifact_secret(secret).encode("utf-8")
    msg = f"{tenant_id}:{run_id}:{filename}:{exp}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_screenshot_signature(
    tenant_id: str,
    run_id: str,
    filename: str,
    exp: int,
    sig: str,
    secret: Optional[str] = None,
) -> bool:
    if not sig:
        return False
    expected = sign_screenshot_url(tenant_id, run_id, filename, exp, secret=secret)
    return hmac.compare_digest(expected, sig)


def _derive_artifact_key(secret: Optional[str] = None) -> bytes:
    raw = _artifact_secret(secret).encode("utf-8")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"atobar-flow-screenshot-aes256gcm",
    )
    return hkdf.derive(raw)


def encrypt_screenshot_bytes(
    tenant_id: str,
    run_id: str,
    filename: str,
    raw_bytes: bytes,
    secret: Optional[str] = None,
) -> bytes:
    key = _derive_artifact_key(secret)
    nonce = secrets.token_bytes(_SCREENSHOT_NONCE_BYTES)
    aad = f"{tenant_id}:{run_id}:{filename}".encode("utf-8")
    ct = AESGCM(key).encrypt(nonce, raw_bytes, aad)
    return nonce + ct


def decrypt_screenshot_bytes(
    tenant_id: str,
    run_id: str,
    filename: str,
    encrypted_blob: bytes,
    secret: Optional[str] = None,
) -> bytes:
    if len(encrypted_blob) <= _SCREENSHOT_NONCE_BYTES:
        raise ValueError("Encrypted screenshot blob too short")
    key = _derive_artifact_key(secret)
    nonce = encrypted_blob[:_SCREENSHOT_NONCE_BYTES]
    ct = encrypted_blob[_SCREENSHOT_NONCE_BYTES:]
    aad = f"{tenant_id}:{run_id}:{filename}".encode("utf-8")
    return AESGCM(key).decrypt(nonce, ct, aad)
