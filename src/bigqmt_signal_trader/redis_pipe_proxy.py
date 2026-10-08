"""Standalone Redis-to-named-pipe RPC and execution-event proxy."""

import argparse
import concurrent.futures
import json
import math
import os
import signal
import socket
import threading
import time
import uuid

from .adapters.redis_common import EVENT_STREAM_TTL_SECONDS, build_redis_client
from .exec_event_pipe import PUBLISH_EXEC_EVENT_METHOD
from .redis_rpc import METHOD_ALIASES
from .transports.base import TransportError, TransportTimeout
from .transports.pipe_transport import NamedPipeTransport
from .transports.redis_transport import RedisTransport, _loads


SUBMIT_METHODS = frozenset(("submit_order", "submit_orders_batch", "passorder"))
CANCEL_METHODS = frozenset(("cancel_order", "cancel_orders_batch"))
EVENT_TYPES = frozenset(("order", "trade", "order_error", "cancel_error"))

_LOCK_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_LOCK_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_EVENT_PUBLISH_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 0
end
redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[2], '*', 'payload', ARGV[1])
redis.call('EXPIRE', KEYS[2], ARGV[3])
redis.call('PUBLISH', KEYS[2], ARGV[1])
redis.call('SET', KEYS[1], '1', 'EX', ARGV[4])
return 1
"""

_EVENT_PUBLISH_NO_STREAM_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 0
end
redis.call('PUBLISH', ARGV[1], ARGV[2])
redis.call('SET', KEYS[1], '1', 'EX', ARGV[3])
return 1
"""


def _canonical(method):
    method = str(method or "")
    return METHOD_ALIASES.get(method, method)


def _masked_account(account_id):
    text = str(account_id or "")
    return (text[:3] + "***") if text else "***"


def _response(request, ok, data=None, error="", code=""):
    result = {
        "schema_version": 1,
        "request_id": str((request or {}).get("request_id") or ""),
        "account_id": str((request or {}).get("account_id") or ""),
        "method": str((request or {}).get("method") or ""),
        "ok": bool(ok),
        "data": data,
        "error": str(error or ""),
        "handled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if code:
        result["proxy_error"] = str(code)
    return result


class RedisPipeProxy(object):
    """Relay one account between Redis RPC routes and local named pipes."""

    def __init__(self, redis_client, account_id, pipe_name="bigqmt_rpc",
                 event_pipe_name="bigqmt_exec", workers=8, max_pending=64,
                 pipe_connect_timeout_seconds=0.25, safety_margin_seconds=0.10,
                 legacy_read_timeout_seconds=30.0, shutdown_grace_seconds=10.0,
                 event_max_bytes=1024 * 1024, event_dedup_ttl_seconds=86400,
                 lock_ttl_seconds=15, lock_renew_seconds=5,
                 rpc_pipe=None, event_pipe=None, redis_transport=None, log=None):
        self.redis = redis_client
        self.account_id = str(account_id or "")
        if not self.account_id:
            raise ValueError("account_id is required")
        self.workers = max(1, int(workers))
        self.max_pending = max(self.workers, int(max_pending))
        self.safety_margin_seconds = max(0.0, float(safety_margin_seconds))
        self.legacy_read_timeout_seconds = max(0.01, float(legacy_read_timeout_seconds))
        self.shutdown_grace_seconds = max(0.0, float(shutdown_grace_seconds))
        self.event_max_bytes = max(1, int(event_max_bytes))
        self.event_dedup_ttl_seconds = max(1, int(event_dedup_ttl_seconds))
        self.lock_ttl_seconds = max(3, int(lock_ttl_seconds))
        self.lock_renew_seconds = max(0.2, float(lock_renew_seconds))
        self._log = log or (lambda message: print("[bigqmt_proxy] %s" % message))
        self._rpc_pipe = rpc_pipe or NamedPipeTransport(
            account_id=self.account_id,
            pipe_name=pipe_name,
            connect_timeout_seconds=float(pipe_connect_timeout_seconds),
            print_prefix="[bigqmt_proxy_rpc]",
        )
        self._event_pipe = event_pipe or NamedPipeTransport(
            account_id=self.account_id,
            pipe_name=event_pipe_name,
            print_prefix="[bigqmt_proxy_events]",
        )
        self._redis_transport = redis_transport or RedisTransport(
            redis_client, account_id=self.account_id,
            response_redis_client=redis_client,
            print_prefix="[bigqmt_proxy]",
        )
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="bigqmt-proxy"
        )
        self._slots = threading.BoundedSemaphore(self.max_pending)
        self._running = False
        self._accepting = False
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._renew_thread = None
        self._instance_id = "%s:%s:%s" % (
            socket.gethostname(), os.getpid(), uuid.uuid4().hex
        )
        self._lock_key = "bigqmt:rpc_proxy:lock:%s" % self.account_id
        self._futures = set()
        self._event_streams_supported = True

    def _log_error(self, message):
        try:
            self._log("ERROR %s" % message)
        except Exception:
            pass

    def _acquire_lock(self):
        acquired = self.redis.set(
            self._lock_key, self._instance_id, nx=True, ex=self.lock_ttl_seconds
        )
        if not acquired:
            raise RuntimeError("another proxy owns account %s" % _masked_account(self.account_id))

    def _renew_loop(self):
        while self._running:
            if self._stop_event.wait(self.lock_renew_seconds):
                return
            if not self._running:
                return
            try:
                renewed = self.redis.eval(
                    _LOCK_RENEW_SCRIPT, 1, self._lock_key, self._instance_id,
                    self.lock_ttl_seconds,
                )
            except Exception as exc:
                self._log_error("lock renewal failed: %s" % exc)
                renewed = 0
            if not renewed:
                with self._state_lock:
                    self._accepting = False
                self._log_error("proxy lock lost; refusing new work")
                self._running = False
                return

    def _release_lock(self):
        try:
            self.redis.eval(
                _LOCK_RELEASE_SCRIPT, 1, self._lock_key, self._instance_id
            )
        except Exception as exc:
            self._log_error("lock release failed: %s" % exc)

    def _reject_startup_backlog(self):
        count = int(self.redis.llen(self._redis_transport.request_queue) or 0)
        for _ in range(count):
            raw = self.redis.lpop(self._redis_transport.request_queue)
            if raw is None:
                break
            try:
                request = _loads(raw)
                response = _response(
                    request, False,
                    error="ProxyStaleBacklog: request predates proxy startup",
                    code="STALE_BACKLOG",
                )
                self._redis_transport.send_response(request, response)
            except Exception as exc:
                self._log_error("discarded undecodable startup backlog item: %s" % exc)

    def start(self):
        if self._running:
            return self
        self._acquire_lock()
        try:
            self._reject_startup_backlog()
            self._stop_event.clear()
            self._running = True
            self._event_pipe.start_receiving(self._handle_event, background_threads=True)
            with self._state_lock:
                self._accepting = True
            self._redis_transport.start_receiving(self._ingress, background_threads=True)
            self._renew_thread = threading.Thread(
                target=self._renew_loop, name="bigqmt-proxy-lock", daemon=True
            )
            self._renew_thread.start()
        except Exception:
            self._running = False
            self._accepting = False
            try:
                self._event_pipe.stop()
            except Exception:
                pass
            self._release_lock()
            raise
        self._log("ready account=%s workers=%d max_pending=%d" % (
            _masked_account(self.account_id), self.workers, self.max_pending))
        return self

    def _validate_request(self, request):
        if not isinstance(request, dict):
            return "INVALID_REQUEST", "request must be an object"
        if not str(request.get("request_id") or ""):
            return "INVALID_REQUEST", "request_id is required"
        if not str(request.get("method") or ""):
            return "INVALID_REQUEST", "method is required"
        if str(request.get("account_id") or "") != self.account_id:
            return "INVALID_REQUEST", "account_id does not match proxy"
        for field in ("reply_key", "reply_list", "reply_channel"):
            value = request.get(field)
            if value is not None and not isinstance(value, str):
                return "INVALID_REQUEST", "%s must be a string" % field
        if _canonical(request.get("method")) in SUBMIT_METHODS:
            try:
                timeout = float(request.get("timeout_seconds"))
            except (TypeError, ValueError):
                timeout = 0.0
            if not math.isfinite(timeout) or timeout <= 0:
                return "TIMEOUT_REQUIRED", "positive timeout_seconds is required"
        return "", ""

    def _ingress(self, request):
        with self._state_lock:
            accepting = self._accepting
        if not accepting:
            return _response(
                request, False, error="ProxyShuttingDown: ingress is closed",
                code="SHUTTING_DOWN" if self._running else "LOCK_LOST",
            )
        code, message = self._validate_request(request)
        if code:
            return _response(request, False, error=message, code=code)
        if not self._slots.acquire(False):
            return _response(
                request, False, error="ProxyOverloaded: relay queue is full",
                code="OVERLOADED",
            )
        forwarded = dict(request)
        # Never trust a caller-supplied ingress timestamp: this proxy owns the
        # deadline origin and QMT deliberately preserves it with setdefault().
        forwarded["_received_at"] = time.time()
        try:
            future = self._executor.submit(self._relay, request, forwarded)
        except RuntimeError:
            self._slots.release()
            return _response(
                request, False, error="ProxyShuttingDown: executor is closed",
                code="SHUTTING_DOWN",
            )
        with self._state_lock:
            self._futures.add(future)
        future.add_done_callback(self._future_done)
        return None

    def _future_done(self, future):
        with self._state_lock:
            self._futures.discard(future)
        self._slots.release()
        try:
            future.result()
        except Exception as exc:
            self._log_error("relay worker failed: %s" % exc)

    def _safe_rpc_error(self, request, code, message):
        try:
            self._redis_transport.send_response(
                request, _response(request, False, error=message, code=code)
            )
        except Exception as exc:
            self._log_error("could not return %s: %s" % (code, exc))

    def _relay(self, original, forwarded):
        canonical = _canonical(forwarded.get("method"))
        is_write = canonical in SUBMIT_METHODS or canonical in CANCEL_METHODS
        timeout_value = forwarded.get("timeout_seconds")
        try:
            timeout_seconds = float(timeout_value)
            if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                raise ValueError
            remaining = forwarded["_received_at"] + timeout_seconds - time.time()
        except (TypeError, ValueError):
            remaining = self.legacy_read_timeout_seconds

        if canonical not in CANCEL_METHODS and remaining <= self.safety_margin_seconds:
            self._safe_rpc_error(
                original, "REQUEST_EXPIRED",
                "ProxyRequestExpired: deadline exhausted before pipe dispatch",
            )
            return
        pipe_timeout = (
            self.legacy_read_timeout_seconds
            if canonical in CANCEL_METHODS
            else max(0.01, remaining)
        )
        try:
            response = self._rpc_pipe.send_request(forwarded, pipe_timeout)
        except TransportTimeout as exc:
            if not is_write:
                self._safe_rpc_error(original, "PIPE_TIMEOUT", str(exc))
            else:
                self._log_error("unknown write outcome request_id=%s code=PIPE_TIMEOUT" %
                                original.get("request_id"))
            return
        except TransportError as exc:
            unavailable = "cannot connect" in str(exc).lower()
            if unavailable or not is_write:
                self._safe_rpc_error(
                    original, "PIPE_UNAVAILABLE" if unavailable else "PIPE_ERROR", str(exc)
                )
            else:
                self._log_error("unknown write outcome request_id=%s code=PIPE_ERROR" %
                                original.get("request_id"))
            return
        except Exception as exc:
            if not is_write:
                self._safe_rpc_error(original, "PIPE_ERROR", str(exc))
            else:
                self._log_error("unknown write outcome request_id=%s code=PIPE_ERROR" %
                                original.get("request_id"))
            return

        if not isinstance(response, dict) or any(
            str(response.get(key) or "") != str(forwarded.get(key) or "")
            for key in ("request_id", "account_id", "method")
        ):
            if not is_write:
                self._safe_rpc_error(
                    original, "PIPE_PROTOCOL_ERROR",
                    "ProxyProtocolError: response identity mismatch",
                )
            else:
                self._log_error("unknown write outcome request_id=%s code=PIPE_PROTOCOL_ERROR" %
                                original.get("request_id"))
            return
        try:
            self._redis_transport.send_response(original, response)
        except Exception as exc:
            self._log_error("Redis reply failed request_id=%s: %s" % (
                original.get("request_id"), exc))

    def _event_ack(self, request, ok, error=""):
        response = _response(request, ok, data={"accepted": bool(ok)}, error=error)
        if error:
            response["event_error"] = error
        return response

    def _handle_event(self, request):
        if not isinstance(request, dict):
            return self._event_ack({}, False, "INVALID_EVENT")
        if str(request.get("method") or "") != PUBLISH_EXEC_EVENT_METHOD:
            return self._event_ack(request, False, "INVALID_EVENT")
        if str(request.get("account_id") or "") != self.account_id:
            return self._event_ack(request, False, "ACCOUNT_MISMATCH")
        event_id = str(request.get("request_id") or "")
        event = (request.get("params") or {}).get("event")
        if not event_id or not isinstance(event, dict) or event.get("event_type") not in EVENT_TYPES:
            return self._event_ack(request, False, "INVALID_EVENT")
        if str(event.get("account_id") or self.account_id) != self.account_id:
            return self._event_ack(request, False, "ACCOUNT_MISMATCH")
        event = dict(event)
        event["event_id"] = event_id
        event["account_id"] = self.account_id
        payload = json.dumps(event, ensure_ascii=False, default=str)
        if len(payload.encode("utf-8")) > self.event_max_bytes:
            return self._event_ack(request, False, "EVENT_TOO_LARGE")
        try:
            self._publish_event_atomic(event_id, event["event_type"], payload)
        except Exception as exc:
            self._log_error("event Redis publish failed id=%s: %s" % (event_id, exc))
            return self._event_ack(request, False, "EVENT_REDIS_ERROR")
        return self._event_ack(request, True)

    def _publish_event_atomic(self, event_id, event_type, payload):
        channel_templates = {
            "order": "bigqmt:order_events:{account_id}",
            "trade": "bigqmt:trade_events:{account_id}",
            "order_error": "bigqmt:order_error_events:{account_id}",
            "cancel_error": "bigqmt:cancel_error_events:{account_id}",
        }
        channel = channel_templates[event_type].format(account_id=self.account_id)
        dedup_key = "bigqmt:exec_proxy:event:%s:%s" % (self.account_id, event_id)
        if self._event_streams_supported:
            try:
                return self.redis.eval(
                    _EVENT_PUBLISH_SCRIPT, 2, dedup_key, channel, payload, 2000,
                    EVENT_STREAM_TTL_SECONDS, self.event_dedup_ttl_seconds,
                )
            except Exception as exc:
                text = str(exc).lower()
                if "unknown command" not in text or "xadd" not in text:
                    raise
                self._event_streams_supported = False
                self._log("Redis Streams unavailable; execution events use Pub/Sub only")
        return self.redis.eval(
            _EVENT_PUBLISH_NO_STREAM_SCRIPT, 1, dedup_key, channel, payload,
            self.event_dedup_ttl_seconds,
        )

    def stop(self):
        with self._state_lock:
            self._accepting = False
        self._stop_event.set()
        self._redis_transport.stop()
        try:
            self._event_pipe.stop()
        except Exception:
            pass
        deadline = time.time() + self.shutdown_grace_seconds
        while time.time() < deadline:
            with self._state_lock:
                if not self._futures:
                    break
            time.sleep(0.05)
        try:
            self._rpc_pipe.stop()
        except Exception:
            pass
        self._executor.shutdown(wait=False)
        self._running = False
        self._release_lock()
        renew = self._renew_thread
        if renew is not None and renew.is_alive():
            renew.join(1.0)
        self._renew_thread = None
        close = getattr(self.redis, "close", None)
        if callable(close):
            close()


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    address = parser.add_mutually_exclusive_group()
    address.add_argument("--redis-url")
    address.add_argument("--redis-host", default="127.0.0.1")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--redis-db", type=int, default=5)
    parser.add_argument("--redis-username")
    parser.add_argument("--redis-password-env", default="BIGQMT_REDIS_PASSWORD")
    parser.add_argument("--pipe-name", default="bigqmt_rpc")
    parser.add_argument("--event-pipe-name", default="bigqmt_exec")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-pending", type=int, default=64)
    parser.add_argument("--pipe-connect-timeout", type=float, default=0.25)
    parser.add_argument("--safety-margin", type=float, default=0.10)
    parser.add_argument("--legacy-read-timeout", type=float, default=30.0)
    parser.add_argument("--event-max-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--event-dedup-ttl", type=int, default=86400)
    parser.add_argument("--shutdown-grace", type=float, default=10.0)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.redis_url and any(
        value is not None
        for value in (args.redis_username,)
    ):
        raise SystemExit("--redis-url cannot be combined with --redis-username")
    redis_config = {
        "url": args.redis_url,
        "host": args.redis_host,
        "port": args.redis_port,
        "db": args.redis_db,
        "username": args.redis_username,
        "password": os.environ.get(args.redis_password_env) if args.redis_password_env else None,
        "protocol": 2,
    }
    proxy = RedisPipeProxy(
        build_redis_client(redis_config),
        account_id=args.account_id,
        pipe_name=args.pipe_name,
        event_pipe_name=args.event_pipe_name,
        workers=args.workers,
        max_pending=args.max_pending,
        pipe_connect_timeout_seconds=args.pipe_connect_timeout,
        safety_margin_seconds=args.safety_margin,
        legacy_read_timeout_seconds=args.legacy_read_timeout,
        shutdown_grace_seconds=args.shutdown_grace,
        event_max_bytes=args.event_max_bytes,
        event_dedup_ttl_seconds=args.event_dedup_ttl,
    )
    stopped = threading.Event()

    def _stop(_signum, _frame):
        stopped.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    proxy.start()
    try:
        while not stopped.wait(1.0):
            if not proxy._running:
                return 1
    finally:
        proxy.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
