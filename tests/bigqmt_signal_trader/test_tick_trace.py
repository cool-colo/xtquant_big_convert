"""Portable tests of timestamps at the actual source and transport boundaries."""

import ctypes
import json
import threading
from copy import deepcopy
from types import SimpleNamespace

import pytest

from bigqmt_signal_trader.quote_push_channel import RedisQuotePushChannel
from bigqmt_signal_trader.quote_push_channel import decode_push_payload
from bigqmt_signal_trader.quote_subscription_manager import ContextInfoQuoteSource
from bigqmt_signal_trader.tick_trace import stamp_quote_batch
from bigqmt_signal_trader.tick_trace import unix_time_ns
from bigqmt_signal_trader.transports.pipe_push_channel import PipeQuotePushChannel


def batch():
    return {
        "000001.SZ": {"time": 1700000000000, "lastPrice": 10.0},
        "600000.SH": {"time": 1700000000001, "bidPrice": [10.0]},
    }


@pytest.fixture(params=["native", "python36"])
def timestamp_clock(request, monkeypatch):
    """Exercise transport boundaries with and without the Python 3.7 API."""
    if request.param == "python36":
        monkeypatch.delattr("bigqmt_signal_trader.tick_trace.time.time_ns", raising=False)

    def set_clock(value):
        if request.param == "native":
            monkeypatch.setattr("bigqmt_signal_trader.tick_trace.time.time_ns", lambda: value)
        else:
            monkeypatch.setattr("bigqmt_signal_trader.tick_trace.time.time", lambda: value / 1e9)

    return set_clock


def test_native_clock_keeps_integer_nanosecond_precision(monkeypatch):
    expected = 1700000000123456789
    monkeypatch.setattr("bigqmt_signal_trader.tick_trace.time.time_ns", lambda: expected)
    assert unix_time_ns() == expected


def test_python36_clock_keeps_unix_epoch_and_nanosecond_units(monkeypatch):
    monkeypatch.delattr("bigqmt_signal_trader.tick_trace.time.time_ns", raising=False)
    monkeypatch.setattr("bigqmt_signal_trader.tick_trace.time.time", lambda: 1700000000.125)
    actual = unix_time_ns()
    assert type(actual) is int
    # The float fallback has the same epoch/unit, but not native ns precision.
    assert abs(actual - 1700000000125000000) <= 256


def test_trace_is_copy_on_write_at_every_stage_and_resets_on_new_callback():
    data = batch()
    original = deepcopy(data)
    first = stamp_quote_batch(data, "qmt_callback_ns", 100, start=True)
    second = stamp_quote_batch(first, "pipe_publish_ns", 200)
    assert data == original
    assert all(tick["_trace"] == {"qmt_callback_ns": 100} for tick in first.values())
    assert all(tick["_trace"] == {"qmt_callback_ns": 100, "pipe_publish_ns": 200}
               for tick in second.values())
    restarted = stamp_quote_batch(second, "qmt_callback_ns", 300, start=True)
    assert all(tick["_trace"] == {"qmt_callback_ns": 300} for tick in restarted.values())


def test_callback_trace_does_not_turn_empty_ticks_into_data_bearing_ticks():
    data = {"000001.SZ": {}, "600000.SH": None}
    assert stamp_quote_batch(data, "qmt_callback_ns", 100, start=True) is data
    data["600000.SH"] = {"lastPrice": 10}
    traced = stamp_quote_batch(data, "qmt_callback_ns", 100, start=True)
    assert traced["000001.SZ"] == {}
    assert traced["600000.SH"]["_trace"] == {"qmt_callback_ns": 100}


@pytest.mark.parametrize("data", [None, "invalid", [], {}, {"value": 1},
                                  {"000001.SZ": {"_trace": "invalid"}}, batch()])
def test_legacy_or_invalid_payload_is_not_given_fabricated_stages(data):
    assert stamp_quote_batch(data, "redis_publish_ns", 123) is data


def test_source_timestamps_whole_quote_and_option_before_normalization(monkeypatch, timestamp_clock):
    callbacks = {}
    context = SimpleNamespace(
        subscribe_whole_quote=lambda codes, callback: callbacks.setdefault("whole", callback) and 1,
        subscribe_quote=lambda *args: callbacks.setdefault("option", args[-1]) and 2,
    )
    received = []
    source = ContextInfoQuoteSource(context)
    source.subscribe(["000001.SZ", "10010974.SHO"], received.append)
    timestamp_clock(100)
    callbacks["whole"](batch())
    assert all(tick["_trace"]["qmt_callback_ns"] == 100 for tick in received[0].values())

    def normalize(data):
        # Change the clock while normalizing: the callback must have taken its
        # timestamp already, not after this work.
        timestamp_clock(200)
        return {"10010974.SHO": {"time": data["time"][-1]}}

    monkeypatch.setattr("bigqmt_signal_trader.quote_subscription_manager.latest_quote_batch", normalize)
    callbacks["option"]({"time": [1, 2]})
    assert received[1]["10010974.SHO"] == {"time": 2, "_trace": {"qmt_callback_ns": 100}}


class RecordingDLL:
    def __init__(self):
        self.frames = []

    def WriteFile(self, handle, payload, length, written, overlapped):
        self.frames.append(json.loads(payload.decode("utf-8")))
        ctypes.cast(written, ctypes.POINTER(ctypes.c_ulong))[0] = length
        return 1

    def CloseHandle(self, handle):
        pass


def test_pipe_marks_before_encoding_and_after_read_before_decoding(monkeypatch, timestamp_clock):
    dll = RecordingDLL()
    wintypes = SimpleNamespace(DWORD=ctypes.c_ulong)
    publisher = PipeQuotePushChannel(bind_address="unused")
    publisher._running = True
    publisher._sub_handles[1] = threading.Lock()
    publisher._dll = lambda: (dll, wintypes)
    data = stamp_quote_batch(batch(), "qmt_callback_ns", 100, start=True)
    original = deepcopy(data)
    timestamp_clock(200)
    publisher.publish("SH,SZ", data)
    assert data == original
    frame = dll.frames[0]
    assert all(tick["_trace"]["pipe_publish_ns"] == 200 for tick in frame["data"].values())

    subscriber = PipeQuotePushChannel(connect_address="unused")
    subscriber._dll = lambda: (dll, wintypes)
    subscriber._connect_client = lambda stopped: 1
    subscriber._read_one_message = lambda *args: json.dumps(frame).encode()
    stopped = threading.Event()
    received = []

    def callback(topic, payload):
        received.append(payload)
        stopped.set()

    real_loads = json.loads

    def slow_decode(raw):
        timestamp_clock(400)
        return real_loads(raw)

    timestamp_clock(300)
    monkeypatch.setattr("bigqmt_signal_trader.transports.pipe_push_channel.json.loads", slow_decode)
    subscriber._sub_loop(set(), callback, stopped)
    assert all(tick["_trace"]["proxy_pipe_received_ns"] == 300 for tick in received[0].values())


@pytest.mark.parametrize("encoding", ["json", "msgpack"])
def test_redis_marks_before_encoding_and_after_receive_before_decode(encoding, monkeypatch, timestamp_clock):
    if encoding == "msgpack":
        pytest.importorskip("msgpack")
    frames = []
    redis = SimpleNamespace(publish=lambda topic, data: frames.append(data))
    channel = RedisQuotePushChannel(redis, account_id="acct", wire_encoding=encoding)
    data = stamp_quote_batch(batch(), "qmt_callback_ns", 100, start=True)
    original = deepcopy(data)
    timestamp_clock(200)
    channel.publish("SH,SZ", data)
    assert data == original
    assert all(tick["_trace"]["redis_publish_ns"] == 200
               for tick in decode_push_payload(frames[0])["data"].values())
    stopped = threading.Event()
    received = []
    channel.redis.pubsub = lambda **kwargs: SimpleNamespace(
        subscribe=lambda *channels: None,
        get_message=lambda **kwargs: {"type": "message", "channel": b"bigqmt:quote_push:acct:SH,SZ", "data": frames[0]},
        close=lambda: None,
    )
    real_decode = decode_push_payload

    def slow_decode(raw):
        timestamp_clock(400)
        return real_decode(raw)

    def callback(topic, payload):
        received.append(payload)
        stopped.set()

    timestamp_clock(300)
    monkeypatch.setattr("bigqmt_signal_trader.quote_push_channel.decode_push_payload", slow_decode)
    channel._sub_loop(["SH,SZ"], callback, stopped)
    assert all(tick["_trace"]["adapter_redis_received_ns"] == 300 for tick in received[0].values())


def test_execution_event_payload_does_not_acquire_quote_trace(monkeypatch, timestamp_clock):
    def unexpected_clock():
        pytest.fail("execution events must not call the diagnostic clock")

    monkeypatch.setattr("bigqmt_signal_trader.quote_push_channel.unix_time_ns", unexpected_clock)
    frames = []
    channel = RedisQuotePushChannel(SimpleNamespace(publish=lambda topic, data: frames.append(data)))
    event = {"event_id": "one", "details": {"_trace": {"qmt_callback_ns": 100}}}
    channel.publish("exec:trade", event)
    assert decode_push_payload(frames[0])["data"] == event


def test_pipe_execution_event_delivery_bypasses_diagnostic_clock(monkeypatch, timestamp_clock):
    def unexpected_clock():
        pytest.fail("execution events must not call the diagnostic clock")

    monkeypatch.setattr("bigqmt_signal_trader.transports.pipe_push_channel.unix_time_ns", unexpected_clock)
    channel = PipeQuotePushChannel(bind_address="unused")
    event = {"trade_id": "T1"}
    with pytest.raises(RuntimeError, match="not running"):
        channel.publish("exec:trade", event)
    channel._running = True
    with pytest.raises(RuntimeError, match="no connected readers"):
        channel.publish("exec:trade", event)
    dll = RecordingDLL()
    channel._dll = lambda: (dll, SimpleNamespace(DWORD=ctypes.c_ulong))
    channel._sub_handles[1] = threading.Lock()
    assert channel.publish("exec:trade", event) == 1
    assert dll.frames == [{"topic": "exec:trade", "data": event}]
