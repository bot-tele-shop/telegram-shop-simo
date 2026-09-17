"""Server-owned feature readiness rules.

The dashboard may request enablement, but only this module decides whether a
feature is actually available. Payment, inventory, refund, authentication and
audit controls are deliberately not feature flags.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class FeatureKey(StrEnum):
    OFFERS = "offers"
    REFERRALS = "referrals"
    RESELLER_API = "reseller_api"
    RESTOCK_ANNOUNCEMENTS = "restock_announcements"
    LOW_STOCK_ALERTS = "low_stock_alerts"
    LOCALIZATION = "localization"


class FeatureState(StrEnum):
    DISABLED = "disabled"
    SETUP_REQUIRED = "setup_required"
    ENABLED = "enabled"


@dataclass(frozen=True)
class FeatureEvaluation:
    state: FeatureState
    missing: tuple[str, ...]


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _non_negative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _non_empty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _missing_requirements(
    feature: FeatureKey,
    config: Mapping[str, object],
    facts: Mapping[str, object],
) -> tuple[str, ...]:
    if feature is FeatureKey.OFFERS:
        return () if _positive_integer(facts.get("active_promotion_count")) else (
            "active promotion",
        )

    if feature is FeatureKey.REFERRALS:
        missing = []
        if config.get("reward_policy") not in {"discount", "store_credit", "commission"}:
            missing.append("reward policy")
        if not _positive_integer(config.get("hold_days")):
            missing.append("positive hold period")
        if not _positive_integer(config.get("per_user_limit")):
            missing.append("positive per-user limit")
        return tuple(missing)

    if feature is FeatureKey.RESELLER_API:
        missing = []
        terms_url = config.get("terms_url")
        if not (_non_empty_text(terms_url) and str(terms_url).startswith("https://")):
            missing.append("API terms URL")
        if not _positive_integer(facts.get("authorized_client_count")):
            missing.append("authorized API client")
        return tuple(missing)

    if feature is FeatureKey.RESTOCK_ANNOUNCEMENTS:
        missing = []
        destination = config.get("destination_chat_id")
        if not isinstance(destination, int) or isinstance(destination, bool) or destination == 0:
            missing.append("destination chat")
        if facts.get("bot_can_post") is not True:
            missing.append("verified bot posting permission")
        return tuple(missing)

    if feature is FeatureKey.LOW_STOCK_ALERTS:
        missing = []
        if not _non_empty_text(config.get("owner_destination")):
            missing.append("owner notification destination")
        if not _non_negative_integer(config.get("threshold")):
            missing.append("non-negative stock threshold")
        return tuple(missing)

    if feature is FeatureKey.LOCALIZATION:
        return () if _positive_integer(facts.get("complete_locale_count")) else (
            "complete additional locale",
        )

    raise ValueError(f"unknown feature: {feature}")


def evaluate_feature(
    feature: FeatureKey,
    *,
    requested_enabled: bool,
    config: Mapping[str, object],
    facts: Mapping[str, object],
) -> FeatureEvaluation:
    """Resolve requested enablement into an enforceable server state."""

    if not isinstance(feature, FeatureKey):
        raise ValueError(f"unknown feature: {feature}")
    if not requested_enabled:
        return FeatureEvaluation(FeatureState.DISABLED, ())

    missing = _missing_requirements(feature, config, facts)
    if missing:
        return FeatureEvaluation(FeatureState.SETUP_REQUIRED, missing)
    return FeatureEvaluation(FeatureState.ENABLED, ())
