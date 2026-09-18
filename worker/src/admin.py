"""Private admin API for the Digital Shelf dashboard.

Auth model: the dashboard signs in with Supabase Auth and sends the user JWT
as a Bearer token. Every request is validated server-side against Supabase
AND against the server-controlled owner allowlist (OWNER_EMAILS env). A hidden
URL, browser flag or supplied Telegram ID is never authorization.

Bearer tokens (not cookies) are used, so there is no CSRF surface; CORS is
restricted to the dashboard origin. Service-role and encryption keys never
leave the Worker.
"""

import re

from db import DB
from fernet import Fernet
from telegram import Telegram

MAX_BODY_BYTES = 64 * 1024
MAX_STOCK_LINES = 500
MAX_LINE_LEN = 1500
EDITABLE_SETTINGS = ("shop_name", "support_contact", "terms_text", "privacy_text", "checkout_paused")
SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")


class AdminError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class Admin:
    def __init__(self, env):
        self.env = env
        self.db = DB(str(env.SUPABASE_URL), str(env.SUPABASE_SERVICE_ROLE_KEY))
        self.fernet = Fernet(str(env.STOCK_FERNET_KEY))
        self.tg = Telegram(str(env.TELEGRAM_BOT_TOKEN))
        self.owner_emails = {
            e.strip().lower()
            for e in str(getattr(env, "OWNER_EMAILS", "") or "").split(",")
            if e.strip()
        }
        self.origin = str(getattr(env, "DASHBOARD_ORIGIN", "") or "")

    # ---- auth ---------------------------------------------------------------

    async def authorize(self, request):
        """Return the owner email, or raise AdminError(401/403)."""
        auth = str(request.headers.get("authorization") or "")
        if not auth.lower().startswith("bearer "):
            raise AdminError(401, "missing bearer token")
        token = auth[7:].strip()
        if not token or len(token) > 8192:
            raise AdminError(401, "invalid token")
        # Validate the user JWT against Supabase Auth on every request.
        user = await self.db.request_auth_user(token)
        email = str((user or {}).get("email") or "").lower()
        if not email:
            raise AdminError(401, "invalid session")
        if email not in self.owner_emails:
            raise AdminError(403, "not an owner account")
        return email

    async def audit(self, actor, action, target=None, detail=None):
        try:
            await self.db.insert(
                "admin_audit",
                {"actor": actor, "action": action, "target": target, "detail": detail or {}},
            )
        except Exception:
            pass  # never leak; audit failure must not expose internals

    # ---- body parsing -------------------------------------------------------

    async def body(self, request):
        length = request.headers.get("content-length")
        if length is not None and int(str(length)) > MAX_BODY_BYTES:
            raise AdminError(413, "request too large")
        try:
            data = await request.json()
            if hasattr(data, "to_py"):
                data = data.to_py()
        except Exception:
            raise AdminError(400, "invalid JSON body") from None
        if not isinstance(data, dict):
            raise AdminError(400, "expected a JSON object")
        return data

    # ---- endpoints ----------------------------------------------------------

    async def overview(self, actor):
        return await self.db.rpc("admin_overview", {})

    async def analytics(self, actor):
        try:
            return await self.db.rpc("admin_analytics", {})
        except Exception as exc:
            # httpclient.HttpError without the import: tests stub the module.
            if getattr(exc, "status", None) == 404:
                raise AdminError(
                    503, "analytics unavailable: apply migration 0005_admin_analytics.sql"
                ) from None
            raise

    async def list_products(self, actor):
        return await self.db.rpc("admin_products", {})

    async def create_product(self, actor, data):
        sku = str(data.get("sku") or "").strip()
        title = str(data.get("title") or "").strip()
        description = str(data.get("description") or "").strip()
        category = str(data.get("category") or "general").strip()[:64]
        price = data.get("price_stars")
        if not SKU_RE.match(sku):
            raise AdminError(400, "SKU must be 2-64 chars: letters, digits, - or _")
        if not 1 <= len(title) <= 64:
            raise AdminError(400, "title must be 1-64 characters")
        if len(description) > 500:
            raise AdminError(400, "description must be at most 500 characters")
        if not isinstance(price, int) or isinstance(price, bool) or not 1 <= price <= 100000:
            raise AdminError(400, "price_stars must be a whole number between 1 and 100000")
        existing = await self.db.select_one("products", {"sku": f"eq.{sku}", "select": "sku"})
        if existing:
            raise AdminError(409, "a product with this SKU already exists")
        await self.db.insert(
            "products",
            {
                "sku": sku,
                "title": title,
                "description": description,
                "category": category,
                "price_stars": price,
                "active": False,  # new products are always inactive first
                "source": "stock",
            },
        )
        await self.audit(actor, "product.create", sku, {"title": title, "price_stars": price})
        return {"ok": True, "sku": sku}

    async def update_product(self, actor, data):
        sku = str(data.get("sku") or "").strip()
        if not SKU_RE.match(sku):
            raise AdminError(400, "invalid SKU")
        product = await self.db.select_one("products", {"sku": f"eq.{sku}", "select": "*"})
        if not product:
            raise AdminError(404, "product not found")
        changes = {}
        if "title" in data:
            title = str(data["title"]).strip()
            if not 1 <= len(title) <= 64:
                raise AdminError(400, "title must be 1-64 characters")
            changes["title"] = title
        if "description" in data:
            description = str(data["description"]).strip()
            if len(description) > 500:
                raise AdminError(400, "description must be at most 500 characters")
            changes["description"] = description
        if "category" in data:
            changes["category"] = str(data["category"]).strip()[:64] or "general"
        if "price_stars" in data:
            price = data["price_stars"]
            if not isinstance(price, int) or isinstance(price, bool) or not 1 <= price <= 100000:
                raise AdminError(400, "price_stars must be a whole number between 1 and 100000")
            changes["price_stars"] = price
        if "active" in data:
            changes["active"] = bool(data["active"])
        if not changes:
            raise AdminError(400, "nothing to update")
        if changes.get("active") is True and product.get("source") == "stock":
            available = await self.db.select(
                "stock",
                {"sku": f"eq.{sku}", "state": "eq.available", "select": "id"},
                limit=1,
            )
            if not available:
                raise AdminError(409, "upload stock before activating this product")
        await self.db.update("products", {"sku": f"eq.{sku}"}, changes)
        await self.audit(actor, "product.update", sku, changes)
        return {"ok": True, "sku": sku, "changes": sorted(changes)}

    async def upload_stock(self, actor, data):
        sku = str(data.get("sku") or "").strip()
        lines = data.get("lines")
        product = await self.db.select_one("products", {"sku": f"eq.{sku}", "select": "sku,source"})
        if not product or product["source"] != "stock":
            raise AdminError(404, "choose an existing stock-backed product")
        if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_STOCK_LINES:
            raise AdminError(400, f"upload 1-{MAX_STOCK_LINES} lines at a time")
        cleaned = []
        for raw in lines:
            line = str(raw).strip()
            if not 1 <= len(line) <= MAX_LINE_LEN or any(ord(c) < 32 for c in line):
                raise AdminError(400, "each line must be 1-1500 printable characters")
            cleaned.append(line)

        # Fingerprint every line, then only insert ones not already stored:
        # duplicates are reported, never overwritten (sold/quarantined rows are
        # protected because fingerprints are unique across ALL states).
        fingerprints = [self.fernet.fingerprint(line) for line in cleaned]
        existing = await self.db.select(
            "stock", {"sku": f"eq.{sku}", "select": "fingerprint"}, limit=10000
        )
        known = {row["fingerprint"] for row in existing}
        rows = []
        seen = set()
        duplicates = 0
        for line, fp in zip(cleaned, fingerprints):
            if fp in known or fp in seen:
                duplicates += 1
                continue
            seen.add(fp)
            rows.append({
                "sku": sku,
                "fingerprint": fp,
                "ciphertext": await self.fernet.encrypt(line),
                "state": "available",
            })
        for row in rows:
            await self.db.insert("stock", row)
        await self.audit(actor, "stock.upload", sku,
                         {"accepted": len(rows), "duplicates": duplicates})
        return {"ok": True, "accepted": len(rows), "duplicates": duplicates,
                "rejected": len(cleaned) - len(rows) - duplicates}

    async def list_stock(self, actor, params):
        sku = str(params.get("sku") or "").strip()
        if not SKU_RE.match(sku):
            raise AdminError(400, "choose a product SKU")
        product = await self.db.select_one("products", {"sku": f"eq.{sku}", "select": "sku"})
        if not product:
            raise AdminError(404, "product not found")
        rows = await self.db.select(
            "stock",
            {
                "sku": f"eq.{sku}",
                "select": "id,state,fingerprint,created_at,order_id",
                "order": "created_at.desc",
            },
            limit=500,
        )
        # Fingerprints are hashes, not codes. Never return ciphertext.
        return [
            {
                "id": row["id"],
                "state": row["state"],
                "fingerprint": str(row.get("fingerprint") or "")[:12],
                "created_at": row.get("created_at"),
                "assigned": bool(row.get("order_id")),
            }
            for row in rows
        ]

    async def list_orders(self, actor, params):
        state = params.get("state") or None
        query = params.get("q") or None
        if state and state not in (
            "invoice", "checkout", "expired", "cancelled", "paid", "delivering",
            "delivered", "delivery_failed", "needs_refund", "refund_pending", "refunded",
        ):
            raise AdminError(400, "unknown state filter")
        return await self.db.rpc(
            "admin_orders", {"p_state": state, "p_query": query, "p_limit": 100}
        )

    async def resend_order(self, actor, data):
        order_id = str(data.get("order_id") or "").strip()
        order = await self.db.select_one("orders", {"id": f"eq.{order_id}", "select": "*"})
        if not order:
            raise AdminError(404, "order not found")
        item = await self.db.select_one(
            "stock", {"order_id": f"eq.{order_id}", "select": "ciphertext"}
        )
        if not item:
            raise AdminError(409, "no code is assigned to this order")
        code = await self.fernet.decrypt(item["ciphertext"])
        await self.tg.send_message(
            order["user_id"],
            f"Your purchase is here!\n\nOrder: {order_id}\n\n{code}\n\n"
            f"Keep this message private. Need help? /paysupport",
        )
        await self.db.rpc("confirm_delivery", {"p_order_id": order_id})
        # Audit without retaining the plaintext code.
        await self.audit(actor, "order.resend", order_id, {"user_id": order["user_id"]})
        return {"ok": True, "order_id": order_id}

    async def refund_order(self, actor, data):
        order_id = str(data.get("order_id") or "").strip()
        if data.get("confirm") is not True:
            raise AdminError(400, "refunds require explicit confirmation")
        order = await self.db.select_one("orders", {"id": f"eq.{order_id}", "select": "*"})
        if not order:
            raise AdminError(404, "order not found")
        if order["state"] in ("refunded",):
            raise AdminError(409, "order is already refunded")
        charge_id = order.get("charge_id")
        if not charge_id:
            raise AdminError(409, "order has no recorded charge to refund")
        try:
            await self.tg.call(
                "refundStarPayment",
                user_id=order["user_id"],
                telegram_payment_charge_id=charge_id,
            )
        except Exception as exc:
            await self.audit(actor, "order.refund_failed", order_id,
                             {"error": f"{type(exc).__name__}"})
            raise AdminError(502, f"Telegram refund call failed: {type(exc).__name__}") from None
        await self.db.rpc(
            "record_refund",
            {"p_charge_id": charge_id, "p_user_id": order["user_id"],
             "p_amount": order["price_stars"]},
        )
        await self.audit(actor, "order.refund", order_id, {"charge_id": charge_id})
        return {"ok": True, "order_id": order_id, "state": "refunded"}

    async def get_settings(self, actor):
        rows = await self.db.select("metadata", {"select": "key,value"}, limit=100)
        settings = {row["key"]: row["value"] for row in rows}
        return {key: settings.get(key, "") for key in EDITABLE_SETTINGS}

    async def update_settings(self, actor, data):
        changes = {}
        for key in EDITABLE_SETTINGS:
            if key not in data:
                continue
            value = str(data[key])
            if key == "checkout_paused":
                value = "true" if data[key] in (True, "true", "on", "1") else "false"
            elif len(value) > 4000:
                raise AdminError(400, f"{key} is too long")
            changes[key] = value
        if not changes:
            raise AdminError(400, "nothing to update")
        for key, value in changes.items():
            await self.db.upsert("metadata", {"key": key, "value": value}, on_conflict="key")
        terms_changed = "terms_text" in changes or "privacy_text" in changes
        await self.audit(actor, "settings.update", None,
                         {"keys": sorted(changes), "terms_changed": terms_changed})
        return {"ok": True, "updated": sorted(changes),
                "terms_changed": terms_changed,
                "note": "Buyers must re-accept the terms at their next checkout."
                if terms_changed else None}


ROUTES = {
    "overview": ("GET", Admin.overview),
    "analytics": ("GET", Admin.analytics),
    "products": ("GET", Admin.list_products),
    "products/create": ("POST", Admin.create_product),
    "products/update": ("POST", Admin.update_product),
    "stock": ("GET", Admin.list_stock),
    "stock/upload": ("POST", Admin.upload_stock),
    "orders": ("GET", Admin.list_orders),
    "orders/resend": ("POST", Admin.resend_order),
    "orders/refund": ("POST", Admin.refund_order),
    "settings": ("GET", Admin.get_settings),
    "settings/update": ("POST", Admin.update_settings),
}


async def handle_admin(request, env, path, query):
    """Dispatch /admin/api/<path>. Returns (status, payload_dict)."""
    admin = Admin(env)
    route = ROUTES.get(path)
    if not route:
        return 404, {"ok": False, "error": "not found"}
    method, handler = route
    if str(request.method) != method:
        return 405, {"ok": False, "error": "method not allowed"}
    actor = await admin.authorize(request)
    if method == "GET":
        result = await handler(admin, actor, query) if path in ("orders", "stock") \
            else await handler(admin, actor)
    else:
        result = await handler(admin, actor, await admin.body(request))
    return 200, {"ok": True, "data": result}
