"""Deterministic IV/Greeks tests; no QMT process or market feed required."""

import ast
import datetime
import io
import math
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.option_analytics import (
    black_scholes_price,
    calculate_option_analytics,
    implied_volatility,
    normalize_option_type,
    option_greeks,
)
from bigqmt_signal_trader.xtquant_compat import BigQmtXtData


class BlackScholesMathTest(unittest.TestCase):
    def test_option_type_normalization_covers_qmt_detail_values(self):
        for raw in ("C", "call", "CALL", "认购", "购"):
            self.assertEqual(normalize_option_type(raw), "C")
        for raw in ("P", "put", "PUT", "认沽", "沽"):
            self.assertEqual(normalize_option_type(raw), "P")
        with self.assertRaises(ValueError):
            normalize_option_type("future")

    def test_put_call_parity_with_dividend(self):
        spot, strike, years, rate, dividend, sigma = 100.0, 103.0, 0.75, 0.03, 0.01, 0.24
        call = black_scholes_price("C", spot, strike, years, rate, sigma, dividend)
        put = black_scholes_price("P", spot, strike, years, rate, sigma, dividend)
        parity = spot * math.exp(-dividend * years) - strike * math.exp(-rate * years)
        self.assertAlmostEqual(call - put, parity, places=11)

    def test_iv_round_trip_is_stable_for_call_and_put(self):
        for kind in ("C", "P"):
            for sigma in (0.08, 0.25, 0.80):
                price = black_scholes_price(
                    kind, 3.055, 3.0, 22.0 / 365.0, 0.016883, sigma, 0.0
                )
                solved = implied_volatility(
                    kind, 3.055, 3.0, price, 22.0 / 365.0, 0.016883, 0.0
                )
                self.assertAlmostEqual(solved, sigma, places=7)

    def test_iv_rejects_a_price_outside_no_arbitrage_bounds(self):
        with self.assertRaisesRegex(ValueError, "no-arbitrage bounds"):
            implied_volatility("C", 3.0, 2.0, 0.2, 30.0 / 365.0, 0.0)

    def test_greek_units_are_explicit_and_consistent(self):
        greeks = option_greeks("CALL", 100, 100, 1.0, 0.05, 0.2, 0.02)
        self.assertGreater(greeks["delta"], 0)
        self.assertGreater(greeks["gamma"], 0)
        self.assertAlmostEqual(greeks["vega_1pct"], greeks["vega"] * 0.01)
        self.assertAlmostEqual(greeks["rho_1pct"], greeks["rho"] * 0.01)
        self.assertAlmostEqual(greeks["theta_per_day"], greeks["theta_per_year"] / 365.0)

    def test_combined_analytics_reprices_the_observed_option(self):
        observed = black_scholes_price("P", 100, 102, 45.0 / 365.0, 0.02, 0.31)
        result = calculate_option_analytics("PUT", 100, 102, observed, 45, 0.02)
        self.assertAlmostEqual(result["implied_volatility"], 0.31, places=7)
        self.assertAlmostEqual(result["theoretical_price"], observed, places=9)
        self.assertEqual(result["option_type"], "P")
        self.assertLess(result["delta"], 0)


class _UnusedClient(object):
    local_cache_config = {"enabled": False}


class _FormulaOptionClient(object):
    local_cache_config = {"enabled": False}

    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method != "get_instrument_detail":
            raise AssertionError("unexpected RPC fallback: %s" % method)
        return {
            "ExpireDate": 20260923,
            "ExtendInfo": {
                "OptExercisePrice": 3.0,
                "OptUndlCode": "510050",
                "OptUndlMarket": "SH",
                "OptUndlRiskFreeRate": 0.016883,
                "optType": "CALL",
            },
        }


class _FakeOptionData(BigQmtXtData):
    def __init__(self):
        super(_FakeOptionData, self).__init__(_UnusedClient())
        self.as_of = datetime.datetime(2026, 9, 1, 15, 0, 0)
        self.underlying_price = 3.055
        self.call_price = black_scholes_price(
            "C", self.underlying_price, 3.0, 22.0 / 365.0, 0.016883, 0.25
        )
        self.put_price = black_scholes_price(
            "P", self.underlying_price, 3.1, 22.0 / 365.0, 0.016883, 0.30
        )
        self.details = {
            "CALL.SHO": {
                "ExpireDate": 20260923,
                "OptExercisePrice": 3.0,
                "OptUndlCode": "510050",
                "OptUndlMarket": "SH",
                "OptUndlRiskFreeRate": 0.016883,
                "optType": "CALL",
            },
            "PUT.SHO": {
                "ExpireDate": 20260923,
                "OptExercisePrice": 3.1,
                "OptUndlCode": "510050",
                "OptUndlMarket": "SH",
                "OptUndlRiskFreeRate": 0.016883,
                "optType": "PUT",
            },
        }
        self.prices = {
            "510050.SH": {"close": [self.underlying_price]},
            "CALL.SHO": {"close": [self.call_price]},
            "PUT.SHO": {"close": [self.put_price]},
        }
        self.tick_prices = {}
        self.daily_prices = {}
        self.last_market_request = None
        self.market_requests = []

    def get_option_detail_data(self, stockcode):
        return self.details[stockcode]

    def get_option_list(self, undl_code, dedate, opttype="", isavailavle=False):
        return ["CALL.SHO", "PUT.SHO"]

    def get_market_data_ex(self, **kwargs):
        self.last_market_request = kwargs
        self.market_requests.append(kwargs)
        if kwargs["period"] == "tick":
            source = self.tick_prices
        elif kwargs["period"] == "1d":
            source = self.daily_prices or self.prices
        else:
            source = self.prices
        return {code: source.get(code) for code in kwargs["stock_list"]}


class ContractAnalyticsTest(unittest.TestCase):
    def test_formula_instrument_metadata_avoids_per_contract_option_rpc(self):
        client = _FormulaOptionClient()
        data = BigQmtXtData(client)
        as_of = datetime.datetime(2026, 9, 1, 15, 0, 0)
        option_price = black_scholes_price(
            "C", 3.055, 3.0, 22.0 / 365.0, 0.016883, 0.25
        )

        result = data.get_option_analytics(
            "CALL.SHO",
            option_price=option_price,
            underlying_price=3.055,
            as_of=as_of,
        )

        self.assertAlmostEqual(result["implied_volatility"], 0.25, places=7)
        self.assertEqual(
            client.calls,
            [("get_instrument_detail", {"code": "CALL.SHO"})],
        )

    def test_one_contract_uses_one_fast_close_request_and_detail_defaults(self):
        data = _FakeOptionData()
        result = data.get_option_analytics("CALL.SHO", as_of=data.as_of)

        self.assertEqual(data.last_market_request["field_list"], ["close"])
        self.assertEqual(
            data.last_market_request["stock_list"], ["CALL.SHO", "510050.SH"]
        )
        self.assertFalse(data.last_market_request["fill_data"])
        self.assertAlmostEqual(result["implied_volatility"], 0.25, places=7)
        self.assertEqual(result["underlying_code"], "510050.SH")
        self.assertEqual(result["expiry_date"], "20260923")
        self.assertAlmostEqual(result["days_to_expiry"], 22.0)
        self.assertEqual(result["option_price_source"], "1m_close")

    def test_explicit_prices_skip_market_data(self):
        data = _FakeOptionData()
        result = data.get_option_analytics(
            "PUT.SHO",
            option_price=data.put_price,
            underlying_price=data.underlying_price,
            as_of=data.as_of,
        )

        self.assertIsNone(data.last_market_request)
        self.assertAlmostEqual(result["implied_volatility"], 0.30, places=7)
        self.assertEqual(result["option_price_source"], "argument")
        self.assertLess(result["delta"], 0)

    def test_chain_keeps_bad_contracts_as_errors(self):
        data = _FakeOptionData()
        data.prices["PUT.SHO"] = {"close": [10.0]}
        result = data.get_option_chain_analytics(
            "510050.SH", "202609", as_of=data.as_of
        )

        self.assertEqual(result["count"], 2)
        self.assertEqual(result["valid_count"], 1)
        self.assertEqual(result["error_count"], 1)
        self.assertNotIn("analytics_error", result["contracts"][0])
        self.assertIn("no-arbitrage bounds", result["contracts"][1]["analytics_error"])

    def test_missing_intraday_close_falls_back_to_tick_last_in_one_batch(self):
        data = _FakeOptionData()
        data.prices["CALL.SHO"] = {"close": []}
        data.prices["PUT.SHO"] = {"close": []}
        data.tick_prices = {
            "CALL.SHO": {"lastPrice": [data.call_price]},
            "PUT.SHO": {"lastPrice": [data.put_price]},
        }

        result = data.get_option_chain_analytics(
            "510050.SH", "202609", as_of=data.as_of
        )

        self.assertEqual(result["valid_count"], 2)
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(
            [item["option_price_source"] for item in result["contracts"]],
            ["tick_last", "tick_last"],
        )
        self.assertEqual(
            [request["period"] for request in data.market_requests],
            ["1m", "tick"],
        )

    def test_tick_midpoint_then_daily_close_are_labelled_fallbacks(self):
        data = _FakeOptionData()
        data.prices["CALL.SHO"] = {"close": []}
        data.tick_prices["CALL.SHO"] = {
            "lastPrice": [0.0],
            "bidPrice": [[data.call_price - 0.001]],
            "askPrice": [[data.call_price + 0.001]],
        }
        midpoint = data.get_option_analytics("CALL.SHO", as_of=data.as_of)
        self.assertEqual(midpoint["option_price_source"], "tick_mid")
        self.assertAlmostEqual(midpoint["option_price"], data.call_price)

        data = _FakeOptionData()
        data.prices["CALL.SHO"] = {"close": []}
        data.daily_prices["CALL.SHO"] = {"close": [data.call_price]}
        fallback = data.get_option_analytics("CALL.SHO", as_of=data.as_of)
        self.assertEqual(fallback["option_price_source"], "1d_close")
        self.assertEqual(
            [request["period"] for request in data.market_requests],
            ["1m", "tick", "1d"],
        )

    def test_chain_infers_dividend_yield_from_put_call_parity(self):
        data = _FakeOptionData()
        expected_yield = 0.072
        years = 22.0 / 365.0
        # Make the pair share one strike/expiry so parity can infer carry.
        data.details["PUT.SHO"]["OptExercisePrice"] = 3.0
        data.call_price = black_scholes_price(
            "C", data.underlying_price, 3.0, years, 0.016883, 0.25,
            expected_yield,
        )
        # Put-call parity requires prices generated with the same volatility.
        data.put_price = black_scholes_price(
            "P", data.underlying_price, 3.0, years, 0.016883, 0.25,
            expected_yield,
        )
        data.prices["CALL.SHO"] = {"close": [data.call_price]}
        data.prices["PUT.SHO"] = {"close": [data.put_price]}

        result = data.get_option_chain_analytics(
            "510050.SH", "202609", as_of=data.as_of
        )

        self.assertEqual(result["dividend_yield_source"], "put_call_parity_median")
        self.assertEqual(result["dividend_yield_pair_count"], 1)
        self.assertAlmostEqual(result["dividend_yield"], expected_yield, places=8)
        self.assertEqual(result["valid_count"], 2)

    def test_boundary_failure_retries_with_same_strike_parity_yield(self):
        data = _FakeOptionData()
        years = 22.0 / 365.0
        rate = 0.016883
        specs = {
            "C28.SHO": ("CALL", 2.8, 0.10),
            "P28.SHO": ("PUT", 2.8, 0.10),
            "C33.SHO": ("CALL", 3.3, 0.00),
            "P33.SHO": ("PUT", 3.3, 0.00),
        }
        data.details = {}
        data.prices = {"510050.SH": {"close": [data.underlying_price]}}
        pair_prices = {}
        for strike, dividend in ((2.8, 0.10), (3.3, 0.00)):
            parity = (
                data.underlying_price * math.exp(-dividend * years)
                - strike * math.exp(-rate * years)
            )
            pair_prices[("CALL", strike)] = max(parity, 0.0) + 0.0005
            pair_prices[("PUT", strike)] = max(-parity, 0.0) + 0.0005
        for code, (kind, strike, dividend) in specs.items():
            data.details[code] = {
                "ExpireDate": 20260923,
                "OptExercisePrice": strike,
                "OptUndlCode": "510050",
                "OptUndlMarket": "SH",
                "OptUndlRiskFreeRate": rate,
                "optType": kind,
            }
            data.prices[code] = {"close": [pair_prices[(kind, strike)]]}
        data.get_option_list = lambda *args, **kwargs: list(specs)

        result = data.get_option_chain_analytics(
            "510050.SH", "202609", as_of=data.as_of
        )

        self.assertEqual(result["valid_count"], 4)
        self.assertEqual(result["error_count"], 0)
        self.assertTrue(any(
            item["dividend_yield_source"] == "put_call_parity_strike"
            for item in result["contracts"]
        ))


class ClientOnlyImportBoundaryTest(unittest.TestCase):
    def test_qmt_server_modules_do_not_import_option_analytics(self):
        """Keep local models out of the QMT strategy/runtime import graph."""
        src = os.path.join(ROOT, "src")
        client_only = {
            os.path.join("bigqmt_signal_trader", "__init__.py"),
            os.path.join("bigqmt_signal_trader", "xtquant_compat.py"),
            os.path.join("bigqmt_signal_trader", "option_analytics.py"),
            os.path.join("bigqmt_signal_trader", "option_analytics_client.py"),
        }
        offenders = []
        for directory, _subdirs, filenames in os.walk(src):
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(directory, filename)
                relative = os.path.relpath(path, src)
                if relative in client_only or "xtquant" in relative.split(os.sep):
                    continue
                with io.open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read())
                for node in ast.walk(tree):
                    module = ""
                    if isinstance(node, ast.ImportFrom):
                        module = node.module or ""
                    elif isinstance(node, ast.Import):
                        module = ",".join(alias.name for alias in node.names)
                    if "option_analytics" in module:
                        offenders.append(relative)

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
