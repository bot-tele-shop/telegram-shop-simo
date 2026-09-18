"""Supplier provider registry. New reseller APIs plug in here.

A provider is a supplier with a real buyer/reseller API (like Canboso).
Routing and pricing are provider-agnostic; fulfillment still requires a
client implementation for that provider (see shop/canboso.py for the shape:
products(), balance(), purchase()).
"""

PROVIDERS = frozenset({"canboso"})


def registered(name: str) -> bool:
    return name in PROVIDERS
