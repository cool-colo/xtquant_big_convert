# coding: utf-8
"""碰交易上下文的 handler 必须走 adjust 主线程 —— 机械校验，不靠人记得。

`get_trade_detail_data` 在主策略线程之外返回空，是这个项目最重要的一条约束。
但 `LISTENER_DEFERRED_METHODS` 一直是**手工维护**的集合，没有任何东西检查
「这个 handler 碰了交易上下文，它在不在名单里」。#204 就是这么漏的：
`get_ipo_data` 当年因为「后台线程返回空」被单独修好并 defer 了，同一批的
9 个孪生方法却一个都没跟上，在线上静默返回空好几个月。

而且它的表现比「返回空」更阴：实测把 `get_asset` 移出 defer 名单，它返回的
不是空列表，而是**行数对、字段全是 None** 的对象 —— 从客户端看像「这个账户
没钱」，不像调用失败。

所以这里用 AST 扫出所有「碰交易上下文」的 handler（含经私有方法的传递闭包），
挨个检查是不是在 defer 名单里。漏一个就红。

豁免必须写在下面的 DELIBERATELY_INLINE 里并说明理由 —— 让"不 defer"成为一个
需要解释的决定，而不是忘了。
"""
import ast
import io
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import (  # noqa: E402
    LISTENER_DEFERRED_METHODS,
    ORDER_METHODS,
)

RPC_SOURCE = os.path.join(ROOT, "src", "bigqmt_signal_trader", "redis_rpc.py")

# 「碰了 QMT 交易上下文」的判据：调了这些取数路径中的任何一个。
TRADE_CONTEXT_CALLS = {
    "_call_qmt_global",     # QMT 注入的交易类全局函数
    "_call_qmt_mapping",
    "_call_qmt_scalar",
    "_call_qmt_object",
    "_query_trade_detail",  # get_trade_detail_data 的 6 种 detail type
    "<qmt_api.get>",        # 直接 self.qmt_api.get(name) 取来自己调
}

# 明知故犯的豁免。加一条就得给一个理由，理由要经得起问。
DELIBERATELY_INLINE = {
    "probe_capabilities":
        "诊断接口，adjust 循环卡住时更要能答 —— defer 了就会在最需要它的时候"
        "失去它。代价是它的信用探测跑在 listener 线程上、可能拿到空字段，"
        "这一点由它自己报的 hollow / thread_routing 说清楚（#204）。",
    "download_history_data":
        "下载可能跑很久，defer 到 adjust 主线程会把整个策略卡住。长任务走"
        "submit_download_history_data + wait_download 那条异步路。",
    "download_history_data2":
        "同 download_history_data。",
}


def _method_calls():
    """函数名 -> 它直接调用的属性名集合（含 self.qmt_api.get 这种取法）。"""
    tree = ast.parse(io.open(RPC_SOURCE, encoding="utf-8").read())
    calls = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        found = set()
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)):
                continue
            found.add(sub.func.attr)
            value = sub.func.value
            if (sub.func.attr == "get" and isinstance(value, ast.Attribute)
                    and value.attr == "qmt_api"):
                found.add("<qmt_api.get>")
        calls[node.name] = found
    return calls


def _touches_trade_context(name, calls, seen=None):
    """handler 可能经一层私有方法才碰到交易上下文，所以要走传递闭包。"""
    seen = set() if seen is None else seen
    if name in seen:
        return False
    seen.add(name)
    own = calls.get(name, set())
    if own & TRADE_CONTEXT_CALLS:
        return True
    return any(_touches_trade_context(callee, calls, seen)
               for callee in own if callee in calls)


def _handlers_touching_trade_context():
    calls = _method_calls()
    out = []
    for name in calls:
        if not name.startswith("_handle_"):
            continue
        if _touches_trade_context(name, calls):
            out.append(name[len("_handle_"):])
    return sorted(out)


class TradeContextDeferralGuard(unittest.TestCase):

    def test_every_trade_context_handler_is_deferred_or_explained(self):
        undeferred = []
        for method in _handlers_touching_trade_context():
            if method in LISTENER_DEFERRED_METHODS:
                continue
            if method in ORDER_METHODS:
                continue          # 下单类本来就不走 listener
            if method in DELIBERATELY_INLINE:
                continue
            undeferred.append(method)

        self.assertEqual(
            undeferred, [],
            "这些 handler 碰了 QMT 交易上下文却没进 LISTENER_DEFERRED_METHODS，"
            "会跑在后台 listener 线程上 —— QMT 在那儿返回的是「行数对、字段全空」"
            "的对象，从客户端看像「没数据」而不是调用失败（#204）。"
            "要么加进 defer 名单，要么在本文件的 DELIBERATELY_INLINE 里写明"
            "为什么可以不 defer：%s" % undeferred)

    def test_the_guard_actually_sees_something(self):
        """判据写错了会让这道闸静默失效 —— 先确认它真的扫得到东西。"""
        found = _handlers_touching_trade_context()
        self.assertGreater(len(found), 15, found)
        for expected in ("query_credit_detail", "query_stk_compacts",
                         "get_assure_contract", "get_unclosed_compacts",
                         "get_ipo_data", "query_account_infos"):
            self.assertIn(expected, found, expected)

    def test_exemptions_are_real_handlers_and_carry_a_reason(self):
        """豁免名单不能烂掉：方法要还存在，理由不能是空话。"""
        found = set(_handlers_touching_trade_context())
        for method, reason in DELIBERATELY_INLINE.items():
            self.assertIn(method, found,
                          "%s 已经不碰交易上下文了，从豁免名单里删掉" % method)
            self.assertNotIn(method, LISTENER_DEFERRED_METHODS,
                             "%s 已经 defer 了，从豁免名单里删掉" % method)
            self.assertGreater(len(reason), 20, method)

    def test_market_data_handlers_are_not_dragged_in(self):
        """行情读不该被这道闸拖去 defer —— 那会白搭一个 adjust 间隔的延迟。"""
        found = set(_handlers_touching_trade_context())
        for name in ("get_ticks", "get_market_data_ex", "ping",
                     "subscribe_whole_quote"):
            self.assertNotIn(name, found, name)


if __name__ == "__main__":
    unittest.main()
