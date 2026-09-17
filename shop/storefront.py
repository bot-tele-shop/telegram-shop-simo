"""Native Telegram storefront presentation shared by both bot runtimes.

Only presentation lives here: balances, purchases and rewards must always come
from trusted application state. Telegram clients control fonts, wallpaper and
the final rendering of button styles.
"""

from html import escape

MENU = (
    (("🛍 Products", "cat:0"),),
    (("🔥 Offers", "offers"),),
    (("👤 Profile", "profile"), ("📋 Orders", "orders")),
    (("⭐ Payments", "payments"), ("🎁 Referrals", "referrals")),
    (("🛟 Support", "support"), ("🔗 API", "api")),
)

QUICK_MENU = (
    ("🛍 Products", "🛟 Support"),
    ("⭐ Payments",),
    ("🔗 API",),
)


def menu_rows():
    return [
        [{"text": text, "callback_data": data, "style": "success"} for text, data in row]
        for row in MENU
    ]


def quick_menu_rows():
    return [[{"text": text} for text in row] for row in QUICK_MENU]


def back_rows():
    return [[{"text": "🏠 Main menu", "callback_data": "home", "style": "success"}]]


def welcome_text(shop_name, *, test_mode=False, paused=False):
    text = (
        "<blockquote>🛍 <b>Digital products</b> ✅\n"
        f"🏛 Welcome to <b>{escape(str(shop_name)[:64])}</b> 🏛\n"
        "<b>Main Menu</b>\n"
        "⭐ Secure checkout with <b>Telegram Stars</b>\n"
        "⚡ Automatic delivery after confirmed payment\n"
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
            "🔥 Offers\n\nSpecial promotions are not configured yet. "
            "Open Products to see every live item and its current price."
        ),
        "profile": (
            f"👤 Your profile\n\nTelegram user ID: {int(user_id)}\n"
            "Payment method: Telegram Stars\n"
            "Your purchases and their status are in Orders.\n\n"
            "This shop does not hold a USDT balance or a customer Stars wallet."
        ),
        "payments": (
            "⭐ Payments\n\n1. Choose a product.\n2. Review and accept the shop terms.\n"
            "3. Pay the Telegram Stars invoice.\n4. Receive your item after payment is confirmed.\n\n"
            "Telegram manages your Stars balance securely. This shop does not hold a "
            "separate USDT balance. For payment or refund help, open Support."
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
