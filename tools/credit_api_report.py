# coding: utf-8
"""两融（信用账户）API 只读体检报告。

**这个工具不下单、不撤单、不改任何东西。** 只调查询接口。

维护者本人没有两融账户，两融那一整片只能靠有两融账户的用户跑一次回报。所以
这个脚本把每个两融接口都真调一遍，记下大 QMT 实际返回了什么 —— 行数、字段名、
字段是不是真有值 —— 然后导出一份报告文件。把报告贴到 issue 里，就能直接看出
是哪个接口出问题，不用来回猜。

用法::

    python tools/credit_api_report.py

配置和 test_all_apis.py 一样，从 bigqmt_signal_trader_local_config 或环境变量
读，不用改脚本。可选参数::

    --account 8886800503     指定账号（默认用配置里的）
    --out DIR                报告写到哪个目录（默认当前目录）
    --full                   报告里带上原始数值。**默认不带** —— 报告是要贴到
                             公开 issue 上的，默认只记「这个字段有没有值」，
                             账号号码也打码。确认过内容再用 --full。
    --wait 3                 查柜台那条路等回调的秒数（默认 3）

产出两个文件：``credit_api_report_<时间戳>.json``（结构化，给维护者看）和
``.txt``（人能读的摘要，贴 issue 用这个）。
"""
from __future__ import print_function

import argparse
import datetime as _dt
import json
import os
import sys
import time
import traceback


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))
# 运行一个脚本时 sys.path[0] 是**脚本所在目录**，不是当前目录 —— 所以
# 「在放着配置的目录下运行」这句话，不把 cwd 加进来是不成立的。放在最前面，
# 别让 QMT 目录里那份真配置抢在用户自己那份前面（CLAUDE.md 记过这个坑）。
_CWD = os.getcwd()
if _CWD not in sys.path:
    sys.path.insert(0, _CWD)

# 本进程自己一个日志文件，别去抢部署那份的句柄（Windows 上两个句柄会让日志轮转
# 永远失败，见 CLAUDE.md / #144）。
os.environ.setdefault("BIGQMT_LOG_NAME", "bigqmt-credit-report")


# ---------------------------------------------------------------------------
# 要体检的接口清单
# ---------------------------------------------------------------------------
# (RPC 方法名, 中文名, 这条 RPC 在大 QMT 那边最终调的东西, 额外参数)
CREDIT_ACCOUNT_CHECKS = [
    ("query_credit_detail", "信用账户明细（终端缓存）",
     "get_trade_detail_data(accId, 'CREDIT', 'ACCOUNT') -> CCreditAccountDetail 3.14", {}),
    ("query_credit_account", "信用账户明细（查柜台，异步回调）",
     "query_credit_account(accId, seq, ContextInfo) + credit_account_callback -> CCreditDetail 3.15", {}),
]

CREDIT_CONTRACT_CHECKS = [
    ("query_stk_compacts", "未了结负债合约",
     "get_unclosed_compacts(accId, accType) -> StkCompacts 3.17", {}),
    ("get_unclosed_compacts", "未了结负债合约（直连全局函数）",
     "get_unclosed_compacts(accId, accType)", {}),
    ("get_closed_compacts", "已了结负债合约（直连全局函数）",
     "get_closed_compacts(accId, accType)", {}),
    ("query_credit_subjects", "担保标的明细",
     "get_assure_contract(accId) -> StkSubjects 3.18", {}),
    ("query_credit_assure", "担保品合约（同 query_credit_subjects）",
     "get_assure_contract(accId)", {}),
    ("get_assure_contract", "担保标的明细（直连全局函数）",
     "get_assure_contract(accId)", {}),
    ("query_credit_slo_code", "可融券明细",
     "get_enable_short_contract(accId) -> CreditSloEnableAmount 3.16", {}),
    ("get_enable_short_contract", "可融券明细（直连全局函数）",
     "get_enable_short_contract(accId)", {}),
    ("get_debt_contract", "负债合约【官方已弃用 6.17】",
     "get_debt_contract(accId)", {}),
]

# #204 一并推回主线程的其余交易类全局函数。不是两融，但和上面那批是同一个
# 修复：它们原来都跑在后台 listener 线程上，而 QMT 交易类查询在主策略线程之外
# 返回「行数对、字段全空」的对象。放进报告，一次跑完能把 #204/#205 全测到。
OTHER_TRADE_GLOBAL_CHECKS = [
    ("get_option_subject_position", "期权标的持仓",
     "get_option_subject_position(accId)", {}),
    ("get_comb_option", "组合期权持仓", "get_comb_option(accId)", {}),
    ("get_new_purchase_limit", "新股申购额度", "get_new_purchase_limit(accId)", {}),
    ("get_ipo_data", "新股数据（早就 defer 的，做对照）", "get_ipo_data(type)", {}),
    # #205：官方签名是 (accountID, accountType)，原来一个参数都没传，
    # _call_qmt_global 把 TypeError 吞掉返回空 —— 从客户端看和「没开港股通」一样。
    ("get_hkt_exchange_rate", "港股通汇率【#205 修复的】",
     "get_hkt_exchange_rate(accId, accountType)", {}),
]

# 对照组：不是两融接口，用来确认桥本身是活的。如果这几个也空，那问题不在两融。
CONTROL_CHECKS = [
    ("ping", "存活/版本", "-", {}),
    ("query_account_infos", "账户信息（按部署配置的账户类型）",
     "get_trade_detail_data(accId, <配置类型>, 'ACCOUNT')", {}),
    ("get_asset", "资产", "get_trade_detail_data(...ACCOUNT)", {}),
    # get_positions 的键就是股票代码 —— 键名本身是持仓数据，报告里要打掉。
    ("get_positions", "持仓", "get_trade_detail_data(...POSITION)", {}, True),
    ("query_orders", "当日委托", "get_trade_detail_data(...ORDER)", {}),
]

# 信用委托类型（xtconstant）。**只做静态核对，不下单。**
CREDIT_ORDER_TYPES = [
    ("CREDIT_BUY", 23, "担保品买入"),
    ("CREDIT_SELL", 24, "担保品卖出"),
    ("CREDIT_FIN_BUY", 27, "融资买入"),
    ("CREDIT_SLO_SELL", 28, "融券卖出"),
    ("CREDIT_BUY_SECU_REPAY", 29, "买券还券"),
    ("CREDIT_DIRECT_SECU_REPAY", 30, "直接还券"),
    ("CREDIT_SELL_SECU_REPAY", 31, "卖券还款"),
    ("CREDIT_DIRECT_CASH_REPAY", 32, "直接还款"),
    ("CREDIT_FIN_BUY_SPECIAL", 40, "专项融资买入"),
    ("CREDIT_SLO_SELL_SPECIAL", 41, "专项融券卖出"),
    ("CREDIT_BUY_SECU_REPAY_SPECIAL", 42, "专项买券还券"),
    ("CREDIT_DIRECT_SECU_REPAY_SPECIAL", 43, "专项直接还券"),
    ("CREDIT_SELL_SECU_REPAY_SPECIAL", 44, "专项卖券还款"),
    ("CREDIT_DIRECT_CASH_REPAY_SPECIAL", 45, "专项直接还款"),
]

# 信用账户明细里，判断「这份数据到底有没有内容」要看的关键字段。
# 缓存那份是 CCreditAccountDetail(3.14)，柜台那份是 CCreditDetail(3.15)，
# 字段名不完全一样 —— 尤其 m_dTotalDebit / m_dTotalDebt 只差一个字母。
KEY_CREDIT_FIELDS = (
    "m_dPerAssurescaleValue",   # 维持担保比例，两份都有
    "m_dTotalDebt",             # 总负债（柜台那份）
    "m_dTotalDebit",            # 总负债（缓存那份）
    "m_dFinMaxQuota", "m_dFinEnableQuota", "m_dFinUsedQuota",
    "m_dSloMaxQuota", "m_dSloEnableQuota", "m_dSloUsedQuota",
    "m_dEnableBailBalance",
    "m_dFinDebt", "m_dSloDebt",
    "m_dAssureAsset", "m_dBalance", "m_dAvailable",
)


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------

def mask_account(account_id):
    text = str(account_id or "")
    if len(text) <= 4:
        return "*" * len(text)
    return text[:2] + "*" * (len(text) - 4) + text[-2:]


# 这几个字段永远记原值。它们不是余额也不是持仓，是「这本账户是什么账户」的
# 判据 —— 打了码报告就没法用了。m_nBrokerType: 2=普通股票 3=信用。
ALWAYS_RAW_FIELDS = ("m_nBrokerType", "m_nDirection", "account_type")


def describe_value(value, full):
    """报告里怎么记一个字段值。

    默认不记原值 —— 这份报告是要贴到公开 issue 上的，账户余额不该跟着走。
    诊断真正需要的只是「这个字段回没回、是不是全是 0」。
    """
    if full:
        return value
    if value is None:
        return "<null>"
    if isinstance(value, bool):
        return "bool:%s" % value
    if isinstance(value, (int, float)):
        return "zero" if value == 0 else "non-zero"
    if isinstance(value, str):
        if not value:
            return "empty-str"
        return "str(len=%d)" % len(value)
    if isinstance(value, (list, tuple)):
        return "list(len=%d)" % len(value)
    if isinstance(value, dict):
        return "dict(keys=%d)" % len(value)
    return type(value).__name__


# ---------------------------------------------------------------------------
# 结果归纳
# ---------------------------------------------------------------------------

def summarize_rows(rows, full, keys_are_data=False):
    """把一批返回行归纳成「字段名 + 每个字段有没有值」。

    *keys_are_data* 用于 get_positions 这类「键就是数据」的返回（键是股票代码）。
    这种字段名不能进报告 —— 报告是要贴到公开 issue 上的，那等于把持仓贴出去。
    """
    out = {"row_count": len(rows)}
    if not rows:
        return out
    first = rows[0]
    if not isinstance(first, dict):
        out["row_type"] = type(first).__name__
        out["sample"] = describe_value(first, full)
        return out
    fields = sorted(first.keys())
    out["field_count"] = len(fields)
    if keys_are_data and not full:
        out["fields_redacted"] = True
        out["fields"] = []
        return out
    out["fields"] = fields
    out["values"] = dict(
        (k, first.get(k) if k in ALWAYS_RAW_FIELDS else describe_value(first.get(k), full))
        for k in fields)
    populated = [k for k in fields
                 if isinstance(first.get(k), (int, float))
                 and not isinstance(first.get(k), bool)
                 and first.get(k) != 0]
    out["non_zero_numeric_fields"] = populated
    # 行数不等于有数据。QMT 交易类查询跑在主策略线程之外时，返回的是**行数对、
    # 字段全空**的对象（实测：get_asset 移出 defer 名单后照样回 5 个键，值全是
    # None）。只看行数会把这种情况判成「有数据」—— 那正是这份报告最该拦住的
    # 误导（#204）。0 是合法值（没有负债就是 0），只有 None/"" 才算没拿到。
    present = [k for k in fields
               if first.get(k) is not None and first.get(k) != ""]
    out["populated_fields"] = len(present)
    # 有字段、但每个字段都没值 —— 这才是跑错线程的签名。零字段是没数据。
    out["hollow"] = bool(fields) and not present
    out["all_numeric_zero"] = (not populated) and any(
        isinstance(first.get(k), (int, float)) and not isinstance(first.get(k), bool)
        for k in fields)
    present_key = [k for k in KEY_CREDIT_FIELDS if k in first]
    if present_key:
        out["key_credit_fields_present"] = present_key
        out["key_credit_fields_non_zero"] = [
            k for k in present_key
            if isinstance(first.get(k), (int, float)) and first.get(k) != 0]
    return out


def normalize_result(result):
    """各个接口返回形状不一：list / dict / {rows: [...]}。统一成 (rows, extra)。"""
    if result is None:
        return [], {}
    if isinstance(result, dict):
        if "rows" in result and isinstance(result["rows"], list):
            extra = dict((k, v) for k, v in result.items() if k != "rows")
            return result["rows"], extra
        # 空 dict 是「没数据」，不是「一行空数据」。当成一行的话，下面的
        # hollow 判据（字段全空）会对它成立 —— 实跑第一次就把已经 defer 的
        # get_ipo_data 误报成跑错线程了。
        return ([result] if result else []), {}
    if isinstance(result, (list, tuple)):
        return list(result), {}
    return [result], {}


# ---------------------------------------------------------------------------
# 跑一个接口
# ---------------------------------------------------------------------------

def run_check(call, method, label, backend, params, full, keys_are_data=False):
    entry = {
        "method": method,
        "label": label,
        "qmt_backend": backend,
        "params": dict(params or {}),
    }
    started = time.time()
    try:
        result = call(method, dict(params or {}))
    except Exception as exc:
        entry["ok"] = False
        entry["error"] = "%s: %s" % (exc.__class__.__name__, exc)
        entry["elapsed_ms"] = round((time.time() - started) * 1000, 1)
        return entry
    entry["ok"] = True
    entry["elapsed_ms"] = round((time.time() - started) * 1000, 1)
    rows, extra = normalize_result(result)
    entry.update(summarize_rows(rows, full, keys_are_data))
    if extra:
        # query_credit_account 的 fresh / stale / query_issued / error 等都在这里,
        # 这些正是判断「柜台那条路到底通没通」的依据，一定要原样记下来。
        entry["envelope"] = extra
    return entry


def verdict_for(entry):
    """一句话结论，给人看的那份报告用。"""
    if not entry.get("ok"):
        return "报错", entry.get("error", "")
    envelope = entry.get("envelope") or {}
    if entry.get("row_count"):
        if entry.get("hollow"):
            return "有行但字段全空", ("行数对、字段名齐全、值全是 None —— QMT 交易类"
                                     "查询跑在主策略线程之外就是这样，看 thread_routing")
        if entry.get("all_numeric_zero"):
            return "有行但数值全为 0", "字段回来了，值都是 0 —— 可能是没数据，也可能是没读到"
        key_nz = entry.get("key_credit_fields_non_zero")
        if key_nz:
            return "有数据", "关键字段有值：%s" % ", ".join(key_nz[:4])
        return "有数据", "%d 行 / %d 字段" % (entry.get("row_count", 0),
                                               entry.get("field_count", 0))
    if envelope.get("callback_bound") is False:
        return "接口没绑上", envelope.get("not_issued_reason", "")
    if envelope.get("query_issued") is False and envelope.get("not_issued_reason"):
        return "没发出查询", envelope["not_issued_reason"]
    if envelope.get("query_issued") and not envelope.get("fresh"):
        return "发了但没等到回调", "柜台没回，或回调没接上"
    return "空", ""


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def render_text(report):
    lines = []
    add = lines.append
    add("=" * 72)
    add("两融（信用账户）API 只读体检报告")
    add("=" * 72)
    meta = report["meta"]
    add("生成时间   : %s" % meta["generated_at"])
    add("账号       : %s (已打码)" % meta["account_masked"])
    add("客户端版本 : %s" % meta["client_version"])
    add("桥端版本   : %s" % meta["bridge_version"])
    add("原始数值   : %s" % ("已包含 (--full)" if meta["full_values"]
                             else "已省略（默认，可安全贴到 issue）"))
    add("")
    add("这份报告只调查询接口，没有下过任何单。")
    add("")

    account = report.get("account_shape") or {}
    add("-" * 72)
    add("这个账户是不是信用账户")
    add("-" * 72)
    add("  部署配置的账户类型 : %s" % account.get("configured_account_type", "?"))
    add("  m_nBrokerType      : %s   (2=普通股票  3=信用)" % account.get("broker_type"))
    add("  判定               : %s" % account.get("verdict", "?"))
    add("")

    for title, key in (("信用账户明细（这次报告的重点）", "credit_account"),
                       ("负债合约 / 标的", "credit_contracts"),
                       ("其余交易类全局函数（#204 一并推回主线程 / #205）",
                        "other_trade_globals"),
                       ("对照组（非两融，用来确认桥本身是活的）", "control")):
        add("-" * 72)
        add(title)
        add("-" * 72)
        for entry in report["checks"].get(key, []):
            state, detail = verdict_for(entry)
            add("  %-28s %-22s %7.1fms" % (entry["method"], state,
                                           entry.get("elapsed_ms", 0)))
            add("      %s" % entry["label"])
            add("      QMT: %s" % entry["qmt_backend"])
            if detail:
                add("      -> %s" % detail)
            if entry.get("fields"):
                shown = entry["fields"][:14]
                add("      字段(%d): %s%s" % (entry.get("field_count", 0),
                                              ", ".join(shown),
                                              " ..." if len(entry["fields"]) > 14 else ""))
            if entry.get("envelope"):
                add("      信封: %s" % json.dumps(entry["envelope"],
                                                  ensure_ascii=False, default=str)[:300])
            add("")
        add("")

    add("-" * 72)
    add("桥端能力探测 (probe_capabilities)")
    add("-" * 72)
    probe = report.get("probe") or {}
    if probe.get("error"):
        add("  探测失败: %s" % probe["error"])
    else:
        for name, value in sorted((probe.get("credit_probe") or {}).items()):
            add("  %-40s %s" % (name, json.dumps(value, ensure_ascii=False)[:200]))
        globals_map = probe.get("qmt_globals") or {}
        missing = sorted(k for k, v in globals_map.items() if not v)
        if missing:
            add("")
            add("  这台终端没有绑上的全局函数: %s" % ", ".join(missing))
    add("")

    callback = (report.get("probe") or {}).get("credit_callback") or {}
    if callback:
        add("-" * 72)
        add("信用账户回调（查柜台那条路）")
        add("-" * 72)
        add("  note_credit_account 可用 : %s" % callback.get("note_credit_account_available"))
        add("  QMT 实际回调次数         : %s   (0 = QMT 压根没回调)"
            % callback.get("callbacks_seen"))
        add("  桥里缓存的行数           : %s" % callback.get("cached_rows"))
        add("")

    routing = (report.get("probe") or {}).get("thread_routing") or {}
    if routing.get("available"):
        add("-" * 72)
        add("线程路由（QMT 交易类查询在主策略线程之外返回空行/空字段）")
        add("-" * 72)
        add("  process_in_listener : %s" % routing.get("process_in_listener"))
        add("  listener 方法数     : %s" % routing.get("listener_method_count"))
        for name, where in sorted((routing.get("sample") or {}).items()):
            add("  %-28s %s" % (name, where))
        add("")

    add("-" * 72)
    add("信用委托类型常量（静态核对，没有下过单）")
    add("-" * 72)
    for item in report.get("order_types", []):
        flag = "ok" if item["matches"] else ("缺失" if item["actual"] is None else "不一致")
        add("  %-36s 期望 %-3s 实际 %-6s %-6s %s"
            % (item["name"], item["expected"], item["actual"], flag, item["meaning"]))
    add("")

    add("=" * 72)
    add("结论")
    add("=" * 72)
    for line in report.get("conclusions", []):
        add("  - %s" % line)
    add("")
    add("请把这个文件（以及同名的 .json）贴到 issue 里。")
    return "\n".join(lines)


def build_conclusions(report):
    out = []
    account = report.get("account_shape") or {}
    if account.get("broker_type") == 3:
        out.append("这是信用账户（m_nBrokerType=3），两融接口的结果可以采信。")
    elif account.get("broker_type") is not None:
        out.append("这**不是**信用账户（m_nBrokerType=%s）。两融接口返回空是正常的，"
                   "这份报告说明不了两融接口有没有问题。" % account.get("broker_type"))
    else:
        out.append("读不到 m_nBrokerType，无法判断账户类型 —— 先看对照组是不是也空。")

    by_method = {}
    for group in report["checks"].values():
        for entry in group:
            by_method[entry["method"]] = entry

    control_alive = any(by_method.get(m, {}).get("row_count")
                        for m in ("query_account_infos", "get_asset", "ping"))
    if not control_alive:
        out.append("对照组也没数据 —— 问题多半不在两融接口，先查桥/账户/终端本身。")

    # query_credit_account 报「没绑上」有两种成因，差别很大：这台终端根本没有
    # 这个全局函数（那就无解，只能走同步那条），还是有但没重启策略（重启就好）。
    # probe 的 global_namespace 走的是策略侧 _resolve_runtime_name 同一条解析
    # 路径（qmt_api -> globals -> builtins），所以它能分开这两种。
    counter_entry = by_method.get("query_credit_account", {})
    counter_env = counter_entry.get("envelope") or {}
    if counter_env.get("query_issued") and not counter_env.get("fresh"):
        seen = counter_env.get("callbacks_seen")
        waited = counter_env.get("waited_seconds")
        if seen == 0:
            out.append("query_credit_account 发出去了，但 QMT 一次回调都没给"
                       "（callbacks_seen=0，等了 %s 秒）。可能是等太短、这个账户"
                       "柜台不答，或者 credit_account_callback 没挂到被挂载的"
                       "入口文件上 —— 加大 --wait 再跑一次，仍是 0 就把这份报告"
                       "贴上来。" % waited)
        elif seen:
            out.append("query_credit_account 的回调**来过 %s 次**，但这次没等到"
                       "新结果 —— 回调链路是通的，问题在时序或限流，不在绑定。"
                       % seen)
    if (counter_entry.get("envelope") or {}).get("callback_bound") is False:
        in_namespace = ((report.get("probe") or {}).get("global_namespace")
                        or {}).get("query_credit_account")
        if in_namespace is False:
            # 曾经这里断言「这台终端没有这个函数」。那个结论是错的：真实原因是
            # 入口文件里一张手抄的函数名清单漏了它，桥自己没捕获到，而用户在
            # 大 QMT 里直接调是好用的。一个桥的 bug 被报成了券商终端的能力
            # 缺失 —— 错误但听起来很确定的结论，比不给结论更坏（#202）。
            out.append("query_credit_account 没绑上。有两种可能，从这里分不开："
                       "(a) 桥没捕获到它 —— 确认入口文件用的是 "
                       "capture_qmt_injected_funcs，并且**真的重启过策略**"
                       "（入口文件 reload_deployment() 刷不了）；"
                       "(b) 这台终端确实没有这个函数。"
                       "**先在大 QMT 里直接调一次 query_credit_account 试试**，"
                       "能调通就是 (a)。同步的 query_credit_detail 不受影响。")
        else:
            out.append("query_credit_account 在命名空间里但没绑上，多半是部署了"
                       "没**重启策略**（入口文件 reload_deployment() 刷不了）。"
                       "不影响同步那条。")

    cached = by_method.get("query_credit_detail", {})
    counter = by_method.get("query_credit_account", {})
    cached_ok = bool(cached.get("row_count"))
    counter_ok = bool(counter.get("row_count"))
    if cached_ok and counter_ok:
        out.append("信用账户明细两条路都有数据：缓存 (query_credit_detail) 和 "
                   "柜台 (query_credit_account) 都通。")
    elif cached_ok and not counter_ok:
        out.append("缓存那条通、柜台那条没数据 —— 用 query_credit_detail；"
                   "把 query_credit_account 的信封贴上来看是没绑、被限流还是没回调。")
    elif counter_ok and not cached_ok:
        out.append("柜台那条通、缓存那条空 —— 说明 CCreditAccountDetail 在这台终端"
                   "没被填上，query_credit_detail 应该回退到查柜台那条。这正是 #202 要解决的。")
    else:
        out.append("信用账户明细两条路都空。请把 probe_capabilities 那一段一起贴上来。")

    hollow = sorted(m for m, e in by_method.items() if e.get("hollow"))
    if hollow:
        out.append("这些接口返回了行、但字段全是 None：%s —— 几乎可以肯定是跑在"
                   "后台线程上了（QMT 交易类查询要主策略线程），把 thread_routing "
                   "那一段贴上来。" % ", ".join(hollow))

    broken = [m for m, e in by_method.items() if not e.get("ok")]
    if broken:
        out.append("直接报错的接口: %s" % ", ".join(sorted(broken)))
    empty = sorted(m for m, e in by_method.items()
                   if e.get("ok") and not e.get("row_count"))
    if empty:
        out.append("返回空的接口: %s" % ", ".join(empty))
    return out


# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="两融 API 只读体检报告（不下单）")
    parser.add_argument("--account", default=None, help="账号，默认用配置里的")
    parser.add_argument("--out", default=".", help="报告输出目录")
    parser.add_argument("--full", action="store_true",
                        help="报告里带上原始数值（默认省略，便于直接贴 issue）")
    parser.add_argument("--wait", type=float, default=0.0,
                        help="查柜台那条路等回调的秒数（默认 0，不等）。实盘实测回调投递在 MainThread 上，而 handler 也在 MainThread —— 等待永远等不到，还白占 adjust 主线程。结果随后落进缓存，下一次调用取到")
    args = parser.parse_args(argv)

    from bigqmt_signal_trader.xtquant_compat import XtQuantTrader
    from bigqmt_signal_trader.version import __version__ as client_version

    account_id = args.account or os.environ.get("BIGQMT_ACCOUNT_ID") or ""
    trader = XtQuantTrader(None, int(time.time()) % 100000,
                           account_id=account_id or None)
    trader.start()
    trader.connect()
    if not account_id:
        try:
            account_id = trader.client.account_id
        except Exception:
            account_id = ""

    def call(method, params):
        params = dict(params or {})
        params.setdefault("account_id", account_id)
        return trader.client.call(method, params, account_id=account_id)

    # 先确认桥是通的。不先探这一下的话，配置没读到会让下面 16 个接口每一个都
    # 报同一条「account_id is required」，报告看着像「两融全挂了」——那种报告
    # 贴上来是帮倒忙的。
    try:
        handshake = call("ping", {}) or {}
    except Exception as exc:
        print("连不上桥，体检没跑：%s: %s" % (exc.__class__.__name__, exc))
        print("")
        print("这个脚本要能找到账号和 redis/zmq 配置，方式任选其一：")
        print("  1. 在有 bigqmt_signal_trader_client_config.py 的目录下运行；")
        print("  2. 设环境变量 BIGQMT_ACCOUNT_ID（以及 redis 那几个）；")
        print("  3. 加 --account <账号> 参数。")
        print("确认 QMT 终端开着、策略在跑，再重试。")
        try:
            trader.stop()
        except Exception:
            pass
        return 2

    report = {
        "meta": {
            "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "account_masked": mask_account(account_id),
            "client_version": client_version,
            "bridge_version": "?",
            "full_values": bool(args.full),
            "read_only": True,
            "orders_placed": 0,
        },
        "checks": {"credit_account": [], "credit_contracts": [],
                   "other_trade_globals": [], "control": []},
    }

    report["meta"]["bridge_version"] = handshake.get("version", "?")
    report["meta"]["account_masked"] = mask_account(account_id)

    print("正在体检两融接口（只读，不下单）...")
    for method, label, backend, params in CREDIT_ACCOUNT_CHECKS:
        p = dict(params)
        if method == "query_credit_account":
            p["wait_seconds"] = args.wait
        print("  ->", method)
        report["checks"]["credit_account"].append(
            run_check(call, method, label, backend, p, args.full))
    for method, label, backend, params in CREDIT_CONTRACT_CHECKS:
        print("  ->", method)
        report["checks"]["credit_contracts"].append(
            run_check(call, method, label, backend, params, args.full))
    for method, label, backend, params in OTHER_TRADE_GLOBAL_CHECKS:
        print("  ->", method)
        report["checks"]["other_trade_globals"].append(
            run_check(call, method, label, backend, params, args.full))
    for check in CONTROL_CHECKS:
        method, label, backend, params = check[:4]
        keys_are_data = check[4] if len(check) > 4 else False
        print("  ->", method)
        report["checks"]["control"].append(
            run_check(call, method, label, backend, params, args.full, keys_are_data))

    # 账户形状：信用账户 m_nBrokerType=3。两条路哪条有行就从哪条读。
    # m_nBrokerType 在 ALWAYS_RAW_FIELDS 里，所以脱敏模式下也是真值。信用
    # 账户那两条优先 —— 对照组读的是部署配置的账户类型，可能根本不是信用那本。
    broker_type = None
    for entry in report["checks"]["credit_account"] + report["checks"]["control"]:
        value = (entry.get("values") or {}).get("m_nBrokerType")
        if isinstance(value, int):
            broker_type = value
            break
    probe = {}
    try:
        probe = call("probe_capabilities", {}) or {}
    except Exception as exc:
        probe = {"error": "%s: %s" % (exc.__class__.__name__, exc)}
    report["probe"] = probe
    credit_object = (probe.get("credit_probe") or {}).get(
        "get_trade_detail_data(CREDIT,ACCOUNT)") or {}
    if broker_type is None:
        broker_type = credit_object.get("broker_type")
    report["account_shape"] = {
        "broker_type": broker_type,
        "configured_account_type": (handshake.get("account_type")
                                    or probe.get("account_type") or "?"),
        "verdict": ("信用账户" if broker_type == 3 else
                    ("普通账户" if broker_type is not None else "判断不了")),
        "credit_object_probe": credit_object,
    }

    # 信用委托类型：只核对常量，绝不下单。
    order_types = []
    try:
        from xtquant import xtconstant
    except Exception:
        xtconstant = None
    for name, expected, meaning in CREDIT_ORDER_TYPES:
        actual = getattr(xtconstant, name, None) if xtconstant else None
        order_types.append({"name": name, "expected": expected, "actual": actual,
                            "matches": actual == expected, "meaning": meaning})
    report["order_types"] = order_types

    report["conclusions"] = build_conclusions(report)

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.abspath(args.out)
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    json_path = os.path.join(out_dir, "credit_api_report_%s.json" % stamp)
    text_path = os.path.join(out_dir, "credit_api_report_%s.txt" % stamp)
    text = render_text(report)
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
    with open(text_path, "w", encoding="utf-8") as handle:
        handle.write(text)

    print()
    print(text)
    print()
    print("报告已写入:")
    print("  ", text_path)
    print("  ", json_path)

    try:
        trader.stop()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
