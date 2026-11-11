import unittest
from unittest.mock import patch

from other_leagues_ledger import (
    ConfigurationError,
    LeagueConfig,
    _parse_league,
    process_league,
)


class FakeDiscord:
    def __init__(self):
        self.posts = []

    def post_discord(self, webhook, content):
        self.posts.append((webhook, content))


class TribunalCutoverTests(unittest.TestCase):
    def setUp(self):
        self.transactions = [
            {
                "type": "trade",
                "status": "complete",
                "transaction_id": "trade-1",
                "status_updated": 1800000000000,
                "roster_ids": [1, 2],
                "adds": {"a": 1, "b": 2},
            },
            {
                "type": "waiver",
                "status": "complete",
                "transaction_id": "waiver-1",
                "status_updated": 1800000000001,
                "adds": {"c": 1},
            },
        ]
        self.state = {"leagues": {"free_ironbound_sixteen": {
            "last_seen_ms": 1799999999999, "seen_at_last_ms": []
        }}}
        self.league = LeagueConfig(
            key="free_ironbound_sixteen",
            name="Free Ironbound Sixteen",
            sleeper_league_id="1339055357728686080",
            webhook_waivers="https://example.test/waivers",
            webhook_trades="https://example.test/trades",
            trade_receipts_enabled=False,
        )

    @patch("other_leagues_ledger.roster_name_map")
    @patch("other_leagues_ledger.fetch_league_transactions")
    def test_cutover_preserves_waivers_and_advances_trade_checkpoint(self, fetch, names):
        names.return_value = ({1: "Alpha", 2: "Bravo"}, {})
        fetch.return_value = self.transactions
        client = FakeDiscord()
        result = process_league(self.league, self.state, {"a": "A", "b": "B", "c": "C"},
                                client, (12,))
        self.assertEqual(result.transactions, 2)
        self.assertEqual(result.messages, 1)
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(client.posts[0][0], self.league.webhook_waivers)
        self.assertIn("Player Transaction", client.posts[0][1])
        self.assertEqual(self.state["leagues"][self.league.key]["last_seen_ms"],
                         1800000000001)
        self.assertEqual(len(self.state["leagues"][self.league.key]["seen_at_last_ms"]), 1)

    @patch("other_leagues_ledger.roster_name_map")
    @patch("other_leagues_ledger.fetch_league_transactions")
    def test_other_leagues_keep_trade_receipts(self, fetch, names):
        names.return_value = ({1: "Alpha", 2: "Bravo"}, {})
        fetch.return_value = self.transactions
        from dataclasses import replace
        enabled_league = replace(self.league, trade_receipts_enabled=True)
        client = FakeDiscord()
        result = process_league(enabled_league, self.state, {"a": "A", "b": "B", "c": "C"},
                                client, (12,))
        self.assertEqual(result.messages, 2)
        self.assertEqual({webhook for webhook, _ in client.posts},
                         {self.league.webhook_waivers, self.league.webhook_trades})

    def test_cutover_config_requires_boolean_not_string(self):
        raw = {
            "key": "free_ironbound_sixteen", "name": "Free",
            "sleeper_league_id": "1339055357728686080",
            "webhooks": {"waivers": "waivers", "trades": "trades"},
            "trade_receipts_enabled": "false",
        }
        with self.assertRaisesRegex(ConfigurationError, "trade_receipts_enabled"):
            _parse_league(raw, 0, {})
        raw["trade_receipts_enabled"] = False
        self.assertFalse(_parse_league(raw, 0, {}).trade_receipts_enabled)
        raw.pop("trade_receipts_enabled")
        self.assertTrue(_parse_league(raw, 0, {}).trade_receipts_enabled)


if __name__ == "__main__":
    unittest.main()
