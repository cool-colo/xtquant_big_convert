# coding: utf-8
"""Tests for multi_account.py: multi-account RPC service manager.

Covers:
  1. SecondaryHandlersProxy injects account_id into params
  2. MultiAccountRpcServiceManager delegates start/stop/drain
  3. build_multi_account_rpc_service with 0/1/2+ entries in MAP
  4. cancel(order_ref, account_id=...) per-request routing
  5. Single-account fallback (MAP empty or single entry)
"""

import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from bigqmt_signal_trader.account_type_map import reload as reload_map
from bigqmt_signal_trader.multi_account import (
    SecondaryHandlersProxy,
    MultiAccountRpcServiceManager,
    build_multi_account_rpc_service,
)


# ---------------------------------------------------------------------------
# SecondaryHandlersProxy
# ---------------------------------------------------------------------------

class SecondaryHandlersProxyTest(unittest.TestCase):

    def test_injects_account_id_when_missing(self):
        handlers = types.SimpleNamespace(handle=mock.MagicMock(return_value="ok"))
        proxy = SecondaryHandlersProxy(handlers, "FUTURE_ACCT")
        result = proxy.handle("ping", {"foo": "bar"})
        handlers.handle.assert_called_once_with("ping", {"foo": "bar", "account_id": "FUTURE_ACCT"})
        self.assertEqual(result, "ok")

    def test_does_not_override_existing_account_id(self):
        handlers = types.SimpleNamespace(handle=mock.MagicMock(return_value="ok"))
        proxy = SecondaryHandlersProxy(handlers, "FUTURE_ACCT")
        proxy.handle("ping", {"account_id": "STOCK_ACCT"})
        handlers.handle.assert_called_once_with("ping", {"account_id": "STOCK_ACCT"})

    def test_injects_on_empty_params(self):
        handlers = types.SimpleNamespace(handle=mock.MagicMock(return_value="ok"))
        proxy = SecondaryHandlersProxy(handlers, "FUTURE_ACCT")
        proxy.handle("ping")
        handlers.handle.assert_called_once_with("ping", {"account_id": "FUTURE_ACCT"})

    def test_delegates_attribute_access(self):
        handlers = types.SimpleNamespace(account_id="PRIMARY", order_gateway=None)
        proxy = SecondaryHandlersProxy(handlers, "SECONDARY")
        self.assertEqual(proxy.account_id, "PRIMARY")
        self.assertIsNone(proxy.order_gateway)

    def test_delegates_setattr(self):
        handlers = types.SimpleNamespace(x=1)
        proxy = SecondaryHandlersProxy(handlers, "SEC")
        proxy.x = 42
        self.assertEqual(handlers.x, 42)


# ---------------------------------------------------------------------------
# MultiAccountRpcServiceManager
# ---------------------------------------------------------------------------

class MultiAccountRpcServiceManagerTest(unittest.TestCase):

    def _service(self, account_id="ACCT1"):
        svc = mock.MagicMock()
        svc.account_id = account_id
        svc.redis = "redis_obj"
        svc.listen_redis = "listen_redis_obj"
        svc.drain_request_queue.return_value = 5
        svc.drain_pending.return_value = 3
        return svc

    def test_delegates_start(self):
        s1, s2 = self._service(), self._service("ACCT2")
        mgr = MultiAccountRpcServiceManager([s1, s2], handlers="h")
        mgr.start()
        s1.start.assert_called_once()
        s2.start.assert_called_once()

    def test_delegates_stop(self):
        s1, s2 = self._service(), self._service("ACCT2")
        mgr = MultiAccountRpcServiceManager([s1, s2], handlers="h")
        mgr.stop()
        s1.stop.assert_called_once()
        s2.stop.assert_called_once()

    def test_drain_sums(self):
        s1, s2 = self._service(), self._service("ACCT2")
        mgr = MultiAccountRpcServiceManager([s1, s2], handlers="h")
        self.assertEqual(mgr.drain_request_queue(max_items=20), 10)
        self.assertEqual(mgr.drain_pending(max_items=20), 6)

    def test_primary_attrs_exposed(self):
        s1 = self._service()
        mgr = MultiAccountRpcServiceManager([s1], handlers="h")
        self.assertEqual(mgr.account_id, "ACCT1")
        self.assertEqual(mgr.redis, "redis_obj")
        self.assertEqual(mgr.listen_redis, "listen_redis_obj")

    def test_getattr_delegates_to_primary(self):
        s1 = self._service()
        s1.some_attr = "value"
        mgr = MultiAccountRpcServiceManager([s1], handlers="h")
        self.assertEqual(mgr.some_attr, "value")


# ---------------------------------------------------------------------------
# build_multi_account_rpc_service
# ---------------------------------------------------------------------------

class BuildMultiAccountServiceTest(unittest.TestCase):

    def setUp(self):
        reload_map()

    def test_empty_map_falls_back_to_single(self):
        """No MAP → build_single_fn called, result returned as-is."""
        sys.modules.pop("bigqmt_signal_trader_local_config", None)
        reload_map()
        single = mock.MagicMock()
        result = build_multi_account_rpc_service(None, None, {}, single)
        single.assert_called_once()
        self.assertEqual(result, single.return_value)

    def test_single_entry_map_falls_back_to_single(self):
        """MAP with 1 entry → still single service."""
        fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"ACCT1": "STOCK"}
        with mock.patch.dict("sys.modules",
                             {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()
            single = mock.MagicMock()
            result = build_multi_account_rpc_service(None, None, {}, single)
            single.assert_called_once()
            self.assertEqual(result, single.return_value)

    def test_two_entries_builds_secondary(self):
        """MAP with 2 entries → MultiAccountRpcServiceManager with 2 services."""
        fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"ACCT1": "STOCK", "ACCT2": "FUTURE"}
        with mock.patch.dict("sys.modules",
                             {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()

            primary = mock.MagicMock()
            primary.account_id = "ACCT1"
            primary.redis = "redis"
            primary.listen_redis = "listen_redis"

            single = mock.MagicMock(return_value=primary)

            # Mock _build_secondary to avoid needing real Redis
            with mock.patch(
                "bigqmt_signal_trader.multi_account._build_secondary"
            ) as mock_secondary:
                secondary = mock.MagicMock()
                secondary.account_id = "ACCT2"
                mock_secondary.return_value = secondary

                result = build_multi_account_rpc_service(
                    None, None, {}, single)
                self.assertIsInstance(result, MultiAccountRpcServiceManager)
                self.assertEqual(len(result._services), 2)
                mock_secondary.assert_called_once()

    def test_build_single_returns_none_propagates(self):
        """build_single_fn returns None → None."""
        sys.modules.pop("bigqmt_signal_trader_local_config", None)
        reload_map()
        result = build_multi_account_rpc_service(
            None, None, {}, lambda *a, **kw: None)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# cancel(account_id) per-request routing
# ---------------------------------------------------------------------------

class CancelPerRequestTest(unittest.TestCase):
    """cancel(order_ref, account_id=...) routes via _resolve_account_type."""

    def test_cancel_with_account_id(self):
        from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway

        fake_cfg = types.ModuleType("bigqmt_signal_trader_local_config")
        fake_cfg.BIGQMT_ACCOUNT_TYPE_MAP = {"FUTURE_ACCT": "FUTURE"}
        with mock.patch.dict("sys.modules",
                             {"bigqmt_signal_trader_local_config": fake_cfg}):
            reload_map()
            gw = BigQmtOrderGateway(context_info=None, account_type="STOCK")

            # Mock cancel_func to capture args
            captured = {}
            def fake_cancel(order_sys_id, account_id, account_type, context_info):
                captured["account_id"] = account_id
                captured["account_type"] = account_type
                return True

            gw.cancel_func = fake_cancel
            from bigqmt_signal_trader.models import OrderRef
            result = gw.cancel(OrderRef(order_sys_id="SYS1", user_order_id="U1"),
                               account_id="FUTURE_ACCT")
            self.assertTrue(result.success)
            self.assertEqual(captured["account_id"], "FUTURE_ACCT")
            self.assertEqual(captured["account_type"], "FUTURE")

    def test_cancel_without_account_id_uses_default(self):
        from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway

        sys.modules.pop("bigqmt_signal_trader_local_config", None)
        reload_map()
        gw = BigQmtOrderGateway(context_info=None, account_type="STOCK")

        captured = {}
        def fake_cancel(order_sys_id, account_id, account_type, context_info):
            captured["account_id"] = account_id
            captured["account_type"] = account_type
            return True

        gw.cancel_func = fake_cancel
        gw.account_id = "DEFAULT_ACCT"
        from bigqmt_signal_trader.models import OrderRef
        result = gw.cancel(OrderRef(order_sys_id="SYS1", user_order_id="U1"))
        self.assertTrue(result.success)
        self.assertEqual(captured["account_id"], "DEFAULT_ACCT")
        self.assertEqual(captured["account_type"], "STOCK")


# ---------------------------------------------------------------------------
# Integration: _handle_cancel_order passes account_id
# ---------------------------------------------------------------------------

class CancelHandlerIntegrationTest(unittest.TestCase):
    """_handle_cancel_order passes account_id to gateway.cancel()."""

    def test_cancel_handler_passes_account_id(self):
        from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers
        from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway

        # Patch cancel to capture account_id
        captured = {}
        original_cancel = DryRunOrderGateway.cancel
        def capturing_cancel(self, order_ref, account_id=None):
            captured["account_id"] = account_id
            return original_cancel(self, order_ref, account_id=account_id)

        with mock.patch.object(DryRunOrderGateway, "cancel", capturing_cancel):
            gw = DryRunOrderGateway()
            handlers = BigQmtRpcHandlers(
                account_id="PRIMARY_ACCT",
                market_data=None,
                position_provider=None,
                order_gateway=gw,
                allow_order_methods=True,
            )
            handlers._handle_cancel_order({
                "account_id": "FUTURE_ACCT",
                "order_sys_id": "SYS123",
            })
            self.assertEqual(captured["account_id"], "FUTURE_ACCT")


if __name__ == "__main__":
    unittest.main()
