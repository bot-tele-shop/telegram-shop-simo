import pytest

from digital_shelf.features import (
    FeatureKey,
    FeatureState,
    evaluate_feature,
)


@pytest.mark.parametrize("feature", list(FeatureKey))
def test_disabled_feature_is_unavailable_without_readiness_checks(feature: FeatureKey) -> None:
    result = evaluate_feature(feature, requested_enabled=False, config={}, facts={})

    assert result.state is FeatureState.DISABLED
    assert result.missing == ()


@pytest.mark.parametrize(
    ("feature", "config", "facts", "missing"),
    [
        (FeatureKey.OFFERS, {}, {}, ("active promotion",)),
        (
            FeatureKey.REFERRALS,
            {},
            {},
            ("reward policy", "positive hold period", "positive per-user limit"),
        ),
        (
            FeatureKey.RESELLER_API,
            {},
            {},
            ("API terms URL", "authorized API client"),
        ),
        (
            FeatureKey.RESTOCK_ANNOUNCEMENTS,
            {},
            {},
            ("destination chat", "verified bot posting permission"),
        ),
        (
            FeatureKey.LOW_STOCK_ALERTS,
            {},
            {},
            ("owner notification destination", "non-negative stock threshold"),
        ),
        (FeatureKey.LOCALIZATION, {}, {}, ("complete additional locale",)),
    ],
)
def test_enabled_request_stays_setup_required_until_configuration_is_complete(
    feature: FeatureKey,
    config: dict[str, object],
    facts: dict[str, object],
    missing: tuple[str, ...],
) -> None:
    result = evaluate_feature(feature, requested_enabled=True, config=config, facts=facts)

    assert result.state is FeatureState.SETUP_REQUIRED
    assert result.missing == missing


@pytest.mark.parametrize(
    ("feature", "config", "facts"),
    [
        (FeatureKey.OFFERS, {}, {"active_promotion_count": 1}),
        (
            FeatureKey.REFERRALS,
            {"reward_policy": "discount", "hold_days": 14, "per_user_limit": 5},
            {},
        ),
        (
            FeatureKey.RESELLER_API,
            {"terms_url": "https://example.test/api-terms"},
            {"authorized_client_count": 1},
        ),
        (
            FeatureKey.RESTOCK_ANNOUNCEMENTS,
            {"destination_chat_id": -100123},
            {"bot_can_post": True},
        ),
        (
            FeatureKey.LOW_STOCK_ALERTS,
            {"owner_destination": "123", "threshold": 0},
            {},
        ),
        (FeatureKey.LOCALIZATION, {}, {"complete_locale_count": 1}),
    ],
)
def test_ready_enabled_feature_becomes_available(
    feature: FeatureKey,
    config: dict[str, object],
    facts: dict[str, object],
) -> None:
    result = evaluate_feature(feature, requested_enabled=True, config=config, facts=facts)

    assert result.state is FeatureState.ENABLED
    assert result.missing == ()


def test_unknown_feature_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown feature"):
        evaluate_feature("wallet", requested_enabled=True, config={}, facts={})  # type: ignore[arg-type]

