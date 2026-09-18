"""Local configuration. Secrets never belong in the catalog or source archive."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class CanbosoSettings:
    enabled: bool = False
    api_key: str = field(default="", repr=False)
    allow_purchases: bool = False
    resale_authorized: bool = False
    acknowledge_price_race: bool = False
    budget_currency: str = ""
    spend_budget: str = "0"

    @property
    def key_fingerprint(self) -> str:
        return hashlib.sha256(self.api_key.encode()).hexdigest()

    def problems(self, environment: str) -> list[str]:
        from decimal import Decimal, InvalidOperation

        issues = []
        if self.enabled and (not isinstance(self.api_key, str) or not 8 <= len(self.api_key) <= 512
                             or any(ord(c) < 33 for c in self.api_key)):
            issues.append("Set CANBOSO_API_KEY locally; never put the buyer key in chat or logs")
        if self.allow_purchases:
            if not self.enabled or environment != "production":
                issues.append("Live Canboso purchasing requires enabled integration and production mode")
            if not self.resale_authorized:
                issues.append("Confirm you are authorized to resell the selected supplier products")
            if not self.acknowledge_price_race:
                issues.append("Acknowledge that Canboso has no server-side maximum-price parameter")
            if self.budget_currency not in {"VND", "USD"}:
                issues.append("Set budget_currency to the currency reported by your supplier wallet")
            try:
                budget = Decimal(str(self.spend_budget))
                if not budget.is_finite() or budget <= 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                issues.append("Set a positive cumulative spend_budget before enabling purchases")
        return issues


@dataclass(frozen=True)
class WebhookSettings:
    """Optional. Empty url means long-polling (the default). TLS must terminate
    at a reverse proxy in front of listen_host:listen_port."""

    url: str = ""
    listen_host: str = "127.0.0.1"
    listen_port: int = 8081
    path: str = "/telegram/webhook"
    secret: str = field(default="", repr=False)

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def problems(self) -> list[str]:
        if not self.enabled:
            return []
        issues = []
        if not re.fullmatch(r"https://\S+", self.url):
            issues.append("webhook.url must be a full https:// URL reachable by Telegram")
        if not 1 <= len(self.secret) <= 256 or not re.fullmatch(r"[A-Za-z0-9_-]+", self.secret):
            issues.append(
                "webhook.secret must be 1-256 characters of A-Z a-z 0-9 _ - "
                "(Telegram secret_token rules); it authenticates every request"
            )
        if not 1 <= self.listen_port <= 65535:
            issues.append("webhook.listen_port must be 1-65535")
        if not self.path.startswith("/") or len(self.path) > 128 or ".." in self.path:
            issues.append("webhook.path must start with / and stay under 128 characters")
        return issues


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(default="", repr=False)
    admin_ids: frozenset[int] = frozenset()
    shop_name: str = "Digital Shelf"
    support_contact: str = ""
    environment: str = "test"
    enable_sales: bool = False
    production_acknowledged: bool = False
    database_path: Path = PROJECT_ROOT / "data" / "shop-test.sqlite3"
    stock_encryption_key: str = field(default="", repr=False)
    terms_text: str = ""
    privacy_text: str = ""
    canboso: CanbosoSettings = field(default_factory=CanbosoSettings)
    webhook: WebhookSettings = field(default_factory=WebhookSettings)

    @property
    def terms_version(self) -> str:
        content = self.terms_text + "\n" + self.privacy_text
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def problems(self, *, require_bot: bool = True) -> list[str]:
        issues = []
        if self.environment not in {"test", "production"}:
            issues.append("environment must be test or production")
        try:
            Fernet(self.stock_encryption_key.encode())
            base64.urlsafe_b64decode(self.stock_encryption_key)
        except (ValueError, TypeError):
            issues.append("Set a valid stock_encryption_key; init creates one locally")
        if not 1 <= len(self.shop_name) <= 64:
            issues.append("shop_name must contain 1-64 characters")
        if require_bot:
            if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,}", self.bot_token):
                issues.append("Set bot_token locally, or use the BOT_TOKEN environment variable")
            if not self.admin_ids or any(x <= 0 for x in self.admin_ids):
                issues.append("Set at least one positive numeric Telegram admin ID")
            if not self.support_contact.strip():
                issues.append("Set a working merchant support contact")
            if not 30 <= len(self.terms_text.strip()) <= 3000:
                issues.append(
                    "Write your real terms_text (30-3000 characters), including refund terms"
                )
            if not 30 <= len(self.privacy_text.strip()) <= 3000:
                issues.append("Write your real privacy_text (30-3000 characters)")
        if self.environment == "production" and not self.production_acknowledged:
            issues.append("Production is locked until production_acknowledged is true")
        issues.extend(self.canboso.problems(self.environment))
        issues.extend(self.webhook.problems())
        return issues

    def validate(self, *, require_bot: bool = True) -> None:
        issues = self.problems(require_bot=require_bot)
        if issues:
            raise ValueError("\n".join(issues))


def load_settings(path: Path | None = None) -> Settings:
    path = (path or PROJECT_ROOT / "config.local.json").resolve()
    if not path.is_file():
        raise ValueError("Local config is missing. Run: python -m shop init")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a JSON object")
    for name in ("enable_sales", "production_acknowledged"):
        if name in raw and type(raw[name]) is not bool:
            raise ValueError(f"{name} must be a JSON boolean, not a string")
    admin_ids = raw.get("admin_ids", [])
    if os.getenv("ADMIN_IDS"):
        admin_ids = [int(x.strip()) for x in os.environ["ADMIN_IDS"].split(",") if x.strip()]
    if not isinstance(admin_ids, list) or any(type(x) is not int for x in admin_ids):
        raise ValueError("admin_ids must be a JSON array of integers")
    supplier = raw.get("canboso", {})
    if not isinstance(supplier, dict):
        raise ValueError("canboso must be a JSON object")
    for name in ("enabled", "allow_purchases", "resale_authorized", "acknowledge_price_race"):
        if name in supplier and type(supplier[name]) is not bool:
            raise ValueError(f"canboso.{name} must be a JSON boolean")
    canboso = CanbosoSettings(
        enabled=supplier.get("enabled", False),
        api_key=os.getenv("CANBOSO_API_KEY", supplier.get("api_key", "")),
        allow_purchases=supplier.get("allow_purchases", False),
        resale_authorized=supplier.get("resale_authorized", False),
        acknowledge_price_race=supplier.get("acknowledge_price_race", False),
        budget_currency=supplier.get("budget_currency", ""),
        spend_budget=str(supplier.get("spend_budget", "0")),
    )
    webhook_raw = raw.get("webhook", {})
    if not isinstance(webhook_raw, dict):
        raise ValueError("webhook must be a JSON object")
    webhook = WebhookSettings(
        url=os.getenv("WEBHOOK_URL", webhook_raw.get("url", "")),
        listen_host=webhook_raw.get("listen_host", "127.0.0.1"),
        listen_port=int(webhook_raw.get("listen_port", 8081)),
        path=webhook_raw.get("path", "/telegram/webhook"),
        secret=os.getenv("WEBHOOK_SECRET", webhook_raw.get("secret", "")),
    )
    db = Path(raw.get("database_path", "data/shop-test.sqlite3"))
    if not db.is_absolute():
        db = path.parent / db
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", raw.get("bot_token", "")),
        admin_ids=frozenset(admin_ids),
        shop_name=raw.get("shop_name", "Digital Shelf"),
        support_contact=raw.get("support_contact", ""),
        environment=raw.get("environment", "test"),
        enable_sales=raw.get("enable_sales", False),
        production_acknowledged=raw.get("production_acknowledged", False),
        database_path=db.resolve(),
        stock_encryption_key=os.getenv("STOCK_ENCRYPTION_KEY", raw.get("stock_encryption_key", "")),
        terms_text=raw.get("terms_text", ""),
        privacy_text=raw.get("privacy_text", ""),
        canboso=canboso,
        webhook=webhook,
    )


def initialize_config(path: Path | None = None) -> Path:
    path = (path or PROJECT_ROOT / "config.local.json").resolve()
    if not path.parent.is_dir():
        raise ValueError("Configuration parent directory does not exist")
    raw = json.loads((PROJECT_ROOT / "config.example.json").read_text(encoding="utf-8"))
    raw["stock_encryption_key"] = Fernet.generate_key().decode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(raw, stream, indent=2)
        stream.write("\n")
    return path
