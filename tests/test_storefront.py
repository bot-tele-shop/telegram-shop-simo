from dataclasses import replace
from pathlib import Path

import pytest
from aiogram.methods import AnswerCallbackQuery, SendInvoice, SendMessage
from aiogram.types import Chat
from test_bot import callback, feed, message
from test_bot import harness as harness

from shop.bot import build_dispatcher
from shop.storefront import MENU, information_text, menu_rows, welcome_text


def last_message(h):
    return next(x for x in reversed(h.session.calls) if isinstance(x, SendMessage))


def test_start_matches_reference_layout_without_wallet(harness):
    feed(harness, message=message('/start'))
    response = last_message(harness)
    assert response.parse_mode == 'HTML'
    assert '<blockquote>' in response.text
    assert harness.settings.shop_name in response.text
    assert 'TEST ENVIRONMENT' in response.text
    assert 'Telegram Stars' in response.text and 'USDT' not in response.text
    rows = response.reply_markup.inline_keyboard
    assert [len(row) for row in rows] == [1, 2, 2]
    assert [[b.callback_data for b in row] for row in rows] == [
        ['cat:0'], ['orders', 'profile'], ['payments', 'support']
    ]
    assert all(b.style == 'success' for row in rows for b in row)
    assert not any(isinstance(c, SendInvoice) for c in harness.session.calls)


@pytest.mark.parametrize('command', ['/start', '/menu'])
def test_start_and_menu_do_not_depend_on_catalog(harness, command, monkeypatch):
    monkeypatch.setattr(harness.store, 'list_products', lambda: pytest.fail('Home queried catalog'))
    feed(harness, message=message(command))
    assert 'Main Menu' in last_message(harness).text


@pytest.mark.parametrize('data', [data for row in MENU for _, data in row] + ['home'])
def test_every_main_menu_button_answers_and_renders(harness, data):
    feed(harness, callback_query=callback(data))
    assert any(isinstance(c, AnswerCallbackQuery) for c in harness.session.calls)
    assert last_message(harness).text
    assert not any(isinstance(c, SendInvoice) for c in harness.session.calls)


@pytest.mark.parametrize('data', ['profile', 'offers', 'payments', 'referrals', 'api'])
def test_info_pages_are_honest_and_have_navigation(harness, data):
    feed(harness, callback_query=callback(data))
    response = last_message(harness)
    assert response.text == information_text(data, 101)
    buttons = [b.callback_data for row in response.reply_markup.inline_keyboard for b in row]
    assert {'home', 'cat:0', 'orders'} <= set(buttons)


def test_welcome_escapes_shop_brand_and_has_no_warranty_promise():
    text = welcome_text('<a href="bad">Brand & Co</a>', paused=True)
    assert '<a href=' not in text and '&lt;a' in text and '&amp;' in text
    assert 'paused' in text and 'warranty' not in text.lower()


def test_customer_profile_is_bound_to_sender(harness):
    feed(harness, callback_query=callback('profile', user_id=102))
    assert '102' in last_message(harness).text
    assert '101' not in last_message(harness).text


def test_private_menu_not_shown_in_group(harness):
    m = message('/start').model_copy(update={'chat': Chat(id=-123, type='group')})
    feed(harness, message=m)
    assert not harness.session.calls


def test_paused_storefront_discloses_checkout_state(harness):
    settings = replace(harness.settings, enable_sales=False)
    harness.dp = build_dispatcher(settings, harness.store, harness.worker)
    feed(harness, message=message('/start'))
    assert 'paused' in last_message(harness).text


def test_products_still_use_original_catalog(harness):
    feed(harness, callback_query=callback('cat:0'))
    response = last_message(harness)
    buttons = [b.callback_data for row in response.reply_markup.inline_keyboard for b in row]
    assert 'p:sample-key' in buttons and 'home' in buttons


def test_callback_payloads_fit_telegram_limit():
    assert all(1 <= len(b['callback_data'].encode()) <= 64 for row in menu_rows() for b in row)


def test_runtime_presentation_copies_are_identical():
    root = Path(__file__).resolve().parents[1]
    assert (root / 'shop/storefront.py').read_bytes() == (root / 'worker/src/storefront.py').read_bytes()
