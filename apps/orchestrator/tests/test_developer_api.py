"""Each developer price call has its own URL. These tests never open IB."""

import json
import unittest
from unittest.mock import patch

from athena_orchestrator.developer import _complete, _one_bar, _one_company, handle
from athena_orchestrator.pages import _skill_rest


def _body(raw: str) -> dict:
    return json.loads(raw)


class _IdleThread:
    started: list["_IdleThread"] = []

    def __init__(self, target=None, args=(), name="", daemon=None):
        self.target = target
        self.args = args
        self.name = name
        _IdleThread.started.append(self)

    def start(self) -> None:
        return None


class DeveloperApiTest(unittest.TestCase):
    def setUp(self) -> None:
        _IdleThread.started.clear()
        self.auth = patch("athena_orchestrator.developer.authorized", return_value=True)
        self.thread = patch("athena_orchestrator.developer.threading.Thread", _IdleThread)
        self.auth.start()
        self.thread.start()

    def tearDown(self) -> None:
        self.thread.stop()
        self.auth.stop()
        from athena_orchestrator import developer

        with developer._LOCK:
            developer._TASKS.clear()

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        code, raw = handle(None, "POST", path, {}, json.dumps(payload).encode(), {})
        return code, _body(raw)

    def test_index_lists_each_call(self) -> None:
        code, raw = handle(None, "GET", "/api/v1", {}, b"", {})
        self.assertEqual(code, 200)
        paths = [(item["method"], item["path"]) for item in _body(raw)["endpoints"]]
        self.assertEqual(
            paths,
            [
                ("POST", "/api/v1/bars"),
                ("POST", "/api/v1/day"),
                ("POST", "/api/v1/option"),
                ("POST", "/api/v1/company"),
                ("GET", "/api/v1/tasks/{task_id}"),
            ],
        )

    def test_a_bad_key_is_rejected(self) -> None:
        with patch("athena_orchestrator.developer.authorized", return_value=False):
            code, raw = handle(None, "POST", "/api/v1/bars", {}, b"{}", {})
        self.assertEqual(code, 401)
        self.assertEqual(_body(raw)["status"], "failed")

    def test_bars_is_a_window_of_stocks(self) -> None:
        code, body = self.post("/api/v1/bars", {"symbols": ["NVDA", "700"], "bar": "1 day", "duration": "5 D"})
        self.assertEqual(code, 202)
        self.assertEqual(body["status"], "running")
        specs = _IdleThread.started[-1].args[1]
        self.assertEqual([item["sec_type"] for item in specs], ["STK", "STK"])
        self.assertEqual(_IdleThread.started[-1].args[5], "")

    def test_bars_refuses_a_single_day(self) -> None:
        code, body = self.post("/api/v1/bars", {"symbols": ["NVDA"], "day": "2026-10-09"})
        self.assertEqual(code, 400)
        self.assertIn("/api/v1/day", body["error"])
        self.assertEqual(_IdleThread.started, [])

    def test_bars_refuses_an_option(self) -> None:
        code, body = self.post(
            "/api/v1/bars",
            {"symbols": [{"symbol": "NVDA", "sec_type": "OPT", "expiry": "20261120", "strike": 180, "right": "P"}]},
        )
        self.assertEqual(code, 400)
        self.assertIn("/api/v1/option", body["error"])

    def test_day_requires_a_session(self) -> None:
        code, body = self.post("/api/v1/day", {"symbols": ["NVDA"]})
        self.assertEqual(code, 400)
        self.assertIn("day is required", body["error"])

    def test_day_asks_for_that_session(self) -> None:
        code, body = self.post("/api/v1/day", {"symbols": ["NVDA"], "day": "2026-10-09"})
        self.assertEqual(code, 202)
        self.assertEqual(body["status"], "running")
        _task_id, specs, duration, _bar, end, day = _IdleThread.started[-1].args
        self.assertEqual(specs[0]["symbol"], "NVDA")
        self.assertEqual(duration, "10 D")
        self.assertEqual(end, "20261009 23:59:59")
        self.assertEqual(day, "2026-10-09")

    def test_option_requires_a_contract(self) -> None:
        code, body = self.post("/api/v1/option", {"symbols": ["NVDA"]})
        self.assertEqual(code, 400)
        self.assertIn("option objects", body["error"])

    def test_option_accepts_a_contract(self) -> None:
        code, body = self.post(
            "/api/v1/option",
            {"symbols": [{"symbol": "NVDA", "expiry": "20261120", "strike": 180, "right": "P"}], "duration": "5 D"},
        )
        self.assertEqual(code, 202)
        spec = _IdleThread.started[-1].args[1][0]
        self.assertEqual(spec["sec_type"], "OPT")
        self.assertEqual(spec["expiry"], "20261120")
        self.assertEqual(spec["strike"], 180)
        self.assertEqual(spec["right"], "P")
        self.assertTrue(body["task_id"])

    def test_company_is_its_own_call(self) -> None:
        code, body = self.post("/api/v1/company", {"symbols": ["NVDA", "700"]})
        self.assertEqual(code, 202)
        self.assertEqual(_IdleThread.started[-1].name, "athena-dev-company")
        self.assertEqual([item["symbol"] for item in _IdleThread.started[-1].args[2]], ["NVDA", "700"])
        self.assertTrue(body["task_id"])

    def test_unknown_task_is_failed(self) -> None:
        code, raw = handle(None, "GET", "/api/v1/tasks/missing", {}, b"", {})
        self.assertEqual(code, 404)
        self.assertEqual(_body(raw)["status"], "failed")

    def test_poll_returns_the_finished_price(self) -> None:
        code, started = self.post("/api/v1/bars", {"symbols": ["NVDA"], "duration": "5 D"})
        task_id = started["task_id"]
        running, raw = handle(None, "GET", f"/api/v1/tasks/{task_id}", {}, b"", {})
        self.assertEqual(running, 200)
        self.assertEqual(_body(raw)["status"], "running")
        _complete(task_id, "done", {"symbols": [{"symbol": "NVDA", "latest": 181.0, "bars": []}]}, "")
        done, raw = handle(None, "GET", f"/api/v1/tasks/{task_id}", {}, b"", {})
        body = _body(raw)
        self.assertEqual(done, 200)
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["result"]["symbols"][0]["latest"], 181.0)
        self.assertEqual(code, 202)

    def test_latest_is_the_newest_close(self) -> None:
        class Broker:
            def historical(self, contract, duration, bar, end):
                return [
                    {"date": "2026-10-08", "time": "2026-10-08", "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1},
                    {"date": "2026-10-09", "time": "2026-10-09", "open": 1, "high": 3, "low": 1, "close": 2.5, "volume": 2},
                ]

        spec = {"symbol": "NVDA", "sec_type": "STK", "expiry": "", "strike": None, "right": ""}
        row = _one_bar(Broker(), spec, "5 D", "1 day", "", "")
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["latest"], 2.5)
        one = _one_bar(Broker(), spec, "10 D", "1 day", "20261008 23:59:59", "2026-10-08")
        self.assertEqual(one["latest"], 1.5)
        self.assertEqual([item["date"] for item in one["bars"]], ["2026-10-08"])

    def test_a_missing_session_fails_that_symbol(self) -> None:
        class Broker:
            def historical(self, contract, duration, bar, end):
                return [{"date": "2026-10-08", "time": "2026-10-08", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]

        spec = {"symbol": "NVDA", "sec_type": "STK", "expiry": "", "strike": None, "right": ""}
        row = _one_bar(Broker(), spec, "10 D", "1 day", "20261009 23:59:59", "2026-10-09")
        self.assertEqual(row["status"], "failed")
        self.assertNotIn("latest", row)

    def test_option_day_uses_the_last_hourly_close(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.bar = ""

            def historical(self, contract, duration, bar, end):
                self.bar = bar
                return [
                    {"date": "2026-10-09", "time": "2026-10-09T13:00:00", "open": 0.4, "high": 0.6, "low": 0.4, "close": 0.5, "volume": 3},
                    {"date": "2026-10-09", "time": "2026-10-09T14:00:00", "open": 0.5, "high": 0.8, "low": 0.4, "close": 0.68, "volume": 4},
                ]

        broker = Broker()
        spec = {"symbol": "NVDA", "sec_type": "OPT", "expiry": "20261120", "strike": 180.0, "right": "P"}
        row = _one_bar(broker, spec, "5 D", "1 day", "", "")
        self.assertEqual(broker.bar, "1 hour")
        self.assertEqual(len(row["bars"]), 1)
        self.assertEqual(row["latest"], 0.68)

    def test_company_result_has_no_bars(self) -> None:
        class Broker:
            def describe(self, contract):
                return {
                    "name": "NVIDIA Corp",
                    "exchange": "SMART",
                    "primary_exchange": "NASDAQ",
                    "currency": "USD",
                    "con_id": 1,
                    "trading_class": "NVDA",
                }

        spec = {"symbol": "NVDA", "sec_type": "STK", "expiry": "", "strike": None, "right": ""}
        row = _one_company(Broker(), spec, ("NVIDIA Corp", 5_530_000_000_000))
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["market_cap"], 5_530_000_000_000)
        self.assertNotIn("bars", row)

    def test_skill_page_shows_each_url(self) -> None:
        html = _skill_rest()
        for path in ("/api/v1/bars", "/api/v1/day", "/api/v1/option", "/api/v1/company"):
            self.assertIn(f'<span class="tag amber">POST</span><span class="mono">http://127.0.0.1:8788{path}</span>', html)
        self.assertIn(
            '<span class="tag green">GET</span><span class="mono">http://127.0.0.1:8788/api/v1/tasks/{task_id}</span>',
            html,
        )
        self.assertIn('class="jk"', html)
        self.assertNotIn("POST /api/v1/bars", html)

    def test_skill_try_has_curl_and_playground(self) -> None:
        from athena_orchestrator.pages import _skill_try

        html = _skill_try()
        self.assertIn("Curl Sample", html)
        self.assertIn("Playground", html)
        self.assertIn("skill-play-kind", html)
        self.assertIn("skill-play-send", html)
        self.assertIn('id="skill-play-url" readonly', html)
        self.assertIn("/api/v1/option", html)
        self.assertIn("data-skill-panel=\"play\" hidden", html)
        blob = html.split('id="skill-play-samples">', 1)[1].split("</script>", 1)[0]
        samples = json.loads(blob)
        self.assertEqual(samples["bars"]["path"], "/api/v1/bars")
        self.assertNotIn("&quot;", blob)

    def test_too_many_symbols(self) -> None:
        code, body = self.post("/api/v1/bars", {"symbols": [f"S{index}" for index in range(21)]})
        self.assertEqual(code, 400)
        self.assertIn("limited to 20", body["error"])
        self.assertEqual(_IdleThread.started, [])

    def test_bad_duration(self) -> None:
        code, body = self.post("/api/v1/bars", {"symbols": ["NVDA"], "duration": "forever"})
        self.assertEqual(code, 400)
        self.assertIn("duration", body["error"])

    def test_bad_bar_size(self) -> None:
        code, body = self.post("/api/v1/bars", {"symbols": ["NVDA"], "bar": "2 hours"})
        self.assertEqual(code, 400)
        self.assertIn("bar must be", body["error"])

    def test_unknown_path(self) -> None:
        code, body = self.post("/api/v1/nope", {"symbols": ["NVDA"]})
        self.assertEqual(code, 404)
        self.assertEqual(body["error"], "Unknown API.")
        code, raw = handle(None, "GET", "/api/v1/missing", {}, b"", {})
        self.assertEqual(code, 404)

    def test_body_must_be_json(self) -> None:
        code, raw = handle(None, "POST", "/api/v1/bars", {}, b"not-json", {})
        self.assertEqual(code, 400)
        self.assertIn("JSON", _body(raw)["error"])

    def test_option_needs_a_strike_and_a_right(self) -> None:
        code, body = self.post(
            "/api/v1/option",
            {"symbols": [{"symbol": "NVDA", "expiry": "20261120", "strike": 0, "right": "P"}]},
        )
        self.assertEqual(code, 400)
        self.assertIn("strike", body["error"])
        code, body = self.post(
            "/api/v1/option",
            {"symbols": [{"symbol": "NVDA", "expiry": "20261120", "strike": 180, "right": "X"}]},
        )
        self.assertEqual(code, 400)
        self.assertIn("right", body["error"])

    def test_a_closed_gateway_fails_the_task_without_an_order(self) -> None:
        code, body = self.post("/api/v1/bars", {"symbols": ["NVDA"], "duration": "5 D"})
        self.assertEqual(code, 202)
        thread = _IdleThread.started[-1]
        with patch("athena_orchestrator.developer.IbBroker") as broker_cls:
            broker_cls.return_value.connect.return_value = False
            thread.target(*thread.args)
        code, raw = handle(None, "GET", f"/api/v1/tasks/{body['task_id']}", {}, b"", {})
        self.assertEqual(_body(raw)["status"], "failed")
        self.assertIn("did not connect", _body(raw)["error"])


if __name__ == "__main__":
    unittest.main()
