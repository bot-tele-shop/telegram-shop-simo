from uuid import uuid4

import pytest
from pydantic import ValidationError

from digital_shelf.catalog import (
    AssetReference,
    CategoryCreateCommand,
    FulfillmentType,
    InventoryPolicy,
    ProductCreateCommand,
    ProductStatus,
    normalize_sku,
    validate_asset_reference,
    validate_fulfillment_policy,
)


@pytest.mark.parametrize(
    ("fulfillment", "inventory"),
    [
        (FulfillmentType.UNIQUE_CODE, InventoryPolicy.FINITE_UNIQUE),
        (FulfillmentType.UNIQUE_URL, InventoryPolicy.FINITE_UNIQUE),
        (FulfillmentType.DOWNLOAD_FILE, InventoryPolicy.FINITE_UNIQUE),
        (FulfillmentType.DOWNLOAD_FILE, InventoryPolicy.UNLIMITED),
        (FulfillmentType.REUSABLE_CONTENT, InventoryPolicy.UNLIMITED),
        (FulfillmentType.MANUAL, InventoryPolicy.MANUAL),
        (FulfillmentType.SUBSCRIPTION_ACCESS, InventoryPolicy.MANUAL),
        (FulfillmentType.SUBSCRIPTION_ACCESS, InventoryPolicy.UNLIMITED),
    ],
)
def test_allowed_fulfillment_inventory_pairs(
    fulfillment: FulfillmentType, inventory: InventoryPolicy
) -> None:
    assert validate_fulfillment_policy(fulfillment, inventory) == ()


@pytest.mark.parametrize(
    ("fulfillment", "inventory", "reason"),
    [
        (FulfillmentType.UNIQUE_CODE, InventoryPolicy.UNLIMITED, "unique_code requires finite_unique"),
        (FulfillmentType.UNIQUE_URL, InventoryPolicy.MANUAL, "unique_url requires finite_unique"),
        (FulfillmentType.REUSABLE_CONTENT, InventoryPolicy.FINITE_UNIQUE, "reusable_content requires unlimited"),
        (FulfillmentType.MANUAL, InventoryPolicy.FINITE_QUANTITY, "manual requires manual"),
        (FulfillmentType.DOWNLOAD_FILE, InventoryPolicy.FINITE_QUANTITY, "download_file requires finite_unique or unlimited"),
    ],
)
def test_invalid_fulfillment_inventory_pairs_are_rejected(
    fulfillment: FulfillmentType, inventory: InventoryPolicy, reason: str
) -> None:
    assert validate_fulfillment_policy(fulfillment, inventory) == (reason,)


def test_sku_is_normalized_and_rejects_control_or_oversized_input() -> None:
    assert normalize_sku("  claude-pro-12m  ") == "CLAUDE-PRO-12M"
    for invalid in ("", "a\nb", "x" * 65, "sku/with/slash"):
        with pytest.raises(ValueError, match="SKU"):
            normalize_sku(invalid)


def test_category_command_normalizes_slug_and_requires_real_name() -> None:
    command = CategoryCreateCommand(slug="  ai-tools  ", name=" AI Tools ", position=0)

    assert command.slug == "ai-tools"
    assert command.name == "AI Tools"

    with pytest.raises(ValidationError):
        CategoryCreateCommand(slug="bad slug", name="")


def test_multiline_fields_accept_newlines_but_still_reject_control_characters() -> None:
    command = ProductCreateCommand(
        category_id=uuid4(),
        sku="multiline",
        title="Multiline",
        description="first line\nsecond line",
        price_stars=5,
        fulfillment_type=FulfillmentType.REUSABLE_CONTENT,
        inventory_policy=InventoryPolicy.UNLIMITED,
    )
    assert "\n" in command.description

    with pytest.raises(ValidationError, match="control characters"):
        ProductCreateCommand(
            category_id=uuid4(),
            sku="control",
            title="Control",
            description="bad\x07description",
            price_stars=5,
            fulfillment_type=FulfillmentType.REUSABLE_CONTENT,
            inventory_policy=InventoryPolicy.UNLIMITED,
        )

    with pytest.raises(ValidationError, match="control characters"):
        ProductCreateCommand(
            category_id=uuid4(),
            sku="newline-title",
            title="bad\ntitle",
            description="Description",
            price_stars=5,
            fulfillment_type=FulfillmentType.REUSABLE_CONTENT,
            inventory_policy=InventoryPolicy.UNLIMITED,
        )


def test_product_command_requires_valid_configuration_and_positive_stars_price() -> None:
    command = ProductCreateCommand(
        category_id=uuid4(),
        sku=" chatgpt-plus ",
        title=" ChatGPT Plus ",
        description="Access",
        price_stars=50,
        fulfillment_type=FulfillmentType.UNIQUE_CODE,
        inventory_policy=InventoryPolicy.FINITE_UNIQUE,
    )

    assert command.sku == "CHATGPT-PLUS"
    assert command.title == "ChatGPT Plus"
    assert command.status is ProductStatus.DRAFT

    with pytest.raises(ValidationError):
        ProductCreateCommand(
            category_id=uuid4(),
            sku="item",
            title="Item",
            description="Description",
            price_stars=0,
            fulfillment_type=FulfillmentType.UNIQUE_CODE,
            inventory_policy=InventoryPolicy.FINITE_UNIQUE,
        )

    with pytest.raises(ValidationError, match="fulfillment"):
        ProductCreateCommand(
            category_id=uuid4(),
            sku="item",
            title="Item",
            description="Description",
            price_stars=1,
            fulfillment_type=FulfillmentType.UNIQUE_CODE,
            inventory_policy=InventoryPolicy.UNLIMITED,
        )


def test_product_status_cannot_be_published_without_explicit_ready_inputs() -> None:
    with pytest.raises(ValidationError, match="draft or ready"):
        ProductCreateCommand(
            category_id=uuid4(),
            sku="item",
            title="Item",
            description="Description",
            price_stars=1,
            fulfillment_type=FulfillmentType.MANUAL,
            inventory_policy=InventoryPolicy.MANUAL,
            status=ProductStatus.AVAILABLE,
        )


@pytest.mark.parametrize(
    "reference",
    [
        AssetReference(storage_key="products/item/image.webp"),
        AssetReference(telegram_file_id="AgACAgQAAxkBAAIB"),
    ],
)
def test_asset_reference_accepts_private_storage_or_telegram_file_id(
    reference: AssetReference,
) -> None:
    assert validate_asset_reference(reference) == reference


@pytest.mark.parametrize(
    "reference",
    [
        AssetReference(storage_key="https://public.example/image.webp"),
        AssetReference(telegram_file_id=""),
    ],
)
def test_asset_reference_rejects_public_or_empty_reference(reference: AssetReference) -> None:
    with pytest.raises(ValueError, match="asset"):
        validate_asset_reference(reference)
