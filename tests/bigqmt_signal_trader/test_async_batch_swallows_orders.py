# coding: utf-8
"""#181 rerouted order_stock_async through the batch endpoint, which has a
different contract -- and orders got swallowed.

Reported from live use: on 0.3.20 a loop of async orders places every order;
after #181 the same loop places nothing unless another call is interleaved,
and a burst of orders produces a callback per order while Big QMT's 委托 list
shows one.

Both symptoms are the same cause. _handle_submit_order (the single path)
auto-tags an order that carries no remark and never dedups:

    signal_id = str(params.get("signal_id") or "rpc-%s" % uuid.uuid4().hex)
    order_tag = str(params.get("remark") or params.get("order_remark") or "").strip()
    if not order_tag:
        order_tag = "bqrpc:%s" % signal_id

_handle_submit_orders_batch does the opposite on both counts: no tag is a hard
rejection (ORDER_TAG_REQUIRED), and a tag seen before answers success without
submitting. That idempotency is a deliberate order_stock_batch feature; it was
never part of order_stock_async's contract, and a caller who repeats a remark
-- or omits it -- silently loses orders while every callback reports success.

The interleaving in the report is the batching threshold: one queued job takes
the single path, so slowing the loop down hides the bug.

The fix opts the async path out rather than weakening the batch endpoint:
order_stock_batch callers supply tags precisely so a retry cannot double-order,
and that default is pinned below.

These are order-placement tests and they lead the file deliberately: a wrong
answer here costs real money, unlike anything else in the suite.
"""
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway
from test_redis_rpc import FakeMarketData, FakePositionProvider


def _handlers(gateway):
    return BigQmtRpcHandlers(
        account_id="acct", market_data=FakeMarketData(),
        position_provider=FakePositionProvider(), order_gateway=gateway,
        allow_order_methods=True,
    )


def _item(code, remark=None):
    item = {"stock_code": code, "order_type": 23, "order_volume": 100,
            "price_type": 11, "price": 10.0, "strategy_name": "s"}
    if remark is not None:
        item["order_remark"] = remark
    return item


class AsyncBacklogPlacesEveryOrderTest(unittest.TestCase):
    """With idempotency opted out, the batch places one order per item."""

    def test_batch_without_remark_places_every_order(self):
        """No remark is the common async case -- it must not be a rejection.

        The single path invents "bqrpc:<uuid>" for exactly this; the batch
        endpoint answered ORDER_TAG_REQUIRED and placed nothing, which is the
        reported "in a loop, ordering alone places no order".
        """
        gateway = DryRunOrderGateway()
        results = _handlers(gateway)._handle_submit_orders_batch({
            "account_id": "acct", "idempotent": False,
            "orders": [_item("600000.SH"), _item("600001.SH"), _item("600002.SH")],
        })
        self.assertEqual(len(gateway.submitted), 3,
                         "batch placed %d of 3 orders; results=%r"
                         % (len(gateway.submitted), results))
        self.assertTrue(all(r.get("success") for r in results), results)

    def test_batch_with_repeated_remark_places_every_order(self):
        """A repeated remark is legal for async and must not dedup.

        This is the "many orders at once, one order in the UI" report: items
        2..N matched _submit_journal on the shared tag and answered
        success/idempotent without ever reaching the gateway.
        """
        gateway = DryRunOrderGateway()
        results = _handlers(gateway)._handle_submit_orders_batch({
            "account_id": "acct", "idempotent": False,
            "orders": [_item("600000.SH", "same"), _item("600001.SH", "same"),
                       _item("600002.SH", "same")],
        })
        self.assertEqual(len(gateway.submitted), 3,
                         "batch placed %d of 3 orders; results=%r"
                         % (len(gateway.submitted), results))

    def test_reported_success_never_exceeds_orders_actually_placed(self):
        """The dangerous half: success is reported for orders that never ran.

        A caller reading these results has no way to tell a placed order from a
        swallowed one, so this holds even if the counts above are allowed to
        differ for some other reason.
        """
        gateway = DryRunOrderGateway()
        results = _handlers(gateway)._handle_submit_orders_batch({
            "account_id": "acct", "idempotent": False,
            "orders": [_item("600000.SH", "same"), _item("600001.SH", "same")],
        })
        claimed = sum(1 for r in results if r.get("success"))
        self.assertLessEqual(claimed, len(gateway.submitted),
                             "reported %d successes for %d placed orders: %r"
                             % (claimed, len(gateway.submitted), results))


class SinglePathControlTest(unittest.TestCase):
    """0.3.20's path, unchanged -- proves the batch endpoint is the deviation."""

    def test_single_path_places_every_order_without_a_remark(self):
        gateway = DryRunOrderGateway()
        handlers = _handlers(gateway)
        for code in ("600000.SH", "600001.SH", "600002.SH"):
            handlers._handle_submit_order(dict(_item(code), wait_settlement=False))
        self.assertEqual(len(gateway.submitted), 3)

    def test_single_path_places_every_order_with_a_repeated_remark(self):
        gateway = DryRunOrderGateway()
        handlers = _handlers(gateway)
        for code in ("600000.SH", "600001.SH", "600002.SH"):
            handlers._handle_submit_order(
                dict(_item(code, "same"), wait_settlement=False))
        self.assertEqual(len(gateway.submitted), 3)


class OldServerFallbackTest(unittest.TestCase):
    """A new client against a server that predates the idempotent flag.

    The flag is ignored there, so the client's unique per-item signal_id is
    what keeps the no-remark case working: the batch tag falls back to
    signal_id, and distinct ids cannot collide into a dedup.
    """

    def test_unique_signal_ids_survive_a_server_without_the_flag(self):
        gateway = DryRunOrderGateway()
        orders = []
        for code in ("600000.SH", "600001.SH", "600002.SH"):
            item = _item(code)
            item["signal_id"] = "bqrpc:%s" % code
            orders.append(item)
        _handlers(gateway)._handle_submit_orders_batch({
            "account_id": "acct", "orders": orders})   # no idempotent flag
        self.assertEqual(len(gateway.submitted), 3)


class BatchContractUnchangedTest(unittest.TestCase):
    """order_stock_batch's own callers must see exactly the old behaviour.

    Its idempotency is the guard against a retried batch double-ordering, so
    the async fix must not reach it. Default = today.
    """

    def test_missing_tag_is_still_rejected_by_default(self):
        gateway = DryRunOrderGateway()
        results = _handlers(gateway)._handle_submit_orders_batch({
            "account_id": "acct", "orders": [_item("600000.SH")]})
        self.assertEqual(len(gateway.submitted), 0)
        self.assertEqual(results[0]["error"], "ORDER_TAG_REQUIRED")
        self.assertTrue(results[0]["explicit_failure"])

    def test_repeated_tag_is_still_deduped_by_default(self):
        gateway = DryRunOrderGateway()
        results = _handlers(gateway)._handle_submit_orders_batch({
            "account_id": "acct",
            "orders": [_item("600000.SH", "tag"), _item("600001.SH", "tag")]})
        self.assertEqual(len(gateway.submitted), 1)
        self.assertFalse(results[0]["idempotent"])
        self.assertTrue(results[1]["idempotent"])

    def test_opting_out_does_not_journal_tags_for_later_batches(self):
        """An async order must not suppress a later deliberate batch item."""
        gateway = DryRunOrderGateway()
        handlers = _handlers(gateway)
        handlers._handle_submit_orders_batch({
            "account_id": "acct", "idempotent": False,
            "orders": [_item("600000.SH", "shared")]})
        results = handlers._handle_submit_orders_batch({
            "account_id": "acct",
            "orders": [_item("600001.SH", "shared")]})
        self.assertEqual(len(gateway.submitted), 2)
        self.assertFalse(results[0]["idempotent"])


class TightLoopEndToEndTest(unittest.TestCase):
    """The reporter's scenario verbatim, offline: a tight order_stock_async
    loop with NO remark, driven through the real worker/batcher/dispatcher and
    answered by the real server handler (dry-run gateway standing in for
    passorder).

    Live on the fixed build (2026-09-04, after close, 20 limit-down buys):
    20/20 orders reached the submit path and were recorded; before the fix the
    same loop placed 0 (ORDER_TAG_REQUIRED swallowed the batch). Everything in
    between -- queueing, batch selection, the idempotent=False opt-out, unique
    per-item tagging, per-seq callbacks -- is what this pins.
    """

    def test_tight_loop_without_remarks_places_and_answers_every_order(self):
        from bigqmt_signal_trader.xtquant_compat import (
            BigQmtXtTrader, XtQuantTraderCallback)

        gateway = DryRunOrderGateway()
        handlers = _handlers(gateway)

        class Recorder(XtQuantTraderCallback):
            def __init__(self):
                self.responses = []
                self.errors = []

            def on_order_stock_async_response(self, r):
                self.responses.append(r)

            def on_order_error(self, e):
                self.errors.append(e)

        trader = BigQmtXtTrader(account_id="acct")
        recorder = Recorder()
        trader.register_callback(recorder)

        # The batch seam wired to the REAL server handler, the way the fixed
        # bridge answers it.
        def real_batch(account, orders, batch_id="", idempotent=True):
            params = {"account_id": "acct", "orders": list(orders)}
            params["idempotent"] = idempotent
            return handlers._handle_submit_orders_batch(params)

        trader.order_stock_batch = real_batch
        n = 20
        seqs = [trader.order_stock_async("acct", "601398.SH", 23, 100, 11, 7.32)
                for _ in range(n)]

        self.assertTrue(trader.wait_async_orders(timeout=5.0))
        trader.stop()

        self.assertEqual(len(gateway.submitted), n,
                         "placed %d of %d orders" % (len(gateway.submitted), n))
        self.assertEqual([e.error_msg for e in recorder.errors], [])
        self.assertEqual(len(recorder.responses), n)
        self.assertEqual([r.seq for r in recorder.responses], seqs)
        order_ids = [str(r.order_id) for r in recorder.responses]
        self.assertEqual(len(set(order_ids)), n,
                         "order ids must not collapse: %r" % order_ids)


class AsyncPathSendsTheOptOutTest(unittest.TestCase):
    """The client half: the async backlog must actually opt out."""

    def test_submit_async_batch_opts_out_and_tags_each_item(self):
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader

        trader = BigQmtXtTrader.__new__(BigQmtXtTrader)
        seen = {}

        def fake_batch(account, orders, batch_id="", idempotent=True):
            seen["idempotent"] = idempotent
            seen["signal_ids"] = [o.get("signal_id") for o in orders]
            return [{"index": i, "success": True, "order_sys_id": "s%d" % i,
                     "user_order_id": "", "code": 0, "error": ""}
                    for i in range(len(orders))]

        trader.order_stock_batch = fake_batch
        trader._enqueue_batch_outcome = lambda seq, fields, entry: None
        group = [((i, (), dict(stock_code="60000%d.SH" % i, order_type=23,
                               order_volume=100, price_type=11, price=10.0)),
                  {"stock_code": "60000%d.SH" % i, "order_type": 23,
                   "order_volume": 100, "price_type": 11, "price": 10.0})
                 for i in range(3)]
        trader._submit_async_batch("acct", group)

        self.assertIs(seen["idempotent"], False)
        self.assertEqual(len(set(seen["signal_ids"])), 3, seen["signal_ids"])
        self.assertTrue(all(seen["signal_ids"]), seen["signal_ids"])
        # "rpc-" so the server derives the same "bqrpc:rpc-<hex>" remark the
        # single path does; see RemarkShapeUnchangedTest.
        self.assertTrue(all(s.startswith("rpc-") for s in seen["signal_ids"]),
                        seen["signal_ids"])


class RemarkShapeUnchangedTest(unittest.TestCase):
    """A no-remark async order must carry the same remark it did on 0.3.20.

    The string is visible in QMT's 备注 column and is what the settlement
    lookup matches on (#152), so the auto-tag's shape is not free to drift --
    an earlier draft of the #190 fix produced "bqrpc:bqrpc:<hex>".
    """

    def _remarks(self, gateway):
        return [str(getattr(r, "remark", "") or "") for r in gateway.submitted]

    def test_batch_auto_tag_matches_the_single_path(self):
        single = DryRunOrderGateway()
        _handlers(single)._handle_submit_order(
            dict(_item("600000.SH"), wait_settlement=False))
        batched = DryRunOrderGateway()
        _handlers(batched)._handle_submit_orders_batch({
            "account_id": "acct", "idempotent": False,
            "orders": [_item("600000.SH")]})

        one, many = self._remarks(single)[0], self._remarks(batched)[0]
        self.assertTrue(one.startswith("bqrpc:rpc-"), one)
        self.assertEqual(len(one), len(many), (one, many))
        self.assertEqual(one.rsplit("-", 1)[0], many.rsplit("-", 1)[0], (one, many))
        self.assertNotIn("bqrpc:bqrpc:", many)


if __name__ == "__main__":
    unittest.main()
