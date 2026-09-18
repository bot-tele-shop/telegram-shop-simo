import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt import PyJWK

from digital_shelf.auth import AuthenticationError, SupabaseJWTVerifier, parse_bearer_token


class StaticKeyClient:
    def __init__(self, key: PyJWK) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, token: str) -> PyJWK:
        del token
        return self.key


@pytest.fixture
def signing_material() -> tuple[ec.EllipticCurvePrivateKey, PyJWK]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk.update({"kid": "test-key", "alg": "ES256", "use": "sig"})
    return private_key, PyJWK.from_dict(public_jwk)


def issue_token(
    private_key: ec.EllipticCurvePrivateKey,
    *,
    overrides: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": "https://project.supabase.co/auth/v1",
        "aud": "authenticated",
        "sub": str(uuid4()),
        "email": "owner@example.com",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides or {})
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": "test-key"})


def verifier(public_jwk: PyJWK) -> SupabaseJWTVerifier:
    return SupabaseJWTVerifier(
        issuer="https://project.supabase.co/auth/v1",
        audience="authenticated",
        key_client=StaticKeyClient(public_jwk),
    )


def test_valid_supabase_token_returns_minimized_identity(
    signing_material: tuple[ec.EllipticCurvePrivateKey, PyJWK],
) -> None:
    private_key, public_jwk = signing_material
    subject = uuid4()

    principal = asyncio.run(
        verifier(public_jwk).verify(issue_token(private_key, overrides={"sub": str(subject)}))
    )

    assert principal.subject == subject
    assert principal.email == "owner@example.com"


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://other.supabase.co/auth/v1"},
        {"aud": "other"},
        {"exp": datetime.now(UTC) - timedelta(seconds=1)},
        {"sub": "not-a-uuid"},
        {"sub": None},
    ],
)
def test_invalid_claims_are_rejected(
    signing_material: tuple[ec.EllipticCurvePrivateKey, PyJWK],
    overrides: dict[str, Any],
) -> None:
    private_key, public_jwk = signing_material

    with pytest.raises(AuthenticationError):
        asyncio.run(verifier(public_jwk).verify(issue_token(private_key, overrides=overrides)))


def test_token_signed_by_another_key_is_rejected(
    signing_material: tuple[ec.EllipticCurvePrivateKey, PyJWK],
) -> None:
    _, public_jwk = signing_material
    attacker_key = ec.generate_private_key(ec.SECP256R1())

    with pytest.raises(AuthenticationError):
        asyncio.run(verifier(public_jwk).verify(issue_token(attacker_key)))


def test_symmetric_algorithm_is_never_accepted() -> None:
    token = jwt.encode(
        {
            "iss": "https://project.supabase.co/auth/v1",
            "aud": "authenticated",
            "sub": str(uuid4()),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        "not-a-production-secret-of-32-bytes",
        algorithm="HS256",
    )
    symmetric_jwk = PyJWK.from_dict(
        {
            "kty": "oct",
            "k": "bm90LWEtcHJvZHVjdGlvbi1zZWNyZXQtb2YtMzItYnl0ZXM",
            "alg": "HS256",
        }
    )

    with pytest.raises(AuthenticationError):
        asyncio.run(verifier(symmetric_jwk).verify(token))


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer signed.token.value", "signed.token.value"),
        ("bearer signed.token.value", "signed.token.value"),
    ],
)
def test_bearer_token_is_parsed_strictly(header: str, expected: str) -> None:
    assert parse_bearer_token(header) == expected


@pytest.mark.parametrize(
    "header",
    [None, "", "Basic abc", "Bearer", "Bearer one two", "Bearer   "],
)
def test_missing_or_malformed_bearer_token_is_rejected(header: str | None) -> None:
    with pytest.raises(AuthenticationError):
        parse_bearer_token(header)
