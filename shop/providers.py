"""Supplier provider registry. New reseller APIs plug in here.

A provider is a supplier with a real buyer/reseller API (like Canboso).
Routing and pricing are provider-agnostic; fulfillment still requires a
client implementation for that provider (see shop/canboso.py for the shape:
products(), balance(), purchase()).

Providers without a documented public buyer API are registered so their
SKUs can be mapped, routed and priced, but purchasing stays locked: the
registry deliberately carries no guessed endpoints, auth headers or URLs.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ShopError


@dataclass(frozen=True)
class Provider:
    name: str             # Registry key; stored in mappings, candidates and intents.
    display: str          # Human-facing name for operator messages.
    config_section: str   # Section in config.local.json.
    api_key_env: str      # Environment variable overriding the configured key.
    documented: bool      # A real, documented buyer API client exists.


PROVIDERS: dict[str, Provider] = {
    entry.name: entry
    for entry in (
        Provider("canboso", "Canboso", "canboso", "CANBOSO_API_KEY", documented=True),
        Provider("jaha_digital", "Jaha Digital", "jaha_digital", "JAHA_DIGITAL_API_KEY",
                 documented=False),
        Provider("elite_emporium", "Elite Digital Emporium", "elite_emporium",
                 "ELITE_EMPORIUM_API_KEY", documented=False),
        Provider("acczone", "Acczone", "acczone", "ACCZONE_API_KEY", documented=False),
    )
}


def registered(name: str) -> bool:
    return name in PROVIDERS


def entry(name: str) -> Provider | None:
    # Tests may monkeypatch PROVIDERS with a plain set of names.
    found = PROVIDERS.get(name) if isinstance(PROVIDERS, dict) else None
    return found if isinstance(found, Provider) else None


def display(name: str) -> str:
    known = entry(name)
    return known.display if known else name


def _hooks(name: str):
    """Provider-specific spec validation and request building, imported lazily
    so this registry never depends on client modules (which need config)."""
    if name == "canboso":
        from . import canboso
        return canboso
    return None


def validate_spec(specification: dict) -> None:
    """Provider-specific mapping rules on top of the generic checks. Only
    fields a provider's documented API supports may pass through."""
    hooks = _hooks(str(specification.get("provider", "")))
    if hooks is not None:
        hooks.validate_mapping_spec(specification)
    elif specification.get("slot_months") is not None:
        raise ShopError("Do not send slot_months for catalog slots or account products")


def build_request(specification: dict, settings, email: str | None) -> dict:
    """The exact, immutable purchase body for one provider. Undocumented
    providers never reach the network: there is no request to build."""
    provider = str(specification.get("provider", ""))
    hooks = _hooks(provider)
    if hooks is None:
        raise ShopError(
            f"{display(provider)} purchasing is not connected: this supplier has no "
            "documented buyer API client yet"
        )
    return hooks.build_purchase_body(settings, specification, email)
