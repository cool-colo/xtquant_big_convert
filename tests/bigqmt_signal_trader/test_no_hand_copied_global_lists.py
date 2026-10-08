# coding: utf-8
"""入口文件不许手抄 QMT 全局函数名单 —— #202 就是被一张手抄表坑的。

QMT 只往**被挂载的那个文件**的命名空间里注入全局函数，所以捕获必须发生在
入口文件里、用它自己的 globals()。策略模块的 `_QMT_INJECTED_GLOBAL_FUNCS`
是这份名单的唯一来源，它自己的注释就写着「do not hand-copy the names
elsewhere」。

但 BIGQMT_REDIS_DRYRUN.py 里就有一张手抄表。给策略模块加
`query_credit_account` 时漏了那张表，于是：

  - 桥拿不到这个函数
  - probe 报 callback_bound=false、global_namespace 也是 False
  - 报告据此下结论「这台终端没有 query_credit_account，重启也没用」
  - 而用户在大 QMT 里直接调它是**好用的** —— 维持担保比例 3.35、总负债 1038962.07

**一个桥的 bug 被报成了券商终端的能力缺失。** 这比返回空更糟：它给出的是
一个错误但听起来很确定的结论，会让人不去查真正的地方。

所以这里钉两件事：入口文件必须用 capture_qmt_injected_funcs 读唯一来源；
不许再出现手抄的名单。
"""
import ast
import io
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader_strategy import (  # noqa: E402
    _EXTRA_QMT_GLOBAL_FUNCS,
    _QMT_INJECTED_GLOBAL_FUNCS,
    capture_qmt_injected_funcs,
)

# 会被 QMT 挂载、因而必须自己做捕获的入口文件。
MOUNTED_ENTRIES = (
    os.path.join("src", "BIGQMT_REDIS_DRYRUN.py"),
    os.path.join("bigqmt_no_redis", "DRYRUN_no_redis.py"),
)

# 手抄表的特征：一串 QMT 全局函数名字面量凑在一起。真正的手抄表有 13-16 个
# 名字；入口里还有一个「这文件是不是作为策略在跑」的哨兵检查只列 3 个
# （passorder / get_trade_detail_data / download_history_data），那不是名单，
# 所以阈值取 5。
KNOWN_GLOBAL_NAMES = set(_QMT_INJECTED_GLOBAL_FUNCS)


def _source(relative):
    return io.open(os.path.join(ROOT, relative), encoding="utf-8",
                   errors="replace").read()


class NoHandCopiedListTest(unittest.TestCase):

    def test_mounted_entries_capture_from_the_single_source(self):
        for relative in MOUNTED_ENTRIES:
            text = _source(relative)
            self.assertIn(
                "capture_qmt_injected_funcs", text,
                "%s 是被 QMT 挂载的入口，必须用 capture_qmt_injected_funcs "
                "从策略模块的唯一来源取名单，不能自己列" % relative)

    def test_no_entry_hand_copies_the_name_list(self):
        offenders = []
        for relative in MOUNTED_ENTRIES:
            tree = ast.parse(_source(relative))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                    continue
                names = set(
                    elt.value for elt in node.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str))
                hits = names & KNOWN_GLOBAL_NAMES
                if len(hits) >= 5:
                    offenders.append("%s:%d 手抄了 %d 个全局函数名 %s"
                                     % (relative, node.lineno, len(hits),
                                        sorted(hits)[:4]))
        self.assertEqual(
            offenders, [],
            "入口文件里不许再出现手抄的 QMT 全局函数名单 —— 策略模块的 "
            "_QMT_INJECTED_GLOBAL_FUNCS 是唯一来源，手抄一份就会漂："
            "#202 的 query_credit_account 就是这么丢的。%s" % offenders)

    def test_the_single_source_carries_the_credit_counter_query(self):
        self.assertIn("query_credit_account", _EXTRA_QMT_GLOBAL_FUNCS)
        self.assertIn("query_credit_account", _QMT_INJECTED_GLOBAL_FUNCS)

    def test_capture_picks_up_everything_the_single_source_lists(self):
        """把整份名单摆进一个假命名空间，捕获必须一个不落。"""
        namespace = dict((name, lambda *a, **k: None)
                         for name in _QMT_INJECTED_GLOBAL_FUNCS)
        captured = capture_qmt_injected_funcs(namespace)
        self.assertEqual(sorted(captured), sorted(_QMT_INJECTED_GLOBAL_FUNCS))

    def test_capture_skips_names_the_terminal_does_not_inject(self):
        """终端没注入的就是没有 —— 捕获不能凭空造出一个。"""
        captured = capture_qmt_injected_funcs({"passorder": lambda *a: None})
        self.assertEqual(sorted(captured), ["passorder"])


if __name__ == "__main__":
    unittest.main()
