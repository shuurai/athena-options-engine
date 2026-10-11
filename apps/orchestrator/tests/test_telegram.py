"""Telegram quote commands. No gateway and no live chat."""

import unittest
from decimal import Decimal
from unittest.mock import patch

from datetime import UTC, datetime

from athena_bots.telegram import format_headlines, help_text, quote_text
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
        for name in ("/stock", "/etf", "/hk", "/fx", "/approve", "/reject", "/drafts", "/halt", "/news"):
            self.assertIn(name, text)
        self.assertIn("lists the stored headlines", text)

    def test_a_symbol_lists_the_stored_headline(self) -> None:
        when = datetime(2026, 10, 11, 12, 4, tzinfo=UTC)
        text = format_headlines("AAPL", [(when, "yahoo", "Apple raises guidance")])
        self.assertIn("AAPL  1 stored", text)
        self.assertIn("yahoo", text)
        self.assertIn("Apple raises guidance", text)
        self.assertEqual(format_headlines("AAPL", []), "No stored headline for AAPL.")
