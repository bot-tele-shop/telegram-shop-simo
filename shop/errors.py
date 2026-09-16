"""Customer-readable errors shared by storage and integrations."""


class ShopError(ValueError):
    """Message is safe for customer display; never include credentials."""
