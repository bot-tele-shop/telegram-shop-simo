from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from digital_shelf.admin import AdminPrincipal
from digital_shelf.api import create_app
from digital_shelf.auth import AuthenticatedIdentity
from digital_shelf.config import Settings
from digital_shelf.store_settings import (
    InvalidSettingValueError,
    SettingKey,
    SettingsPatchCommand,
    SettingUpdate,
    SettingView,
    validate_setting,
)


class FakeVerifier:
    async def verify(self, token: str) -> AuthenticatedIdentity:
        assert token == "valid-token"
        return AuthenticatedIdentity(subject=uuid4(), email="owner@example.com")


class FakeAuthorizer:
    async def authorize(self, identity: AuthenticatedIdentity) -> AdminPrincipal:
        return AdminPrincipal(
            admin_id=uuid4(),
            subject=identity.subject,
            email=identity.email or "owner@example.com",
            roles=("owner",),
            permissions=frozenset({"*"}),
        )


class MemorySettingStore:
    def __init__(self) -> None:
        self.values = {
            SettingKey.CHECKOUT_PAUSED: SettingView(
                key=SettingKey.CHECKOUT_PAUSED,
                value=True,
                revision=1,
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        }

    async def list_settings(self) -> Sequence[SettingView]:
        return list(self.values.values())

    async def update_settings(
        self,
        *,
        command: SettingsPatchCommand,
        actor_admin_id: UUID,
        correlation_id: UUID,
    ) -> Sequence[SettingView]:
        del actor_admin_id, correlation_id
        updated = []
        for item in command.updates:
            key = SettingKey(item.key)
            value = validate_setting(key, item.value)
            view = SettingView(
                key=key,
                value=value,
                revision=item.expected_revision + 1,
                updated_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
            self.values[key] = view
            updated.append(view)
        return updated


def build_client() -> TestClient:
    app = create_app(
        Settings(environment="test"),
        jwt_verifier=FakeVerifier(),
        admin_authorizer=FakeAuthorizer(),
        setting_store=MemorySettingStore(),
    )
    return TestClient(app)


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        (SettingKey.SHOP_NAME, "  Digital Shelf  ", "Digital Shelf"),
        (SettingKey.SUPPORT_CONTACT, " @StoreSupport ", "@StoreSupport"),
        (SettingKey.TERMS_TEXT, "Terms\n", "Terms"),
        (SettingKey.PRIVACY_TEXT, "Privacy\n", "Privacy"),
        (SettingKey.CHECKOUT_PAUSED, True, True),
    ],
)
def test_setting_values_are_normalized_and_typed(
    key: SettingKey, value: object, expected: object
) -> None:
    assert validate_setting(key, value) == expected


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (SettingKey.SHOP_NAME, ""),
        (SettingKey.SHOP_NAME, "x" * 65),
        (SettingKey.SUPPORT_CONTACT, "line\nbreak"),
        (SettingKey.TERMS_TEXT, ""),
        (SettingKey.TERMS_TEXT, "Terms\u0000with null"),
        (SettingKey.PRIVACY_TEXT, "x" * 10_001),
        (SettingKey.CHECKOUT_PAUSED, "true"),
    ],
)
def test_invalid_setting_values_are_rejected(key: SettingKey, value: object) -> None:
    with pytest.raises(InvalidSettingValueError):
        validate_setting(key, value)


def test_owner_can_read_settings() -> None:
    with build_client() as client:
        response = client.get(
            "/admin/v1/settings", headers={"Authorization": "Bearer valid-token"}
        )

    assert response.status_code == 200
    assert response.json()[0]["key"] == "checkout_paused"
    assert response.json()[0]["value"] is True


def test_owner_can_update_settings_atomically() -> None:
    with build_client() as client:
        response = client.patch(
            "/admin/v1/settings",
            headers={"Authorization": "Bearer valid-token"},
            json={
                "updates": [
                    {"key": "checkout_paused", "value": False, "expected_revision": 1},
                    {"key": "shop_name", "value": " My Store ", "expected_revision": 0},
                ]
            },
        )

    assert response.status_code == 200
    assert [(item["key"], item["value"], item["revision"]) for item in response.json()] == [
        ("checkout_paused", False, 2),
        ("shop_name", "My Store", 1),
    ]


def test_unknown_setting_key_returns_400() -> None:
    with build_client() as client:
        response = client.patch(
            "/admin/v1/settings",
            headers={"Authorization": "Bearer valid-token"},
            json={"updates": [{"key": "bot_token", "value": "secret", "expected_revision": 0}]},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "unknown setting key"}


def test_invalid_setting_value_returns_400_without_echoing_value() -> None:
    invalid_value = "not-a-boolean"
    with build_client() as client:
        response = client.patch(
            "/admin/v1/settings",
            headers={"Authorization": "Bearer valid-token"},
            json={
                "updates": [
                    {
                        "key": "checkout_paused",
                        "value": invalid_value,
                        "expected_revision": 1,
                    }
                ]
            },
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid setting value"}
    assert invalid_value not in response.text


def test_duplicate_setting_key_is_rejected_before_write() -> None:
    with pytest.raises(ValueError, match="duplicate setting key"):
        SettingsPatchCommand(
            updates=[
                SettingUpdate(key="shop_name", value="One", expected_revision=0),
                SettingUpdate(key="shop_name", value="Two", expected_revision=0),
            ]
        )
