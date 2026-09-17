"""Screenshot-inspired storefront, shared verbatim with worker/src/storefront.py.

Only presentation lives here: no balances, supplier keys, purchases or rewards.
Telegram clients control fonts, wallpaper and final rendering of button styles.
"""

from html import escape

MENU = (
    (("🛍 Products", "cat:0"),),
    (("📋 Orders", "orders"), ("👤 Profile", "profile")),
    (("⭐ Payments", "payments"), ("🛟 Support", "support")),
)


def menu_rows():
    return [
        [{"text": text, "callback_data": data, "style": "success"} for text, data in row]
        for row in MENU
    ]


def back_rows():
    return [[{"text": "🏠 Main menu", "callback_data": "home", "style": "success"}]]


def welcome_text(shop_name, *, test_mode=False, paused=False):
    text = (
        "<blockquote>🛍 Digital products\n"
        f"🏛 Welcome to <b>{escape(str(shop_name)[:64])}</b>\n"
        "<b>Main Menu</b>\n"
        "⭐ Payments: <b>Telegram Stars</b>\n"
        "Choose an option below 👇</blockquote>"
    )
    if test_mode:
        text += "\n\nTEST ENVIRONMENT — sample items are not real products."
    if paused:
        text += "\nCheckout is paused while the shop is being configured."
    return text


def information_text(page, user_id):
    pages = {
        "offers": (
            "🔥 Offers\n\nNo promotions are configured yet. "
            "Browse Products for the current catalog and prices."
        ),
        "profile": (
            f"👤 Your profile\n\nTelegram user ID: {int(user_id)}\n"
            "Payment method: Telegram Stars\n"
            "Your purchases and their status are in Orders.\n\n"
            "This shop does not hold a USDT balance or a customer Stars wallet."
        ),
        "payments": (
            "⭐ Payments\n\nChoose a product, read and accept the shop terms, "
            "then pay its Telegram Stars invoice. Telegram handles your Stars balance "
            "and any available purchase options.\n\n"
            "There is no USDT top-up or shop wallet. "
            "For payment issues or refund requests, use /paysupport."
        ),
        "referrals": (
            "🎁 Referrals\n\nThe referral program is not enabled yet. "
            "No referral codes, commissions or reward balances are being issued."
        ),
        "api": (
            "🔗 Reseller API\n\nCustomer API access is not available yet. "
            "The supplier connection is private and handled by the shop. "
            "No supplier API keys are exposed here."
        ),
    }
    return pages[page]
