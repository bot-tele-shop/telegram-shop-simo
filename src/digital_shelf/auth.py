"""Fail-closed Supabase JWT verification for canonical admin routes."""

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import jwt
from jwt import PyJWK, PyJWKClient
from jwt.exceptions import InvalidTokenError, PyJWKClientError

_ALLOWED_ALGORITHMS = ("ES256", "RS256")
_REQUIRED_CLAIMS = ("exp", "iss", "sub", "aud")


class AuthenticationError(Exception):
    """A deliberately detail-free authentication failure."""


class SigningKeyClient(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> PyJWK: ...


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> "AuthenticatedIdentity": ...


@dataclass(frozen=True)
class AuthenticatedIdentity:
    subject: UUID
    email: str | None


def parse_bearer_token(authorization: str | None) -> str:
    """Extract one bearer credential without accepting ambiguous whitespace."""

    if not authorization:
        raise AuthenticationError
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise AuthenticationError
    return parts[1]


class SupabaseJWTVerifier:
    """Verify asymmetric Supabase access tokens using the project's JWKS."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        key_client: SigningKeyClient | None = None,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._key_client = key_client or PyJWKClient(
            f"{issuer}/.well-known/jwks.json",
            cache_jwk_set=True,
            lifespan=600,
            cache_keys=True,
        )

    def _decode(self, token: str) -> dict[str, Any]:
        signing_key = self._key_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=list(_ALLOWED_ALGORITHMS),
            audience=self._audience,
            issuer=self._issuer,
            options={"require": list(_REQUIRED_CLAIMS)},
        )
        return dict(claims)

    async def verify(self, token: str) -> AuthenticatedIdentity:
        """Validate a token and return only identity fields used for authorization."""

        try:
            claims = await asyncio.to_thread(self._decode, token)
            subject = UUID(str(claims["sub"]))
            email_value = claims.get("email")
            email = email_value.strip().lower() if isinstance(email_value, str) else None
        except (InvalidTokenError, PyJWKClientError, KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError from exc
        return AuthenticatedIdentity(subject=subject, email=email or None)
