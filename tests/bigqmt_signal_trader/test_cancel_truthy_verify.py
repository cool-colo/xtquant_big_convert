# coding: utf-8
"""A truthy native cancel return must be verified too (issue #151).

The mirror of #148. Full QMT's injected cancel() return describes "the
request was sent", not "the order was cancelled":

    cancel_order_stock_sysid("bigqmt-probe-149-no-such-order")
        -> {'success': True, 'message': ''}
    cancel_order_stock_sysid("99999999999999")
        -> {'success': True, 'message': ''}

-- measured on an account with ZERO cancelable orders, so neither probe
could have matched anything real.

#149 made the falsey half settle against the order snapshot but left the
truthy half on the fast path. Both directions now verify:

- truthy + order already 53/54        -> one immediate lookup confirms,
  no extra wait (the fast path stays fast)
- truthy + order that does not exist  -> failure ("was not found"),
  never a bare success
- truthy + order already filled (56)  -> failure, native return overridden
- status 51/52 (已报待撤/部成待撤)    -> the exchange has ACCEPTED the
  cancel; keep waiting, and at the deadline report the acceptance, not a
  "still status 51" failure (the narrow-window twin of #148)
"""

import json
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers, RedisPubSubRpcService

from test_redis_rpc import (  # noqa: E402  -- reuse the established fakes
    FakeMarketData,
    FakePositionProvider,
    FakeRedis,
    FalseyCancelGateway,
)


class TruthyCancelVerificationTest(unittest.TestCase):
    """issue #151: cancel() success is not proof the order was cancelled."""

    @staticmethod
    def _service(gateway, timeout=5.0):
        redis_client = FakeRedis()
        handlers = BigQmtRpcHandlers(
            account_id="acct",
            market_data=FakeMarketData(),
            position_provider=FakePositionProvider(),
            order_gateway=gateway,
            allow_order_methods=True,
            order_settle_timeout_seconds=timeout,
        )
        return redis_client, RedisPubSubRpcService(
            redis_client, handlers, account_id="acct")

    @staticmethod
    def _cancel(service, request_id="cancel-request-1"):
        service.enqueue_payload({
            "request_id": request_id,
            "account_id": "acct",
            "method": "cancel_order_stock_sysid",
            "params": {"order_sysid": "cancel-1"},
        })

    @staticmethod
    def _response(redis_client, request_id="cancel-request-1"):
        return json.loads(
            redis_client.kv["bigqmt:rpc:resp:acct:%s" % request_id])

    def test_truthy_cancel_of_nonexistent_order_reports_failure(self):
        # The #151 probes: a made-up sysid must not come back success=True.
        gateway = FalseyCancelGateway([], native_success=True)
        redis_client, service = self._service(gateway, timeout=0.0)
        self._cancel(service)

        service.drain_pending()

        response = self._response(redis_client)
        self.assertFalse(response["data"]["success"])
        self.assertIn("was not found", response["data"]["message"])

    def test_truthy_cancel_confirms_immediately_when_already_54(self):
        gateway = FalseyCancelGateway(["54"], native_success=True)
        redis_client, service = self._service(gateway)
        self._cancel(service)

        service.drain_pending()

        # Confirmed on the fast path: no settlement parked, one lookup only.
        self.assertEqual(service.pending_settlement_count(), 0)
        response = self._response(redis_client)
        self.assertTrue(response["data"]["success"])
        self.assertEqual(response["data"]["message"], "")
        self.assertEqual(gateway.lookups, 1)

    def test_truthy_cancel_of_filled_order_reports_failure(self):
        # Native says success; the order was already fully filled (56).
        gateway = FalseyCancelGateway(["56"], native_success=True)
        redis_client, service = self._service(gateway)
        self._cancel(service)

        service.drain_pending()

        response = self._response(redis_client)
        self.assertFalse(response["data"]["success"])
        self.assertIn("reached status 56", response["data"]["message"])

    def test_truthy_cancel_waits_when_status_not_yet_terminal(self):
        gateway = FalseyCancelGateway(["50", "54"], native_success=True)
        redis_client, service = self._service(gateway)
        self._cancel(service)

        service.drain_pending()

        # The immediate lookup saw 50 (cancel not processed yet), so the
        # reply only went out after a SECOND lookup saw 54 -- the truthy
        # native return alone did not answer. lookups==2 is the assertion.
        response = self._response(redis_client)
        self.assertTrue(response["data"]["success"])
        self.assertEqual(gateway.lookups, 2)

    def test_truthy_cancel_fast_path_lookup_error_still_parks(self):
        gateway = FalseyCancelGateway(
            ["54"], native_success=True,
            query_error=RuntimeError("QMT query unavailable"))
        redis_client, service = self._service(gateway, timeout=0.0)
        self._cancel(service)

        # The failed immediate probe must not crash the handler; the
        # settlement parks and fails honestly at the deadline.
        service.drain_pending()

        response = self._response(redis_client)
        self.assertFalse(response["data"]["success"])
        self.assertIn("cancel status lookup failed", response["data"]["message"])


class CancelInFlightStatusTest(unittest.TestCase):
    """51/52 (已报待撤/部成待撤): the cancel is accepted and on its way."""

    @staticmethod
    def _service(gateway, timeout=5.0):
        redis_client = FakeRedis()
        handlers = BigQmtRpcHandlers(
            account_id="acct",
            market_data=FakeMarketData(),
            position_provider=FakePositionProvider(),
            order_gateway=gateway,
            allow_order_methods=True,
            order_settle_timeout_seconds=timeout,
        )
        return redis_client, RedisPubSubRpcService(
            redis_client, handlers, account_id="acct")

    @staticmethod
    def _cancel(service, request_id="cancel-request-1"):
        service.enqueue_payload({
            "request_id": request_id,
            "account_id": "acct",
            "method": "cancel_order_stock_sysid",
            "params": {"order_sysid": "cancel-1"},
        })

    def test_inflight_51_keeps_waiting_then_confirms_54(self):
        gateway = FalseyCancelGateway(["51", "54"])  # falsey native, #148 shape
        redis_client, service = self._service(gateway)
        self._cancel(service)

        service.drain_pending()
        self.assertNotIn(
            "bigqmt:rpc:resp:acct:cancel-request-1", redis_client.kv)
        self.assertEqual(service.pending_settlement_count(), 1)

        service.drain_pending()
        response = json.loads(
            redis_client.kv["bigqmt:rpc:resp:acct:cancel-request-1"])
        self.assertTrue(response["data"]["success"])
        self.assertEqual(response["data"]["message"], "")

    def test_inflight_52_at_deadline_reports_acceptance_not_failure(self):
        gateway = FalseyCancelGateway(["52"])  # stuck in flight at deadline
        redis_client, service = self._service(gateway, timeout=0.0)
        self._cancel(service)

        service.drain_pending()

        response = json.loads(
            redis_client.kv["bigqmt:rpc:resp:acct:cancel-request-1"])
        self.assertTrue(response["data"]["success"])
        self.assertIn("in flight", response["data"]["message"])
        self.assertIn("status 52", response["data"]["message"])

    def test_inflight_51_at_deadline_reports_acceptance_not_failure(self):
        gateway = FalseyCancelGateway(["51"])
        redis_client, service = self._service(gateway, timeout=0.0)
        self._cancel(service)

        service.drain_pending()

        response = json.loads(
            redis_client.kv["bigqmt:rpc:resp:acct:cancel-request-1"])
        self.assertTrue(response["data"]["success"])
        self.assertIn("status 51", response["data"]["message"])


if __name__ == "__main__":
    unittest.main()
