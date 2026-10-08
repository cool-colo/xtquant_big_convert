# Redis-to-Named-Pipe Proxy Design

- Status: Implemented on this branch; Windows/QMT acceptance remains pending
- Code baseline: branch `quant_pipe_bridge_dev` at `391f507`, including
  upstream `main` `4c0a8d8` (`v0.3.61`)
- Consumer baseline: NautilusTrader BigQMT adapter at `4ce268dbaa`
- Last updated: 2026-10-08

## 1. Summary

Some QMT environments prohibit socket activity and may also restrict package
imports and local file access in the embedded Python process. The current
Windows named-pipe transport works in that environment, but only on the local
Windows host. An unchanged remote Linux client still needs Redis.

This design adds a standalone process on the QMT Windows machine:

```text
Linux client (transport=redis; unchanged RPC path)
        |
        | Redis queue or Pub/Sub request
        v
Redis server
        |
        v
Windows proxy (new process, outside QMT)
  RedisTransport: server role
  bounded relay workers
  NamedPipeTransport: client role
  execution-event pipe receiver -> Redis event publisher
        |
        | RPC:    \\.\pipe\bigqmt_rpc_<account_id>
        | events: \\.\pipe\bigqmt_exec_<account_id>
        v
QMT embedded strategy (current code)
  NamedPipeTransport: server role
  RedisPubSubRpcService + existing handlers
  native callbacks -> bounded local event-pipe sink (new)
```

The proxy forwards the existing request dictionary through the RPC pipe and
sends the QMT response through the original Redis reply route. It also
republishes normalized execution events from a separate local event pipe onto
the existing Redis order, trade, order-error, and cancel-error
channels. RPC business payloads and execution-event payloads are not
translated into a new public schema.

## 2. What the current branch already provides

The following are implemented and should be reused:

- `RpcTransport` defines the client (`send_request`) and server
  (`start_receiving`, `send_response`) roles needed by the relay.
- `RedisTransport` receives both the request queue and request Pub/Sub
  channel. Its response path writes the response key, list, and channel in one
  Redis pipeline when supported, with a second client used only as fallback.
- `NamedPipeTransport` uses message-mode Windows pipes, supports messages over
  the 1 MiB buffer by reading `ERROR_MORE_DATA` chunks, and keeps one client
  handle and read buffer per worker thread.
- The pipe server supports multiple simultaneous instances and both receiver
  thread and adjust-driven drain modes. The current factory and runtime now
  forward `config["pipe"]`; a custom `pipe_name` is no longer missing from the
  runtime path.
- `RedisPubSubRpcService.enqueue_payload()` uses
  `payload.setdefault("_received_at", time.time())`. A timestamp inserted by
  the proxy is therefore preserved through the QMT pending queue.
- Requests carry `timeout_seconds` in both the current Redis and non-Redis
  client paths. The generic client default is 30 seconds; the reviewed
  Nautilus adapter explicitly uses 6 seconds by default.
- QMT refuses an expired request before dispatch, using a default 1-second
  margin (capped at 25% of short timeouts). Cancel methods are intentionally
  exempt: a late cancellation is still useful.
- QMT suppresses duplicate order methods by `(account_id, request_id)` for a
  bounded 512-entry, 600-second in-memory window. `get_request_outcome` exposes
  `unknown`, `dispatching`, `dispatched`, `settled`, and `refused` states.
- Native order/trade callbacks are already normalized into the dictionaries
  consumed by `BigQmtXtTrader`; the bridge also defines normalized order-error
  and cancel-error event shapes. The existing Redis publisher defines the
  channel names and stream behavior that the proxy must preserve.
- Pipe mode defaults to no QMT-side Redis block and disables native `xtdata`
  socket access unless explicitly enabled. The dedicated
  `tools/build_pipe_single_file_flat.py` builder produces the flat pipe
  strategy and forces the Redis-dependent features off.

This branch now also implements `bigqmt_signal_trader.redis_pipe_proxy`, the
`bigqmt-redis-pipe-proxy` command, ownership/backpressure/error handling, and
the named-pipe execution-event sink and Redis republisher described below.
The remaining work is validation on the target Windows/QMT environment and
the independent Nautilus cancellation fix in section 3.4.

## 3. NautilusTrader consumer contract

The primary downstream consumer is
`nautilus_trader/adapters/bigqmt` in the sibling NautilusTrader repository.
The proxy is acceptable only if that adapter can connect, load instruments,
poll market and execution state, submit and cancel orders, and reconcile an
uncertain submit without bypassing the safety rules in this document.

### 3.1 RPC and event usage

| Nautilus operation | Current bridge call | Load shape |
|---|---|---|
| Connect | `ping` | One synchronous call; default timeout 6 seconds |
| Load universe | `get_stock_list_in_sector` | Normally one call per configured sector |
| Load instruments | `get_instrument_detail` | Up to 32 asyncio jobs queue into a four-thread RPC pool |
| Quote/depth poll | `get_full_tick([symbol])` | One call per subscription; quote and depth may duplicate it |
| Bar request/poll | `get_market_data_ex` | One symbol per poll; history responses may be large |
| Account state | `query_stock_asset` | Once at connect and each execution poll cycle |
| Positions | `query_stock_positions` | Reports and sellable-volume validation before SELL |
| Orders | `query_stock_orders(..., strategy_name="")` | All account orders, normally once per execution poll |
| Trades | `query_stock_trades(..., strategy_name="")` | All account trades, normally once per execution poll |
| Submit | `order_stock_result` / `order_stock` | Tracked request ID; default timeout 6 seconds |
| Cancel | `cancel_order_stock` | Sequential for cancel-all and batch-cancel paths |

Separately, `register_callback()` starts the current Redis execution-event
subscriber. It expects the four existing per-account order, trade, order-error,
and cancel-error channels continuously; this is the primary execution update
path and is not an RPC call.

The adapter preserves `ClientOrderId` as `order_remark`. Order and trade query
responses must therefore retain `order_remark`/`user_order_id`, broker order
system ID, suffixed `stock_code`, status, trade ID, price, volume, timestamps,
and commission. The relay must remain payload-transparent; losing these fields
breaks Nautilus order correlation or fill deduplication.

### 3.2 Concurrency and cadence

`BigQMTClient` runs blocking bridge calls in a four-worker executor. The data
and execution clients share that object only when account, Redis settings,
transport, and timeout are identical; otherwise each cached client can add
four concurrent RPCs. Whole-universe instrument loading creates up to 32
asyncio jobs, but only four per client are on the RPC wire at once.

With the default execution configuration, real-time Redis callbacks are the
primary order/trade/error path. The fallback reconciliation loop performs
asset, order, and trade queries sequentially, then sleeps for one second. A
SELL adds a positions query before submission. Quote and depth polling create
one task per subscription and can create sustained demand if their interval is
set below the time needed to drain all subscribed symbols.

The proposed eight relay workers therefore cover the normal shared data-plus-
execution client and two distinct four-worker clients. Acceptance tests must
also prove explicit overload behavior; operators running more client
instances must raise `workers`/`max_pending` or reduce polling fan-out.

### 3.3 Required Nautilus configuration for version 1

Use Redis as the Nautilus-facing transport. Disable quote push because version
1 does not relay the whole-quote channel. Keep both polling loops enabled as
reconciliation/backstop paths; execution polling is not a substitute for the
required execution-event relay:

```python
BigQMTDataClientConfig(
    account_id=ACCOUNT,
    redis_host=PROXY_REDIS_HOST,
    redis_port=6379,
    redis_db=5,
    transport="redis",
    rpc_timeout_secs=6.0,
    use_quote_push=False,
    poll_enabled=True,
    poll_interval_secs=MARKET_POLL_INTERVAL,
)

BigQMTExecClientConfig(
    account_id=ACCOUNT,
    account_type="STOCK",
    redis_host=PROXY_REDIS_HOST,
    redis_port=6379,
    redis_db=5,
    transport="redis",
    rpc_timeout_secs=6.0,
    poll_enabled=True,
    poll_interval_secs=1.0,
)
```

`MARKET_POLL_INTERVAL` must be selected from the number of quote/depth
subscriptions and the measured QMT capacity. The adapter default of 60 seconds
is a slow backstop, not a real-time market-data setting. Version 1 must not be
deployed with `use_quote_push=True` and `poll_enabled=False`: the method exists
on the current client, so the adapter's structural capability check reports
push support even though pipe mode has no push data channel.

Disable FormulaServer routing (`BIGQMT_FORMULA_ENABLED=0`) during proxy
acceptance tests so every asserted read traverses Redis, the proxy, and the
pipe. It can be enabled separately after the relay path passes.

The reviewed adapter constructs `StockAccount(account_id)` without forwarding
its `account_type` configuration. This version-1 contract is therefore for the
adapter's current cash A-share (`STOCK`) use. Credit, futures, and other account
types require a separate adapter validation.

The reviewed adapter exposes a Redis password but no username or TLS options.
It therefore works as-is with the default Redis user on a trusted/private
network. A named ACL user or direct TLS requirement needs a companion adapter
configuration change; proxy support for those options alone is insufficient.

### 3.4 Consumer compatibility requirements and known gaps

The current `order_stock_result()` already uses `call_tracked()` and, after a
real timeout, calls `get_request_outcome`. The proxy must preserve that path:
an uncertain submit result must be allowed to become a Redis client timeout.
Returning an `ok=false` `PIPE_TIMEOUT` response instead would raise
`RpcServerRepliedError`; the Nautilus execution client catches it as a normal
failure and emits `OrderRejected` even though the order may be live.

The current Nautilus wrapper has one independent cancellation bug that a relay
cannot repair: `BigQMTClient.cancel_order()` applies `bool()` to
`cancel_order_stock()`, while the current compatibility API returns `0` for
success and `-1` for failure. That reverses the result (`bool(0) == False`,
`bool(-1) == True`). Before version-1 acceptance, the adapter must compare the
return code to zero or call an API with an explicit boolean contract. Proxy
response translation must not be used to hide this consumer bug.

The adapter registers `XtQuantTraderCallback` unconditionally and documents
its order/trade/asset loop as a fallback alongside that real-time callback
feed. Version 1 must therefore republish native execution events to the
existing Redis channels; it must not claim full Nautilus compatibility merely
because `poll_enabled=True`. Polling remains required for reconciliation after
a disconnect or missed event, but it can miss intermediate states, cannot
reconstruct native `on_cancel_error`, and adds at least one poll interval plus
three sequential RPC round trips.

## 4. Goals and scope

### 4.1 Goals

- Keep the Linux RPC client on its existing Redis configuration and wire
  format.
- Support the reviewed NautilusTrader data and execution adapter under the
  version-1 callback-plus-reconciliation configuration in section 3.3.
- Keep Redis, ZMQ, and other socket activity out of the QMT embedded process.
- Reuse the current Redis and pipe transports and preserve typed RPC payloads,
  `request_id`, response fields, and handler behavior.
- Deliver normalized native order/trade and bridge-generated error events
  through a local pipe and the existing Redis event channels.
- Bound relay concurrency and reject excess work explicitly.
- Reject requests that were already queued when a proxy instance starts, so a
  submit cannot be revived after an outage.
- Preserve the current server-side late-dispatch protection across time spent
  in the proxy and QMT queues.
- Never add a relay-level retry after an outcome becomes uncertain.
- Ensure that only one proxy consumes an account's Redis request routes.
- Recover predictably when Redis, QMT, the pipe, or the proxy restarts.

### 4.2 Non-goals for version 1

- Whole-quote push.
- Async download-job queues, full-tick Redis snapshots, position snapshots,
  or general Redis order-identity persistence produced by QMT. The proxy may
  keep only the bounded correlation/deduplication state required for relayed
  execution events.
- Redis Stream signal consumption inside QMT.
- Multiple accounts in one proxy process. Current upstream multi-account code
  deliberately supports secondary endpoints only for Redis and ZMQ; it
  rejects secondary pipe services.
- QMT-side deployment sync, package reload from disk, local cache, or
  persistent file logging in a sandbox that forbids local file access.
- Changing the public Redis RPC envelope.

## 5. Important timing limitation

The current request envelope contains a timeout duration, not a client send
timestamp or absolute deadline. Consequently, neither the existing direct
server nor this proxy can measure time spent before it receives the request.

Version 1 provides these protections without changing the Linux client:

1. all queue entries present before proxy startup are rejected;
2. overload is rejected synchronously instead of creating another long queue;
3. the proxy stamps `_received_at` as soon as it accepts a live request;
4. QMT preserves that value and refuses expired non-cancel requests before
   handler dispatch.

This prevents dispatch after the deadline measured from proxy receipt. It is
not a mathematical end-to-end deadline if a new request is delayed in Redis or
on the network before the proxy sees it. A future strict guarantee requires an
additive client field such as `sent_at` or `deadline`; that would no longer be
an unchanged-client deployment.

## 6. Version 1 behavior

### 6.1 Startup order

The process must start in this order:

1. validate configuration and build Redis clients;
2. acquire the per-account ownership lock;
3. reject the finite pre-start queue snapshot;
4. start lock renewal;
5. construct the RPC pipe client, bounded executor, and execution-event pipe
   receiver;
6. start the execution-event receiver;
7. start the Redis request queue and Pub/Sub receivers;
8. report ready only after all of the above succeed.

The proxy must not consume the request queue before it owns the lock.

### 6.2 Request flow

1. The Linux client sends its normal encoded request to
   `bigqmt:rpc:queue:{account_id}` or the configured request channel.
2. `RedisTransport` decodes it and invokes the proxy ingress callback.
3. Ingress validates the envelope and account, records `time.time()` as the
   proxy receipt time, and attempts to reserve one bounded work slot.
4. If accepted, a worker forwards a copy with
   `_received_at=<proxy receipt time>` through its thread-local pipe handle.
   The original Redis request remains immutable and is retained for reply
   routing.
5. QMT receives the same envelope. The current service preserves
   `_received_at`, applies expiry, account, method, deduplication, thread
   routing, and handler rules, then sends its normal response through the pipe.
6. The worker validates that the response identifies the expected request and
   calls `RedisTransport.send_response(original_request, response)`.
7. The current Redis key/list/channel fan-out completes the Linux client's
   existing wait path.

The ingress callback returns `None`; otherwise `RpcTransport.deliver()` would
automatically send a second response. A worker owns the terminal-delivery
decision for every accepted request: it sends at most one Redis response, or
deliberately sends none for an uncertain write outcome as specified in section
6.5.

#### 6.2.1 Execution-event flow

Execution events use a dedicated one-way pipe; they must never be inserted
into the request/response pipe because an unsolicited frame would break RPC
response correlation.

1. The existing QMT callbacks normalize order/trade payloads with
   `exec_events.py`; settlement/error paths use the same module for
   `order_error` and `cancel_error` payloads exactly as they do for Redis.
2. A new QMT-side event sink adds an internal `event_id` and enqueues the
   normalized dictionary without blocking the callback thread. The ID is a
   per-strategy-instance nonce plus a monotonic sequence number.
3. A sender thread connects to
   `\\.\pipe\bigqmt_exec_<account_id>` using a separate
   `NamedPipeTransport`. It calls `send_request()` with the existing envelope:
   `request_id=event_id`, `method="publish_exec_event"`, and
   `params={"event": normalized_event}`. It retains and resends that same
   envelope after a connection or acknowledgement failure; it never
   regenerates the business event.
4. The proxy validates schema, account, event type, and size, then uses one
   atomic Redis script to deduplicate `event_id`, append the unchanged public
   payload to the existing capped event stream, publish it on the same Redis
   key used as the Pub/Sub channel, and set a bounded deduplication TTL.
5. The proxy acknowledges only after that atomic operation succeeds. A repeat
   `event_id` seen within the deduplication TTL is acknowledged without being
   published twice.

There is one FIFO sender per account and the proxy publishes events from that
connection serially, preserving the order in which the QMT sink accepted
them. If the bounded queue is full, enqueue rejects the newest event and emits
a rate-limited error plus an overflow counter; it must not silently evict an
older unacknowledged event.

The internal `event_id` is also placed in the published event dictionary as an
additive field; the current client ignores unknown fields. The acknowledgement
uses the normal response identity fields and `ok`, plus `event_error` when
unsuccessful. Required public channel
names remain `bigqmt:order_events:{account_id}`,
`bigqmt:trade_events:{account_id}`,
`bigqmt:order_error_events:{account_id}`, and
`bigqmt:cancel_error_events:{account_id}`. Stream trimming and TTL must match
the current `exec_events` publisher.

The QMT-side queue is bounded and reports overflow loudly, but callback code
must not block on pipe or Redis I/O. A temporary event-path outage can
therefore fall back to Nautilus reconciliation for durable order/trade state;
it does not make polling the normal delivery path, and a dropped native
`cancel_error` cannot be reconstructed. Capacity and outage tests must prove
that the intended live load does not overflow the queue.

| Event setting | Proposed default | Meaning |
|---|---:|---|
| `event_queue_capacity` | 4096 | Normalized callbacks retained in QMT memory |
| `event_max_bytes` | 1 MiB | Maximum encoded event frame |
| `event_connect_timeout_seconds` | 0.25 | Wait per connection attempt |
| `event_ack_timeout_seconds` | 2.0 | Wait before reconnecting and resending the same ID |
| `event_dedup_ttl_seconds` | 86400 | Proxy Redis deduplication lifetime |

### 6.3 Validation

Before reserving work, ingress requires:

- a dictionary envelope;
- non-empty `request_id` and `method`;
- `account_id` equal to the proxy account;
- finite, positive `timeout_seconds` for submit methods;
- at least one usable reply route, with route values in the existing envelope
  types.

The canonical submit set is `submit_order`, `submit_orders_batch`, and
`passorder`, including their current aliases. Cancel methods are not part of
the timeout-required rule because current QMT behavior deliberately executes
late cancels.

The proxy must not derive a reply destination from the pipe response. Before
calling `send_response`, it normalizes explicit reply fields from the retained
Redis request (or derives them from the configured templates and that request),
because the current Redis fallback formatter otherwise also consults response
identity fields. A mismatched response `request_id`, `account_id`, or `method`
is `PIPE_PROTOCOL_ERROR`, not a routable response.

### 6.4 Concurrency and backpressure

Use a fixed executor plus a semaphore that bounds all accepted but incomplete
work. Do not rely on the executor's normally unbounded internal queue.

| Setting | Proposed default | Meaning |
|---|---:|---|
| `workers` | 8 | Maximum concurrent pipe round trips |
| `max_pending` | 64 | Total accepted but incomplete requests |
| `pipe_connect_timeout_seconds` | 0.25 | Per-attempt wait for a missing pipe |
| `safety_margin_seconds` | 0.10 | Proxy pre-forward deadline margin |
| `legacy_read_timeout_seconds` | 30 | Operational limit for old reads/cancels |
| `shutdown_grace_seconds` | 10 | Grace for already accepted work |

If no slot is available, return `OVERLOADED` without touching the pipe.
Cross-request ordering is not guaranteed. One synchronous caller remains
ordered because it waits before submitting its next request.

The existing QMT service has its own 200-entry pending queue and may drop its
oldest entry when full. Keeping `max_pending` well below that bound prevents
the proxy from driving that behavior during normal operation.

### 6.5 Timeout and retry semantics

For a request with a positive timeout:

```text
received_at = proxy wall clock at ingress acceptance
deadline    = received_at + timeout_seconds
remaining   = deadline - current proxy wall clock
```

For non-cancel requests, a worker returns `REQUEST_EXPIRED` if `remaining` is
not greater than the proxy safety margin before it calls the pipe transport.
QMT remains the authoritative final guard: its current expiry check uses the
preserved timestamp and its own larger default margin before handler dispatch.

The current `NamedPipeTransport.send_request()` behavior is significant:

- it may reconnect and resend once after a write-side `TransportError`;
- it does not retry a read-side error or timeout;
- its read watchdog starts after pipe connection and the synchronous write;
- synchronous `WriteFile` itself has no deadline.

The proxy must not add another retry around `send_request()`. A read timeout,
broken read, proxy crash after a write, malformed response, or Redis reply
failure is an unknown execution outcome.

For reads, the proxy may return a prompt `PIPE_TIMEOUT`, `PIPE_ERROR`, or
`PIPE_PROTOCOL_ERROR` response because retrying a read has no trading side
effect. For submit and cancel methods, it must not turn an unknown outcome into
an ordinary `ok=false` Redis response. It records the proxy error and sends no
Redis response, allowing the caller's existing wait to expire naturally. This
is required by the current `order_stock_result()` flow: `TimeoutError` triggers
`get_request_outcome`, whereas an error response raises
`RpcServerRepliedError` and bypasses outcome recovery.

Only failures proven to occur before pipe dispatch, such as validation,
startup backlog, overload, request expiry, missing pipe, shutdown, or lock
loss, may be returned immediately for a submit. The caller must use
`get_request_outcome` with the original tracked request ID after every unknown
submit outcome before deciding whether it can be repeated.

Old requests without a positive timeout are handled as follows:

- submit methods: reject with `TIMEOUT_REQUIRED`;
- reads: use `legacy_read_timeout_seconds`;
- cancels: forward using the same operational timeout, preserving current
  late-cancel behavior.

### 6.6 Startup backlog policy

Redis queue entries can survive while the proxy is stopped. After lock
acquisition and before normal reception:

1. read the initial `LLEN` of `bigqmt:rpc:queue:{account_id}`;
2. perform at most that many `LPOP` operations;
3. decode each entry and respond through its own reply route with
   `STALE_BACKLOG`;
4. never forward those entries to QMT.

The single length snapshot is intentional. Existing entries are on the left
because clients use `RPUSH`; requests appended while cleanup runs stay to the
right and are left for normal reception. An undecodable entry has no reliable
reply route and is discarded with a masked diagnostic.

Pub/Sub has no persistent backlog, so this policy applies only to the request
list.

### 6.7 Single-active-proxy lock

Before queue cleanup, acquire:

```text
key:   bigqmt:rpc_proxy:lock:{account_id}
value: <hostname>:<pid>:<random-instance-id>
mode:  SET NX EX 15
```

Renew every 5 seconds with one atomic compare-value-and-expire operation. Safe
release is one atomic compare-value-and-delete operation. Plain `GET` followed
by `EXPIRE` or `DELETE` is not sufficient.

Failure to acquire prevents startup. If ownership is lost or cannot be proven
before the lease expires, stop accepting immediately. Requests racing with
shutdown receive `LOCK_LOST` or `SHUTTING_DOWN`; already accepted work gets the
configured grace period. The process then exits non-zero for its supervisor to
restart.

### 6.8 Shutdown

Shutdown proceeds in this order:

1. close the ingress gate;
2. stop Redis reception;
3. stop accepting new execution-event pipe connections, causing the QMT sink
   to retain new events in its bounded queue;
4. wait up to the grace period for accepted RPC and event-publish workers;
5. stop the RPC pipe transport, which cancels blocking reads and closes tracked
   client handles;
6. close event-pipe handles;
7. release the lock only if still owned;
8. close Redis clients.

Accepted work is never silently resubmitted during shutdown. If its result
cannot be returned, log it as an unknown client outcome.

## 7. Proxy errors and delivery policy

Proxy failures that are safe to deliver retain the normal RPC response shape
and add `proxy_error`:

```json
{
  "schema_version": 1,
  "request_id": "original request id",
  "account_id": "original account id",
  "method": "original method",
  "ok": false,
  "data": null,
  "error": "ProxyPipeUnavailable: QMT named pipe is not available",
  "handled_at": "YYYY-MM-DD HH:MM:SS",
  "proxy_error": "PIPE_UNAVAILABLE"
}
```

| Code | Meaning | Reached QMT | Redis handling |
|---|---|---|---|
| `INVALID_REQUEST` | Envelope or account validation failed | No | Return error |
| `STALE_BACKLOG` | Entry existed before proxy startup | No | Return error |
| `OVERLOADED` | Accepted-work bound is full | No | Return error |
| `REQUEST_EXPIRED` | Non-cancel deadline exhausted before forwarding | No | Return error |
| `TIMEOUT_REQUIRED` | Legacy submit has no positive timeout | No | Return error |
| `PIPE_UNAVAILABLE` | Local QMT pipe could not be opened | No | Return error |
| `PIPE_TIMEOUT` | No pipe response before the read watchdog | Possibly | Return for reads; suppress for writes |
| `PIPE_ERROR` | Other pipe failure after possible write | Unknown | Return for reads; suppress for writes |
| `PIPE_PROTOCOL_ERROR` | Response identity or shape is invalid | Yes/unknown | Return for reads; suppress for writes |
| `SHUTTING_DOWN` | Ingress is closed | No | Return error |
| `LOCK_LOST` | Ownership lease was lost | No new requests | Return error for rejected ingress |

`error` remains human-readable for current clients. `proxy_error` is additive
for operations and future client diagnostics. A suppressed write error still
uses the stable code in logs and counters, but deliberately has no Redis
response envelope.

Execution-event acknowledgements use the existing response envelope with
`request_id=event_id`, `method="publish_exec_event"`, `ok`, and an additive
`event_error`. `INVALID_EVENT`, `ACCOUNT_MISMATCH`, and `EVENT_TOO_LARGE` are
permanent negative acknowledgements: the QMT sink drops that frame and raises
an operational alert. `EVENT_REDIS_ERROR` and a missing acknowledgement are
transient: the sink retains the envelope and reconnects with the same ID.
Event retry rules never apply to RPC requests or order methods. Duplicate
suppression is guaranteed only for the configured event deduplication TTL; an
outage longer than that window requires operator review before live trading
resumes.

## 8. Implementation and command

Add:

```text
module:  src/bigqmt_signal_trader/redis_pipe_proxy.py
module:  src/bigqmt_signal_trader/exec_event_pipe.py
tests:   tests/bigqmt_signal_trader/test_redis_pipe_proxy.py
tests:   tests/bigqmt_signal_trader/test_exec_event_pipe.py
command: bigqmt-redis-pipe-proxy
```

Register the command in `pyproject.toml`. The proxy module may import transport
and wire helpers, but must not import the QMT strategy entry point or require a
QMT runtime. `exec_event_pipe.py` must keep its QMT-side imports to the standard
library/Win32 `ctypes` path so the flat restricted build needs neither Redis nor
another third-party package. Install the Redis optional dependency only in the
proxy environment.

Proposed CLI:

```text
bigqmt-redis-pipe-proxy \
  --account-id ACCOUNT \
  [--redis-url URL | --redis-host HOST --redis-port PORT --redis-db DB] \
  [--redis-username USER] [--redis-password-env NAME] \
  [--pipe-name bigqmt_rpc] \
  [--event-pipe-name bigqmt_exec] \
  [--workers 8] [--max-pending 64] \
  [--pipe-connect-timeout 0.25] \
  [--safety-margin 0.10] \
  [--legacy-read-timeout 30] \
  [--event-max-bytes 1048576] \
  [--event-dedup-ttl 86400] \
  [--shutdown-grace 10]
```

Rules:

- `--account-id` is required and non-empty.
- `--redis-url` is mutually exclusive with individual address options.
- Passwords come from an environment variable, never a literal argument or
  log line.
- Address defaults match the current client: `127.0.0.1:6379`, DB 5, RESP2
  where the installed redis-py supports that option.
- The pipe path is produced by the current `pipe_path(pipe_name, account_id)`.
- RPC and execution events must use distinct pipe names.
- One process serves one account.
- Run in the foreground under a service supervisor; native Windows service
  integration is outside version 1.

## 9. QMT deployment on the current branch

Recommended QMT configuration:

```python
BIGQMT_REDIS_CONFIG = {
    "transport": "pipe",
    "pipe": {
        "pipe_name": "bigqmt_rpc",
    },
    "rpc_background_threads": False,
    "schedule_adjust": True,
    "schedule_adjust_interval": "100nMilliSecond",
    "rpc_allow_order_methods": False,  # enable after read-only acceptance
    "redis_enabled": False,            # explicit for audit clarity
    "native_xtdata_enabled": False,    # avoids the local 58610 socket
    "download_jobs_enabled": False,
    "full_tick_cache_enabled": False,
    "exec_events_enabled": True,
    "exec_events_transport": "pipe",
    "exec_events_pipe_name": "bigqmt_exec",
    "exec_events_queue_capacity": 4096,
    "exec_events_connect_timeout_seconds": 0.25,
    "exec_events_ack_timeout_seconds": 2.0,
    "quote_push": {"enabled": False},
}
```

Unlike the older code on which this document was based, the current runtime
does forward the `pipe` block. `redis_enabled=False` and
`native_xtdata_enabled=False` are already the effective pipe defaults, but
keeping them explicit makes the no-socket requirement auditable.

The runtime forwards `exec_events_transport` and the related flat keys into its
nested `exec_events` configuration. `_exec_event_sink()` selects the local
pipe sink before Redis or quote-push sinks, keeping network I/O out of QMT
while retaining callback normalization and pre-system-ID hold logic.
`BigQmtRpcHandlers._exec_event_sink()` receives the same sink so an order
refused by the settlement path is not lost merely because it did not produce a
normal order callback.

For a restricted terminal, build the current flat pipe strategy with:

```text
python tools/build_pipe_single_file_flat.py
```

The generated `src/BIGQMT_DRYRUN_PIPE_FLAT_ALL_IN_ONE.py` is intentionally
gitignored. `tools/build_pipe_single_file_flat.py` embeds
`exec_event_pipe.py`, describes polling as reconciliation only, and forces the
event-pipe settings above while still forcing Redis and quote push off. Set
`BIGQMT_LOG_ENABLED=0` before bridge initialization if file logging is
prohibited. Features that read or write local data must also remain disabled.

The Linux client remains configured with `transport="redis"` and its existing
Redis address and credentials. It must not be changed to `transport="pipe"`;
that transport is Windows same-host only.

## 10. Compatibility boundaries

### 10.1 RPC results

Normal QMT responses, including `server_error` and timing fields, pass through
unchanged. FormulaServer fast-path reads in the current client may bypass this
relay entirely; an unreachable FormulaServer falls back to RPC as it does
today.

### 10.2 Execution callbacks

Upstream `v0.3.57` added client-side query polling for pipe/mysql deployments
without a reachable Redis event channel. That does not automatically solve
this topology: the unchanged Linux client is deliberately configured for a
reachable Redis transport, so it selects Redis event subscriptions and will
not enter the polling fallback. QMT pipe mode publishes no Redis execution
events.

Version 1 closes that gap with the dedicated execution-event pipe in section
6.2.1. The proxy republishes the native callback payloads on the Redis channels
to which `BigQmtXtTrader.register_callback()` already subscribes, so Nautilus
continues to receive `on_stock_order`, `on_stock_trade`, `on_order_error`, and
`on_cancel_error` through its primary callback path.

The reviewed Nautilus adapter's separate one-second loop remains enabled and
deduplicates durable order/trade state against callback events. It is a
reconciliation fallback, not the normal execution feed or an acceptable
replacement for the event relay. Polling has up to one interval plus three RPC
round trips of delay, can miss intermediate states, and cannot reconstruct a
native cancel error. A deployment whose event pipe is not healthy is degraded
and does not satisfy live acceptance even if account/order/trade polling still
appears functional.

### 10.3 Whole-quote push

Whole-quote data uses a separate Redis Pub/Sub or ZMQ PUB/SUB channel. The
pipe RPC transport has no push channel, and the current pipe build installs a
null/disabled quote channel. Relaying subscription-control RPCs alone cannot
deliver ticks. The Nautilus adapter must use `use_quote_push=False` and poll
`get_full_tick` in version 1. Its quote and depth subscriptions poll separately,
so capacity tests must include both subscriptions for the same symbol.

### 10.4 Redis-backed storage

Download jobs, full-tick snapshots, position snapshots, unrelated application
streams, and general Redis order-identity persistence require a Redis client
in the QMT process in the current implementation. They remain disabled. The
only version-1 exception is the existing capped execution-event streams, which
the proxy writes together with Pub/Sub delivery as specified in section 6.2.1.
Moving the other features to the proxy would require method-specific
application behavior, not a generic envelope relay.

### 10.5 Multi-account

Although pipe paths contain the account ID, current multi-account assembly
does not build secondary pipe services. Use one QMT pipe deployment and one
proxy per account for version 1. Native multi-account pipe support is separate
work.

## 11. Security and operations

- Run the proxy under the same Windows user as QMT where possible so the
  default pipe ACL permits access.
- Keep the pipe local; do not expose it through SMB.
- Protect cross-machine Redis with authentication, firewalling, and a trusted
  private network or encryption.
- Do not log credentials, Redis URLs containing credentials, full account IDs,
  raw order payloads, or reply payloads.
- Log lifecycle, lock state, masked account, request ID, method, queue depth,
  elapsed time, and terminal result class.
- Count accepted, completed, stale, overloaded, expired, pipe unavailable,
  pipe timeout, protocol error, Redis reply failures, execution events by
  type, duplicate event IDs, reconnects, and QMT event-queue overflow.
- A Redis reply failure after QMT ran is an unknown client outcome. Never
  resend the QMT request to repair the reply.

## 12. Test plan

### 12.1 Unit tests

- Queue and Pub/Sub requests preserve the envelope and typed payloads.
- The original Redis request, not the pipe response, controls reply routing.
- `_received_at` is stamped once and preserved by QMT `setdefault` behavior.
- Submit timeout validation recognizes current aliases; cancels remain exempt.
- Expired, stale, overloaded, invalid, and timeoutless-submit requests never
  touch the pipe.
- Accepted work sends at most one Redis response and always releases its slot.
- Pipe read timeout and unknown errors do not trigger a proxy retry.
- Unknown submit/cancel outcomes produce no Redis response; safe pre-dispatch
  failures and read failures produce one error response.
- Response identity mismatch produces `PIPE_PROTOCOL_ERROR`.
- An initial `LLEN`/`LPOP` snapshot rejects only the pre-start finite set.
- Lock acquisition, atomic renewal, loss, and owned release are correct.
- Shutdown closes ingress before stopping worker pipe handles.
- The QMT event sink never performs pipe I/O on the callback thread, preserves
  normalized fields, and enforces its queue and frame-size bounds.
- Event publish plus deduplication is atomic; an acknowledgement lost after
  publish causes a resend with the same ID and no second Redis publication.
- Invalid account/type/size events are rejected and do not reach Redis.

### 12.2 Integration tests without QMT

- Fake Redis plus a fake pipe service completes both Redis request variants.
- Concurrent requests use no more than `workers` pipe handles and
  `max_pending` accepted slots.
- Redis disconnect/reconnect does not create a relay retry after pipe write.
- Pipe absence returns within the configured operational bound.
- Proxy restart rejects queued submits as `STALE_BACKLOG` with no pipe write.
- A second proxy for the account fails before consuming any request.
- Current client `call_tracked` plus `get_request_outcome` resolves simulated
  uncertain submits.
- Fake QMT callback events traverse the event pipe and appear once on the
  existing Redis stream and Pub/Sub channel after forced reconnects and lost
  acknowledgements.

### 12.3 NautilusTrader contract tests

Run these against the reviewed adapter, not only transport fakes:

- `connect()` completes its `ping` through Redis, proxy, and pipe within the
  adapter's 6-second default timeout.
- A whole-universe load queues 32 instrument jobs while no more than four RPCs
  from one cached `BigQMTClient` are simultaneously active; all
  `get_instrument_detail` results retain their expected fields.
- Two independently cached clients can sustain eight simultaneous calls with
  the proposed default worker count.
- With `use_quote_push=False`, quote and depth subscriptions receive valid
  `get_full_tick` data by polling, including simultaneous quote and depth for
  one symbol.
- Bar requests preserve the typed `get_market_data_ex` result and include a
  response larger than 1 MiB.
- With `poll_enabled=True`, the one-second execution loop repeatedly obtains
  asset, all-account orders, and all-account trades, then correlates
  `order_remark == ClientOrderId` and deduplicates fills by trade ID.
- Native order/trade and bridge-generated order-error/cancel-error events
  traverse the event pipe and Redis channels into `_BigQMTTraderCallback`;
  prove callback delivery independently by pausing the Nautilus reconciliation
  loop during this assertion.
- After callback delivery is proven, re-enable execution polling and verify
  the same order status and trade observed by both paths produce one Nautilus
  state transition and one fill.
- SELL validation receives `can_use_volume` (or `available_amount`) before
  submitting.
- A safe pre-dispatch submit rejection becomes `OrderRejected` with no native
  order; a simulated post-write pipe timeout sends no proxy response, triggers
  `order_stock_result()` timeout recovery, and resolves through
  `get_request_outcome` without a duplicate submit.
- Asynchronous submit acknowledgements with no system ID retain `status` and
  `user_order_id`, allowing Nautilus to remain Submitted until a native order
  callback supplies the broker ID; reconciliation must produce the same result
  if that callback was missed.
- After the required adapter cancellation fix, native success (`0`) and
  failure (`-1`) produce the correct Nautilus result.
- Shared data/execution client connect-reference counting and final shutdown do
  not stop the event or RPC resources while one user remains.

### 12.4 Windows tests

- Run the current named-pipe transport suite and new proxy end-to-end tests.
- Exercise messages below and above 1 MiB.
- Restart QMT with the proxy alive and verify new worker connections recover.
- Restart the proxy with QMT alive and verify startup backlog rejection.
- Test parallel per-thread handles and shutdown while reads are blocked.
- Break and restore the event pipe while native callbacks arrive; verify QMT
  remains responsive, retained events are published once after reconnect, and
  overflow is observable under an intentionally undersized test queue.
- Build and syntax-check the current flat pipe strategy.

### 12.5 QMT acceptance order

1. In a read-only deployment, call `probe_capabilities` through the final
   topology and require the named-pipe creation probe to pass. Confirm native
   `xtdata` is reported skipped in no-socket pipe mode.
2. Verify `ping`, deployment info, representative market/account queries, and
   a response larger than 1 MiB.
3. Produce simulated native order/trade callbacks and bridge-generated
   order-error/cancel-error events, and verify each reaches the existing Redis
   channel exactly once without any QMT network socket.
4. Restart QMT, Redis, and proxy independently while issuing reads; failures
   must be bounded and recovery automatic.
5. Queue requests while the proxy is stopped and verify all pre-start entries
   are rejected without a pipe write.
6. Enable orders only in simulation. Test submit, cancel, overload, client
   timeout, proxy loss after pipe write, duplicate request ID, and
   `get_request_outcome` recovery.
7. Confirm through QMT records that expired/stale submits were not dispatched.
8. Run the Nautilus contract tests with quote push disabled and both market and
   execution polling enabled. Confirm execution callbacks are the primary path
   and measure worst-case fallback poll-cycle duration under the intended
   symbol count.
9. Enable live order methods only after the simulation evidence passes and the
   Nautilus cancellation return-code fix is deployed.

## 13. Acceptance criteria

Version 1 is complete when:

- the standalone proxy and command exist and are covered by tests;
- the `xtquant_big_convert` Linux RPC client needs no source or Redis transport
  change; the separate Nautilus cancellation fix identified in section 3.4 is
  still required;
- the reviewed Nautilus adapter connects with its 6-second timeout and passes
  the section 12.3 contract tests under the documented callback-plus-
  reconciliation configuration;
- QMT creates no Redis/ZMQ/native-xtdata socket connection in the restricted
  pipe configuration;
- supported RPC response data matches direct Redis deployment behavior;
- no request present before proxy startup reaches QMT;
- no submit expired under the proxy-receipt deadline is dispatched;
- an uncertain pipe or Redis reply outcome is never automatically retried by
  the proxy;
- an uncertain Nautilus submit reaches `get_request_outcome` rather than being
  converted into an ordinary rejection;
- native order/trade and bridge-generated order-error/cancel-error events
  traverse the event pipe and existing Redis channels without duplicate
  publication under acknowledgement loss within the deduplication window,
  and ordinary operation does not depend on reconciliation polling;
- the Nautilus cancellation return-code inversion is fixed before live use;
- accepted work and pipe concurrency stay within their configured bounds;
- only one proxy consumes an account's queue and channel;
- restarts produce bounded errors rather than hangs;
- callback outage, whole-quote, storage, deadline, and multi-account
  limitations are visible to operators.

## 14. Follow-up work

Recommended later phases are:

1. extend the dedicated QMT-to-proxy event wire to whole-quote events;
2. an additive absolute client deadline for strict end-to-end expiry;
3. proxy-owned download jobs, snapshots, and identity storage where method
   semantics justify it;
4. native multi-account pipe endpoints;
5. Windows service packaging, health checks, and deployment automation.

Every phase keeps the same boundary: QMT uses local IPC only, while the
standalone proxy owns network I/O.
