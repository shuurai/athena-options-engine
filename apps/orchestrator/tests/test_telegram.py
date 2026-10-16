"""Telegram quote commands. No gateway and no live chat."""

import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from datetime import UTC, datetime

from athena_bots.public_url import control_page_url
from athena_bots.telegram import (
    _draft_text,
    _markup,
    format_headlines,
    help_text,
    quote_text,
)
from athena_exec.broker import quote_fields


class TelegramQuoteTest(unittest.TestCase):
    def test_a_number_on_stock_points_to_hk(self) -> None:
        self.assertIn("/hk", quote_text("db", "/stock", "700"))

    def test_hk_drops_a_leading_zero_and_uses_sehk(self) -> None:
        fields = quote_fields("hk", "0700")
        self.assertEqual(fields["symbol"], "700")
        self.assertEqual(fields["exchange"], "SEHK")
        self.assertEqual(fields["currency"], "HKD")
        with patch("athena_exec.broker.IbBroker.connect", return_value=False), patch(
            "athena_bots.telegram._stored_quote", return_value=Decimal("612.5"),
        ):
            text = quote_text("db", "/hk", "0700")
        self.assertIn("700  HK stock  SEHK HKD", text)

    def test_fx_slash_is_the_six_letter_pair(self) -> None:
        fields = quote_fields("fx", "EUR/USD")
        self.assertEqual(fields["base"], "EUR")
        self.assertEqual(fields["currency"], "USD")
        self.assertEqual(fields["symbol"], "EURUSD")
        self.assertEqual(fields["exchange"], "IDEALPRO")

    def test_a_closed_gateway_uses_the_stored_price(self) -> None:
        with patch("athena_exec.broker.IbBroker.connect", return_value=False), patch(
            "athena_bots.telegram._stored_quote", return_value=Decimal("10.5"),
        ):
            text = quote_text("db", "/stock", "AAPL")
        self.assertIn("AAPL  stock  SMART USD  10.50  stored", text)

    def test_help_names_the_quote_and_the_draft_commands(self) -> None:
        text = help_text()
        for name in ("/stock", "/etf", "/hk", "/de", "/fx", "/approve", "/reject", "/drafts", "/halt", "/news"):
            self.assertIn(name, text)
        self.assertIn("lists the stored headlines", text)
        self.assertIn("IFX", text)

    def test_de_quote_fields_use_ibis_eur(self) -> None:
        fields = quote_fields("de", "IFX")
        self.assertEqual(fields["symbol"], "IFX")
        self.assertEqual(fields["currency"], "EUR")
        self.assertEqual(fields["primary"], "IBIS")
        self.assertEqual(fields["exchange"], "SMART")

    def test_a_symbol_lists_the_stored_headline(self) -> None:
        when = datetime(2026, 10, 11, 12, 4, tzinfo=UTC)
        text = format_headlines("AAPL", [(when, "yahoo", "Apple raises guidance")])
        self.assertIn("AAPL  1 stored", text)
        self.assertIn("yahoo", text)
        self.assertIn("Apple raises guidance", text)
        self.assertEqual(format_headlines("AAPL", []), "No stored headline for AAPL.")


class TelegramControlUrlTest(unittest.TestCase):
    def test_control_page_url_uses_public_base(self) -> None:
        with patch.dict(os.environ, {"ATHENA_PUBLIC_BASE_URL": "https://ex.trycloudflare.com", "ATHENA_CONTROL_PORT": ""}, clear=False):
            self.assertEqual(control_page_url("tok"), "https://ex.trycloudflare.com/c/tok")

    def test_control_page_url_falls_back_to_localhost_port(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in {"ATHENA_PUBLIC_BASE_URL"}}
        env["ATHENA_CONTROL_PORT"] = "8787"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(control_page_url("tok"), "http://127.0.0.1:8787/c/tok")

    def test_control_page_url_empty_without_base(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in {"ATHENA_PUBLIC_BASE_URL", "ATHENA_CONTROL_PORT"}}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(control_page_url("tok"), "")

    def test_markup_and_text_include_approve_page(self) -> None:
        url = "https://ex.trycloudflare.com/c/tok"
        text = _draft_text("draft_1", "AAPL", "CLOSE", 1, "MANAGE", "Premium captured 90%.", url, "acct")
        self.assertIn(f"Approve page {url}", text)
        markup = _markup("draft_1", url)
        self.assertEqual(markup["inline_keyboard"][0][0]["url"], url)
        empty = _markup("draft_1", "")
        self.assertEqual(empty["inline_keyboard"][0][0]["text"], "Approve")
