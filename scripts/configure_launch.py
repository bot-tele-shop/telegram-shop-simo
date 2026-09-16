"""Write drafted terms/privacy and the disabled Canboso block into config.local.json."""
import json
from pathlib import Path

path = Path("/tasklet/threads/a_53xtjeh6apz8anh8d6dj/work/telegram-shop/config.local.json")
raw = json.loads(path.read_text(encoding="utf-8"))

raw["terms_text"] = (
    "Digital Shelf - Terms of Sale\n\n"
    "1. Digital goods. We sell digital items (license keys, codes, account credentials) "
    "delivered in this chat after payment confirmation. Delivery is usually immediate; "
    "items sourced from a supplier may take additional handling time, shown in /orders.\n\n"
    "2. Payment. Prices are in Telegram Stars (XTR) and processed entirely by Telegram. "
    "We never see or store your card details. An order is confirmed only when Telegram "
    "reports a successful payment.\n\n"
    "3. Refunds. You are entitled to a full refund in Stars if your item is not delivered, "
    "is invalid on arrival, or becomes unavailable before fulfillment. Request it via "
    "/paysupport with your order ID within 14 days of payment. Refunds are full (not "
    "partial) and return to the paying Telegram account. Once refunded, the delivered "
    "code is revoked, quarantined, and never resold. Successfully redeemed items are "
    "not refundable.\n\n"
    "4. Acceptable use. Items are for your personal use. Do not resell or share delivered "
    "codes unless the product explicitly allows it. We may cancel and refund orders in "
    "cases of abuse or payment fraud.\n\n"
    "5. Liability. Our total liability is limited to the amount you paid. Availability "
    "depends on Telegram and third-party providers.\n\n"
    "By tapping 'I accept' you agree to these terms and the privacy notice. Terms may "
    "change; the version you accepted is recorded with each order."
)

raw["privacy_text"] = (
    "Digital Shelf - Privacy Notice\n\n"
    "What we store: your Telegram user ID, your orders, payment charge references "
    "(never card data - payments are processed entirely by Telegram), delivered codes "
    "(encrypted at rest), and messages you send to the bot.\n\n"
    "Why: to process payments, deliver purchases, prevent fraud, and handle refunds "
    "and support requests.\n\n"
    "What we never do: we do not sell or share your data with advertisers, we do not "
    "see your payment card, and we do not message you outside order-related replies.\n\n"
    "Retention: order and payment records are kept for accounting and dispute handling. "
    "You may request a copy or deletion of your personal data via /paysupport where "
    "legally applicable.\n\n"
    "Security: delivered digital codes are encrypted in our storage. Payment data is "
    "handled by Telegram under its own privacy policy.\n\n"
    "This notice may change; the version in force is always shown here."
)

raw.setdefault("canboso", {})
raw["canboso"].update({
    "enabled": False,
    "allow_purchases": False,
    "resale_authorized": False,
    "acknowledge_price_race": False,
    "budget_currency": "",
    "spend_budget": "0",
})

path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
print("terms chars:", len(raw["terms_text"]))
print("privacy chars:", len(raw["privacy_text"]))
print("config updated")
