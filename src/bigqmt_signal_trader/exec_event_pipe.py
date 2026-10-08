# coding: utf-8
"""Non-blocking QMT execution-event delivery over a dedicated named pipe."""

import json
import queue
import threading
import time
import uuid

from .transports.pipe_transport import NamedPipeTransport


PUBLISH_EXEC_EVENT_METHOD = "publish_exec_event"
PERMANENT_EVENT_ERRORS = frozenset(
    ("INVALID_EVENT", "ACCOUNT_MISMATCH", "EVENT_TOO_LARGE")
)


class ExecEventQueueFull(RuntimeError):
    pass


class NamedPipeExecEventSink(object):
    """A ``publish(topic, data)`` sink which never performs I/O in callbacks.

    One daemon worker preserves FIFO order. An item remains at the head of the
    queue until the proxy acknowledges it, so reconnects reuse the same event
    ID and are safe for the proxy's Redis-side deduplication.
    """

    def __init__(self, account_id, pipe_name="bigqmt_exec", queue_capacity=4096,
                 connect_timeout_seconds=0.25, ack_timeout_seconds=2.0,
                 max_event_bytes=1024 * 1024, retry_interval_seconds=0.25,
                 transport=None, log=None, instance_id=None):
        self.account_id = str(account_id or "")
        self.pipe_name = str(pipe_name or "bigqmt_exec")
        self.queue_capacity = max(1, int(queue_capacity))
        self.ack_timeout_seconds = max(0.01, float(ack_timeout_seconds))
        self.max_event_bytes = max(1, int(max_event_bytes))
        self.retry_interval_seconds = max(0.01, float(retry_interval_seconds))
        self._transport = transport or NamedPipeTransport(
            account_id=self.account_id,
            pipe_name=self.pipe_name,
            connect_timeout_seconds=float(connect_timeout_seconds),
            print_prefix="[bigqmt_exec_pipe]",
        )
        self._log = log or (lambda message: print("[bigqmt_exec_pipe] %s" % message))
        self._queue = queue.Queue(maxsize=self.queue_capacity)
        self._instance_id = str(instance_id or uuid.uuid4().hex)
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._running = False
        self._thread = None
        self._overflow = 0
        self._published = 0
        self._failures = 0
        self._last_overflow_log = 0.0

    def start(self):
        if self._running:
            return self
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="bigqmt-exec-event-pipe", daemon=True
        )
        self._thread.start()
        return self

    def _next_event_id(self):
        with self._sequence_lock:
            self._sequence += 1
            return "%s:%d" % (self._instance_id, self._sequence)

    def publish(self, topic, data):
        if not isinstance(data, dict):
            raise ValueError("execution event must be a dict")
        event = dict(data)
        event_id = str(event.get("event_id") or self._next_event_id())
        event["event_id"] = event_id
        envelope = {
            "schema_version": 1,
            "request_id": event_id,
            "account_id": self.account_id,
            "method": PUBLISH_EXEC_EVENT_METHOD,
            "params": {"event": event},
        }
        encoded_size = len(json.dumps(
            envelope, ensure_ascii=False, default=str).encode("utf-8")
        )
        if encoded_size > self.max_event_bytes:
            raise ValueError("execution event exceeds max_event_bytes")
        try:
            self._queue.put_nowait(envelope)
        except queue.Full:
            self._overflow += 1
            now = time.time()
            if now - self._last_overflow_log >= 5.0:
                self._last_overflow_log = now
                self._log("ERROR event queue full; newest event rejected "
                          "overflow=%d" % self._overflow)
            raise ExecEventQueueFull("execution event queue is full")
        return 1

    def _valid_ack(self, envelope, response):
        return (
            isinstance(response, dict)
            and str(response.get("request_id") or "") == envelope["request_id"]
            and str(response.get("account_id") or "") == self.account_id
            and str(response.get("method") or "") == PUBLISH_EXEC_EVENT_METHOD
        )

    def _run(self):
        current = None
        while self._running:
            if current is None:
                try:
                    current = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue
            try:
                response = self._transport.send_request(
                    current, timeout_seconds=self.ack_timeout_seconds
                )
                if not self._valid_ack(current, response):
                    raise RuntimeError("invalid execution-event acknowledgement")
                if response.get("ok"):
                    self._published += 1
                    self._queue.task_done()
                    current = None
                    continue
                error = str(response.get("event_error") or "EVENT_REDIS_ERROR")
                if error in PERMANENT_EVENT_ERRORS:
                    self._failures += 1
                    self._log("ERROR permanent event rejection id=%s code=%s" % (
                        current["request_id"], error))
                    self._queue.task_done()
                    current = None
                    continue
                raise RuntimeError("transient event rejection: %s" % error)
            except Exception as exc:
                self._failures += 1
                if self._failures <= 3 or self._failures % 100 == 0:
                    self._log("event delivery failed x%d: %s" % (self._failures, exc))
                time.sleep(self.retry_interval_seconds)

    def status(self):
        return {
            "running": self._running,
            "queued": self._queue.qsize(),
            "capacity": self.queue_capacity,
            "published": self._published,
            "failures": self._failures,
            "overflow": self._overflow,
        }

    def stop(self, timeout_seconds=2.0):
        self._running = False
        try:
            self._transport.stop()
        except Exception:
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(max(0.0, float(timeout_seconds)))
        self._thread = None


def build_exec_event_pipe_sink(config, account_id, transport=None, log=None):
    config = dict(config or {})
    return NamedPipeExecEventSink(
        account_id=account_id,
        pipe_name=config.get("pipe_name") or "bigqmt_exec",
        queue_capacity=config.get("queue_capacity") or 4096,
        connect_timeout_seconds=config.get("connect_timeout_seconds") or 0.25,
        ack_timeout_seconds=config.get("ack_timeout_seconds") or 2.0,
        max_event_bytes=config.get("max_event_bytes") or 1024 * 1024,
        retry_interval_seconds=config.get("retry_interval_seconds") or 0.25,
        transport=transport,
        log=log,
    )
