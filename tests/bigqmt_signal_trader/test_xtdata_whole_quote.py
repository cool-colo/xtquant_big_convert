import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import bigqmt_signal_trader.xtquant_compat as compat


class FakeSession:
    """Captures BigQmtXtData -> WholeQuoteClientSession delegation."""

    def __init__(self):
        self.subscribed = []
        self.unsubscribed = []
        self.replayed = 0
        self.started = False
        self._next = 0
        self._active = set()

    def subscribe_whole_quote(self, code_list, callback=None):
        self._next += 1
        self._active.add(self._next)
        self.subscribed.append((list(code_list), callback))
        return self._next

    def unsubscribe_quote(self, sub_id):
        self.unsubscribed.append(sub_id)
        self._active.discard(sub_id)
        return 0

    def has_subscription(self, sub_id):
        return sub_id in self._active

    def replay_subscriptions(self):
        self.replayed += 1

    def start(self):
        self.started = True

    def stop(self):
        pass


class FakeClient:
    def __init__(self):
        self.account_id = "acct"
        self.local_cache_config = {}
        self.full_tick_cache_config = {}
        self.transport_name = "redis"
        self.calls = []

    def call(self, method, params=None, **kwargs):
        self.calls.append((method, params))
        if method == "get_full_tick":
            return {c: {"lastPrice": 1.0} for c in (params or {}).get("codes") or []}
        return {}

    def _redis(self):
        raise AssertionError("redis not expected in this test")


class XtDataWholeQuoteDelegationTest(unittest.TestCase):
    def _xtdata(self, session):
        client = FakeClient()
        data = compat.BigQmtXtData(client)
        data._quote_session_factory = lambda: session
        return data, client

    def test_subscribe_delegates_to_session_and_primes_full_tick(self):
        session = FakeSession()
        data, _client = self._xtdata(session)
        received = []
        sub_id = data.subscribe_whole_quote(["000001.SZ"], callback=received.append)
        # Delegated to the session.
        self.assertEqual(session.subscribed[0][0], ["000001.SZ"])
        self.assertEqual(sub_id, 1)
        # Primed via get_full_tick so the callback fires once with the snapshot.
        self.assertEqual(received, [{"000001.SZ": {"lastPrice": 1.0}}])

    def test_subscribe_starts_session_once(self):
        session = FakeSession()
        data, _client = self._xtdata(session)
        data.subscribe_whole_quote(["SH"], callback=lambda d: None)
        self.assertTrue(session.started)

    def test_unsubscribe_delegates_to_session(self):
        session = FakeSession()
        data, _client = self._xtdata(session)
        sub_id = data.subscribe_whole_quote(["SH"], callback=lambda d: None)
        data.unsubscribe_quote(sub_id)
        self.assertEqual(session.unsubscribed, [sub_id])

    def test_session_reused_across_subscriptions(self):
        session = FakeSession()
        data, _client = self._xtdata(session)
        data.subscribe_whole_quote(["SH"], callback=lambda d: None)
        data.subscribe_whole_quote(["SZ"], callback=lambda d: None)
        self.assertEqual(len(session.subscribed), 2)

    def test_replay_uses_existing_session_without_snapshot_prime(self):
        session = FakeSession()
        data, client = self._xtdata(session)
        received = []
        data.subscribe_whole_quote(["SH"], callback=received.append)
        calls_before = list(client.calls)
        received_before = list(received)

        self.assertEqual(data.replay_whole_quote_subscriptions(), 1)

        self.assertEqual(session.replayed, 1)
        self.assertEqual(client.calls, calls_before)
        self.assertEqual(received, received_before)

    def test_replay_without_an_active_session_is_a_noop(self):
        data, _client = self._xtdata(FakeSession())

        self.assertEqual(data.replay_whole_quote_subscriptions(), 0)


if __name__ == "__main__":
    unittest.main()
