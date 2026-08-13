"""Real-time order/trade (execution) event push over Redis.

Big QMT fires ``order_callback(ContextInfo, orderInfo)`` and
``deal_callback(ContextInfo, dealInfo)`` inside the strategy process. We normalize
the QMT order/deal object (ThinkTrader ``m_*`` fields) into a plain dict and
publish it to a Redis channel, so clients receive ``on_stock_order`` /
``on_stock_trade`` callbacks in real time (MiniQMT style) instead of polling.

Channels (also used as capped streams for short replay, xadd + publish):
- ``bigqmt:order_events:{account_id}``
- ``bigqmt:trade_events:{account_id}``

The normalized field names match ``BigQmtXtTrader._order_from_dict`` /
``_trade_from_dict`` so the client can shape them straight into MiniQMT objects.
"""

import json
import time


ORDER_CHANNEL_TEMPLATE = "bigqmt:order_events:{account_id}"
TRADE_CHANNEL_TEMPLATE = "bigqmt:trade_events:{account_id}"

EVENT_ORDER = "order"
EVENT_TRADE = "trade"

# ThinkTrader enum_EEntrustBS (买卖方向, the m_nDirection field), universal across
# 股票/期货/期权. Ref: https://dict.thinktrader.net/innerApi/enum_constants.html
ENTRUST_BUY = 48         # 买入 / 多
ENTRUST_SELL = 49        # 卖出 / 空
ENTRUST_PLEDGE_IN = 81   # 质押入库
ENTRUST_PLEDGE_OUT = 66  # 质押出库

# enum_EEntrustBS (买卖方向, the m_nDirection field), per QMT enum docs.
# 48=买, 49=卖.  Universal across 股票/期货/期权.
#
# Real-world findings from live COrderDetail/CDealDetail callbacks
# (diagnosed via exec_events_debug_raw_fields=True, 2026-07-29):
#   QMT returns m_nDirection=48 **unconditionally** — even for sell orders.
#   m_nOffsetFlag correctly reflects direction (48=买, 49=卖 for stocks).
#   m_nOpType correctly reflects direction (23=买, 24=卖) on orders.
#   query_orders uses m_nOffsetFlag and works correctly in production.
#
# Therefore _extract_direction uses an arbitration chain:
#   Preferred: m_nOffsetFlag (most reliable in live callbacks, matches query_orders)
#   Fallback:  m_nDirection (traditional EEntrustBS; can be stuck at 48 in calls)
#   Arbiter:   when direction≠offset (futures: sell+open=49+48),
#              consult m_nOpType (23/24) to resolve the conflict; for trades
#              (no m_nOpType) trust m_nOffsetFlag (QMT docs confirm stock
#              direction=offset).
#   Last:      order_type (MiniQMT STOCK_BUY=23 / STOCK_SELL=24) and plain text
# Unknown -> "" (the raw value is always preserved so callers can refine).
OFFSET_OPEN = 48
OFFSET_CLOSE = 49
OFFSET_CLOSE_TODAY = 51
OFFSET_CLOSE_YESTERDAY = 52

_BUY_DIRECTIONS = {ENTRUST_BUY, str(ENTRUST_BUY), OFFSET_OPEN, str(OFFSET_OPEN), 23, "23", "BUY", "buy", "B"}
_SELL_DIRECTIONS = {ENTRUST_SELL, str(ENTRUST_SELL), OFFSET_CLOSE, str(OFFSET_CLOSE), OFFSET_CLOSE_TODAY, str(OFFSET_CLOSE_TODAY), OFFSET_CLOSE_YESTERDAY, str(OFFSET_CLOSE_YESTERDAY), 24, "24", "SELL", "sell", "S"}


def order_channel(account_id):
    return ORDER_CHANNEL_TEMPLATE.format(account_id=str(account_id or ""))


def trade_channel(account_id):
    return TRADE_CHANNEL_TEMPLATE.format(account_id=str(account_id or ""))


def _attr(obj, names, default=None):
    for name in names:
        if isinstance(obj, dict):
            if name in obj and obj[name] is not None:
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return default


def _qmt_datetime(date_val, time_val):
    """Combine a QMT ``YYYYMMDD`` date and ``HHMMSS`` time into ``YYYY-MM-DD HH:MM:SS``.

    QMT order callbacks carry ``m_strInsertDate`` / ``m_strInsertTime`` and deal
    callbacks carry ``m_strTradeDate`` / ``m_strTradeTime`` as separate all-digit
    strings (e.g. ``"20240222"`` + ``"091259"``). We keep the QMT-recorded date rather
    than re-deriving it downstream (which would wrongly assume "today" for overnight or
    post-close backfill). Returns ``""`` when either part is missing or malformed.
    """
    date_text = str(date_val or "").strip()
    time_text = str(time_val or "").strip()
    if not date_text or not time_text:
        return ""
    if not (date_text.isdigit() and len(date_text) == 8):
        return ""
    if not time_text.isdigit():
        return ""
    time_text = time_text.zfill(6)
    if len(time_text) != 6:
        return ""
    return "%s-%s-%s %s:%s:%s" % (
        date_text[0:4],
        date_text[4:6],
        date_text[6:8],
        time_text[0:2],
        time_text[2:4],
        time_text[4:6],
    )


def _stock_code_with_exchange(obj):
    code = str(
        _attr(obj, ["m_strInstrumentID", "stock_code", "m_strInstrument"], "") or "",
    ).strip().upper()
    if not code:
        raise ValueError("QMT execution event is missing instrument ID")
    if "." in code:
        return code

    exchange = str(
        _attr(obj, ["m_strExchangeID", "exchange_id", "market"], "") or "",
    ).strip().upper()
    if exchange not in {"SH", "SZ", "BJ"}:
        raise ValueError(
            "QMT execution event has no supported exchange ID for instrument %s: %r"
            % (code, exchange),
        )
    return "%s.%s" % (code, exchange)


def _action_from_direction(direction):
    if direction in _BUY_DIRECTIONS:
        return "BUY"
    if direction in _SELL_DIRECTIONS:
        return "SELL"
    return ""


def _is_buy(val):
    v = int(val)
    return v in _BUY_DIRECTIONS


def _is_sell(val):
    v = int(val)
    return v in _SELL_DIRECTIONS


def _conflict_resolve(d_val, o_val, obj):
    """When m_nDirection and m_nOffsetFlag disagree, arbitrate via m_nOpType.

    Live diagnosis confirms:
      - Stock sell: direction=48(buy), offset=49(sell), op_type=24(sell) → sell
      - Futures sell+open: direction=49(sell), offset=48(open), op_type=24(sell) → sell
      - Futures buy+close: direction=48(buy), offset=49(close), op_type=23(buy) → buy

    Returns a resolved value, or None if no arbiter can decide.
    """
    op = _attr(obj, ["m_nOpType", "op_type", "order_type"])
    if op is not None:
        try:
            op_int = int(op)
            if op_int in _BUY_DIRECTIONS:
                return d_val if _is_buy(d_val) else o_val if _is_buy(o_val) else op
            if op_int in _SELL_DIRECTIONS:
                return d_val if _is_sell(d_val) else o_val if _is_sell(o_val) else op
        except (TypeError, ValueError):
            if op in _BUY_DIRECTIONS:
                return d_val if _is_buy(d_val) else o_val if _is_buy(o_val) else op
            if op in _SELL_DIRECTIONS:
                return d_val if _is_sell(d_val) else o_val if _is_sell(o_val) else op
    # no arbiter — trust offset (QMT docs confirm stock direction=offset)
    return o_val


def _extract_direction(obj):
    """Extract buy/sell direction, matching query_orders' reliable logic.

    Priority chain (documented with live-diagnosis justification):
      1. m_nOffsetFlag         — most reliable in live callbacks (matches query_orders)
      2. m_nDirection           — traditional EEntrustBS (can be stuck at 48)
      3. Arbitration: when direction≠offset, consult m_nOpType (orders: 23/24)
         to resolve correctly for both stocks AND futures.
      4. m_nOpType / order_type — last resort fallback.

    The raw value is always returned (even pledge=81) so callers can inspect it;
    _action_from_direction maps only known buy/sell values, leaving others "".

    References
    ----------
    - Live diagnosis 2026-07-29 (COrderDetail/CDealDetail):
      m_nDirection=48 unconditionally, m_nOffsetFlag=48(buy)/49(sell) correct,
      m_nOpType=23(buy)/24(sell) correct (orders only).
    - QMT enum docs: enum_EEntrustBS (48=买,49=卖), enum_EOffset_Flag_Type
      (48=开仓,49=平仓). For stocks direction=offset; for futures they differ.
    - query_orders uses m_nOffsetFlag and works correctly in production.
    """
    offset = _attr(obj, ["m_nOffsetFlag", "offset_flag"])
    direction = _attr(obj, ["m_nDirection", "direction"])

    # 1. offset alone — use it directly (matches query_orders)
    if offset is not None and direction is None:
        try:
            o = int(offset)
            if o in _BUY_DIRECTIONS or o in _SELL_DIRECTIONS:
                return offset
        except (TypeError, ValueError):
            if offset in _BUY_DIRECTIONS or offset in _SELL_DIRECTIONS:
                return offset

    # 2. direction alone — use it
    if direction is not None and offset is None:
        try:
            d = int(direction)
            if d in _BUY_DIRECTIONS or d in _SELL_DIRECTIONS:
                return direction
            if d != 0:
                return direction
        except (TypeError, ValueError):
            if direction in _BUY_DIRECTIONS or direction in _SELL_DIRECTIONS:
                return direction
            return direction

    # 3. both present
    if direction is not None and offset is not None:
        try:
            d = int(direction)
            o = int(offset)
            d_valid = (d in _BUY_DIRECTIONS or d in _SELL_DIRECTIONS)
            o_valid = (o in _BUY_DIRECTIONS or o in _SELL_DIRECTIONS)

            if d_valid and o_valid:
                if d == o:
                    return direction  # agree → use either
                # disagree → arbitrate via m_nOpType
                return _conflict_resolve(d, o, obj)

            if d_valid and not o_valid:
                return direction
            if o_valid and not d_valid:
                return offset
            # neither valid — fall through
        except (TypeError, ValueError):
            pass

    # 4. last resort: m_nOpType / order_type
    return _attr(obj, ["m_nOpType", "op_type", "order_type"])


# Fields we care about when diagnosing a direction misread. Anything starting
# with "m_" is captured automatically; these are the MiniQMT-style names that
# do not match that prefix.
_RAW_SNAPSHOT_EXTRA_FIELDS = (
    "stock_code",
    "order_type",
    "op_type",
    "direction",
    "offset_flag",
    "order_status",
    "order_volume",
    "traded_volume",
    "price",
    "order_id",
    "order_sysid",
    "order_sys_id",
    "trade_id",
    "traded_id",
    "strategy_name",
    "order_remark",
)


def raw_field_snapshot(obj, max_repr=120):
    """Capture every readable field of a live QMT callback object.

    Direction extraction relies on understanding what ``m_nDirection``,
    ``m_nOffsetFlag`` and ``m_nOpType`` carry in live callbacks. This dumps
    every readable field so one live order settles the question.

    Returns ``{name: "<type> <value>"}``. Never raises: a callback that dies
    while being diagnosed would be worse than no diagnosis.
    """
    snapshot = {}
    try:
        if isinstance(obj, dict):
            names = list(obj.keys())
        else:
            names = [name for name in dir(obj) if name.startswith("m_")]
            names.extend(_RAW_SNAPSHOT_EXTRA_FIELDS)
    except Exception:
        return {"__error__": "dir() failed"}
    seen = set()
    for name in names:
        key = str(name)
        if key in seen or key.startswith("__"):
            continue
        seen.add(key)
        try:
            if isinstance(obj, dict):
                if key not in obj:
                    continue
                value = obj[key]
            else:
                if not hasattr(obj, key):
                    continue
                value = getattr(obj, key)
            if callable(value):
                continue
            text = repr(value)
            if len(text) > max_repr:
                text = text[:max_repr] + "..."
            snapshot[key] = "%s %s" % (type(value).__name__, text)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not break callbacks
            snapshot[key] = "<unreadable: %s>" % exc.__class__.__name__
    return snapshot


def format_raw_snapshot(kind, obj):
    """One-line, GBK-safe rendering of :func:`raw_field_snapshot` for the QMT panel."""
    snapshot = raw_field_snapshot(obj)
    parts = ["%s=%s" % (name, snapshot[name]) for name in sorted(snapshot)]
    return "[bigqmt_exec_raw] %s type=%s %s" % (
        kind,
        type(obj).__name__,
        " | ".join(parts) or "<no fields>",
    )


def normalize_order_event(order, account_id=""):
    """Build a JSON-able order event dict from a Big QMT orderInfo object."""
    direction = _extract_direction(order)
    remark = str(
        _attr(
            order,
            ["m_strRemark", "m_strOrderRemark", "user_order_id", "order_remark", "remark"],
            "",
        )
        or "",
    )
    return {
        "event_type": EVENT_ORDER,
        "account_id": str(_attr(order, ["m_strAccountID", "account_id"], account_id) or account_id or ""),
        "stock_code": _stock_code_with_exchange(order),
        "order_sys_id": str(_attr(order, ["m_strOrderSysID", "order_sys_id", "order_sysid", "order_id"], "") or ""),
        "order_volume": _attr(order, ["m_nVolumeTotal", "order_volume", "volume"]),
        "traded_volume": _attr(order, ["m_nVolumeTraded", "traded_volume"]),
        "price": _attr(order, ["m_dLimitPrice", "price", "limit_price"]),
        "status": _attr(order, ["m_nOrderStatus", "order_status", "status"]),
        "direction": direction,
        "action": _action_from_direction(direction),
        "offset_flag": _attr(order, ["m_nOffsetFlag", "offset_flag"]),
        "strategy_name": str(_attr(order, ["m_strOptName", "strategy_name", "order_remark", "remark"], "") or ""),
        "remark": remark,
        "user_order_id": remark,
        # QMT-recorded委托时间 (m_strInsertDate + m_strInsertTime), so downstream can
        # persist the venue's order time rather than the local receive clock.
        "order_at": _qmt_datetime(
            _attr(order, ["m_strInsertDate", "insert_date", "order_date"], ""),
            _attr(order, ["m_strInsertTime", "insert_time", "order_time"], ""),
        ),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at_ts": time.time(),
    }


def normalize_trade_event(trade, account_id=""):
    """Build a JSON-able trade (成交) event dict from a Big QMT dealInfo object."""
    direction = _extract_direction(trade)
    remark = str(
        _attr(
            trade,
            ["m_strRemark", "m_strOrderRemark", "user_order_id", "order_remark", "remark"],
            "",
        )
        or "",
    )
    return {
        "event_type": EVENT_TRADE,
        "account_id": str(_attr(trade, ["m_strAccountID", "account_id"], account_id) or account_id or ""),
        "stock_code": _stock_code_with_exchange(trade),
        "order_sys_id": str(_attr(trade, ["m_strOrderSysID", "order_sys_id", "order_sysid", "order_id"], "") or ""),
        "trade_id": str(_attr(trade, ["m_strTradeID", "trade_id"], "") or ""),
        "volume": _attr(trade, ["m_nVolume", "volume", "traded_volume"]),
        "price": _attr(trade, ["m_dPrice", "price", "traded_price"]),
        "amount": _attr(trade, ["m_dTradeAmount", "amount"]),
        "commission": _attr(trade, ["m_dComssion", "m_dCommission", "commission"]),
        "direction": direction,
        "action": _action_from_direction(direction),
        "offset_flag": _attr(trade, ["m_nOffsetFlag", "offset_flag"]),
        "remark": remark,
        "user_order_id": remark,
        # QMT-recorded成交时间. Prefer the full date+time (m_strTradeDate +
        # m_strTradeTime); fall back to a pre-normalized traded_at/trade_time string
        # (which may be time-only) for backward compatibility.
        "traded_at": _qmt_datetime(
            _attr(trade, ["m_strTradeDate", "trade_date", "traded_date"], ""),
            _attr(trade, ["m_strTradeTime", "trade_time"], ""),
        )
        or str(_attr(trade, ["traded_at", "trade_time", "m_strTradeTime"], "") or ""),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at_ts": time.time(),
    }


def _publish(redis_client, channel, event, maxlen=2000):
    raw = json.dumps(event, ensure_ascii=False, default=str)
    try:
        redis_client.xadd(channel, {"payload": raw}, maxlen=maxlen, approximate=True)
    except Exception:
        pass
    redis_client.publish(channel, raw)
    return event


def publish_order_event(redis_client, account_id, event):
    return _publish(redis_client, order_channel(account_id), event)


def publish_trade_event(redis_client, account_id, event):
    return _publish(redis_client, trade_channel(account_id), event)
