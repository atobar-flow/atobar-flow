"""GitHub Actions OIDC token verification for client CI authentication (M07feat01)."""

from typing import Any, Optional

import jwt

GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_JWKS_URL = "https://token.actions.githubusercontent.com/.well-known/jwks"
DEFAULT_AUDIENCE = "atobar-flow"


def get_default_jwk_client() -> jwt.PyJWKClient:
    """Returns a PyJWKClient configured with GitHub Actions JWKS endpoint."""
    return jwt.PyJWKClient(GITHUB_JWKS_URL, cache_keys=True)


def verify_github_oidc_token(
    token: str,
    expected_audience: str = DEFAULT_AUDIENCE,
    jwk_client: Optional[Any] = None,
) -> dict[str, Any]:
    """Verifies a GitHub Actions OIDC JWT token against GitHub's JWKS.

    Validates signature, issuer, audience, and expiration.
    Returns the decoded token claims dictionary.
    """
    client = jwk_client or get_default_jwk_client()
    try:
        signing_key = client.get_signing_key_from_jwt(token)
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=expected_audience,
            issuer=GITHUB_OIDC_ISSUER,
        )
        return claims
    except Exception as e:
        raise ValueError(f"Invalid OIDC token: {e}") from e
