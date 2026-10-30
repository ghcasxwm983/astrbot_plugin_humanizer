# -*- coding: utf-8 -*-
"""离线回归：不装 AstrBot 也能跑。

    python tests/test_humanizer.py

astrbot 的运行时用不到，这里用最小桩件顶上，只验规则函数本身。
"""

import asyncio
import importlib.util
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _install_astrbot_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    class _Any:
        def __init__(self, *args, **kwargs):
            pass

    astrbot = types.ModuleType("astrbot")
    astrbot.logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
    )
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict

    event = types.ModuleType("astrbot.api.event")

    class _Filter:
        @staticmethod
        def on_llm_response(*args, **kwargs):
            return lambda fn: fn

    event.filter = _Filter()
    event.AstrMessageEvent = _Any

    provider = types.ModuleType("astrbot.api.provider")
    provider.LLMResponse = _Any

    star = types.ModuleType("astrbot.api.star")

    class _Star:
        def __init__(self, context):
            self.context = context

    star.Context = _Any
    star.Star = _Star
    star.register = lambda *args, **kwargs: (lambda cls: cls)

    api.event, api.provider, api.star = event, provider, star
    astrbot.api = api
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.provider": provider,
            "astrbot.api.star": star,
        }
    )


def _load_plugin_module():
    _install_astrbot_stubs()
    spec = importlib.util.spec_from_file_location("humanizer_main", os.path.join(ROOT, "main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main = _load_plugin_module()

DEFAULTS = {
    "enable": True,
    "strip_openers": True,
    "strip_closers": True,
    "strip_connectives": True,
    "colloquial_swap": True,
    "clean_markdown": True,
    "llm_rewrite": False,
}


def make_plugin(**overrides):
    config = dict(DEFAULTS)
    config.update(overrides)
    return main.HumanizerPlugin(context=None, config=config)


class CleanMarkdownBoldTests(unittest.TestCase):
    def clean(self, s):
        return main._clean_markdown(s)

    def test_paired_bold_is_unwrapped(self):
        self.assertEqual(self.clean("这是**重点**内容"), "这是重点内容")
        self.assertEqual(self.clean("**整段加粗**"), "整段加粗")
        self.assertEqual(self.clean("**2024 年**的账"), "2024 年的账")

    def test_triple_star_also_unwrapped(self):
        self.assertEqual(self.clean("***很重要***"), "很重要")

    def test_math_power_operator_untouched(self):
        self.assertEqual(self.clean("2 ** 10 = 1024"), "2 ** 10 = 1024")
        self.assertEqual(self.clean("a ** b ** c"), "a ** b ** c")
        self.assertEqual(self.clean("x**2**3"), "x**2**3")

    def test_kwargs_untouched(self):
        self.assertEqual(self.clean("def f(**kwargs): pass"), "def f(**kwargs): pass")

    def test_unpaired_stars_untouched(self):
        self.assertEqual(self.clean("**未闭合"), "**未闭合")
        self.assertEqual(self.clean("结尾两个星号**"), "结尾两个星号**")

    def test_single_star_and_underscore_left_alone(self):
        self.assertEqual(self.clean("*斜体*"), "*斜体*")
        self.assertEqual(self.clean("__粗体__"), "__粗体__")
        self.assertEqual(self.clean("some_var_name"), "some_var_name")

    def test_bold_does_not_cross_lines(self):
        self.assertEqual(self.clean("**第一行\n第二行**"), "**第一行\n第二行**")


class RuoReplacementTests(unittest.TestCase):
    """「若」只在连词位置口语化，成语与固搭不动（issue #3）。"""

    def humanize(self, s):
        return make_plugin(
            strip_openers=False,
            strip_closers=False,
            strip_connectives=False,
            clean_markdown=False,
        ).humanize(s)

    def test_idioms_untouched(self):
        for s in [
            "欣喜若狂",
            "置若罔闻",
            "安之若素",
            "若即若离",
            "若隐若现",
            "判若两人",
            "视若无睹",
            "泰然自若",
            "若无其事",
            "了若指掌",
            "视若他人",
            "若干问题",
            "否则就算了",
            "宛若仙境",
        ]:
            self.assertEqual(self.humanize(s), s, s)

    def test_conjunction_forms_replaced(self):
        self.assertEqual(self.humanize("若是这样"), "要是这样")
        self.assertEqual(self.humanize("假若是这样"), "要是这样")
        self.assertEqual(self.humanize("假若你不同意"), "要是你不同意")
        self.assertEqual(self.humanize("倘若失败"), "要是失败")
        self.assertEqual(self.humanize("如若不行就换方案"), "要是不行就换方案")
        self.assertEqual(self.humanize("若你同意，我们就开始"), "要是你同意，我们就开始")
        self.assertEqual(self.humanize("若不及时处理，会出问题"), "要是不及时处理，会出问题")
        self.assertEqual(self.humanize("但若你觉得不合适"), "但要是你觉得不合适")

    def test_no_double_replacement(self):
        self.assertEqual(self.humanize("若是"), "要是")
        self.assertEqual(self.humanize("若是是"), "要是是")

    def test_closer_does_not_clip_ruo_inside_word(self):
        # 收尾套话规则里也有「若」，不能把「倘若」拆成「倘 + 若」再啃掉后半句。
        text = "倘若你还有问题，欢迎随时提问"
        self.assertEqual(make_plugin().humanize(text), text)
        self.assertEqual(
            make_plugin().humanize("配置就这些。若你还有其他问题，欢迎随时提问"), "配置就这些。"
        )


class Plain:
    """假 Plain：类名必须叫 Plain，main 的类名兜底靠它认。"""

    def __init__(self, text=""):
        self.text = text


class _Image:
    pass


class _Chain:
    def __init__(self, comps):
        self.chain = list(comps)


class _Resp:
    """复刻 AstrBot LLMResponse 的 completion_text 语义（issue #5 的现场）。"""

    def __init__(self, comps):
        self.result_chain = _Chain(comps)

    @property
    def completion_text(self):
        return "".join(c.text for c in self.result_chain.chain if isinstance(c, Plain))

    @completion_text.setter
    def completion_text(self, value):
        # 真实实现：剔掉所有 Plain，再整段插到索引 0
        self.result_chain.chain = [c for c in self.result_chain.chain if not isinstance(c, Plain)]
        self.result_chain.chain.insert(0, Plain(value))


class _StrResp:
    """没有消息链的纯字符串响应。"""

    def __init__(self, text):
        self.completion_text = text


class _Event:
    pass


class MessageChainTests(unittest.TestCase):
    """图文混排时按段原位改写，不打乱组件顺序（issue #5）。"""

    def run_hook(self, resp, **overrides):
        plugin = make_plugin(**overrides)
        asyncio.run(plugin.on_llm_response(_Event(), resp))
        return plugin

    def test_image_keeps_its_position(self):
        img = _Image()
        resp = _Resp([
            Plain("这是一个很好的问题。正文在中间。"),
            img,
            Plain("希望这对你有帮助。"),
        ])
        self.run_hook(resp)
        comps = resp.result_chain.chain
        self.assertEqual([type(c).__name__ for c in comps], ["Plain", "_Image", "Plain"])
        self.assertIs(comps[1], img)
        self.assertEqual(comps[0].text, "正文在中间。")
        self.assertEqual(comps[2].text, "")

    def test_completion_text_is_joined_view(self):
        resp = _Resp([Plain("**要点**：因此要这样做。"), _Image(), Plain("结尾")])
        self.run_hook(resp)
        self.assertEqual(resp.completion_text, "要点：所以要这样做。结尾")

    def test_multi_component_chain_still_has_two_plains(self):
        resp = _Resp([Plain("开头"), _Image(), Plain("结尾")])
        self.run_hook(resp)
        self.assertEqual(len(resp.result_chain.chain), 3)

    def test_middle_plain_is_not_treated_as_ending(self):
        resp = _Resp([
            Plain("开头"),
            _Image(),
            Plain("如果还有其他问题，欢迎随时提问。"),
            _Image(),
            Plain("结尾"),
        ])
        self.run_hook(resp)
        self.assertEqual(resp.result_chain.chain[2].text, "如果还有其他问题，欢迎随时提问。")

    def test_single_plain_chain_never_emptied(self):
        # 只有一段正文时，规则把话吃干净也不发空消息，保留原文
        resp = _Resp([Plain("希望这对你有帮助。")])
        self.run_hook(resp)
        self.assertEqual(resp.completion_text, "希望这对你有帮助。")

    def test_plain_string_response_uses_assignment(self):
        resp = _StrResp("**结论**：因此可以直接上线。")
        self.run_hook(resp)
        self.assertEqual(resp.completion_text, "结论：所以可以直接上线。")

    def test_image_only_chain_is_noop(self):
        resp = _Resp([_Image()])
        self.run_hook(resp)
        self.assertEqual(len(resp.result_chain.chain), 1)


class HumanizeTests(unittest.TestCase):
    def test_protected_code_block_keeps_stars(self):
        text = "看代码：\n```python\nprint(2 ** 10)\n```\n结论是**很好**"
        self.assertEqual(
            make_plugin().humanize(text),
            "看代码：\n```python\nprint(2 ** 10)\n```\n结论是很好",
        )

    def test_inline_code_keeps_stars(self):
        self.assertIn("`a ** b`", make_plugin().humanize("幂运算写成 `a ** b` 就行"))

    def test_power_operator_survives_full_pipeline(self):
        text = "在 Python 里 2 ** 10 等于 1024"
        self.assertEqual(make_plugin().humanize(text), text)

    def test_opener_and_closer_still_stripped(self):
        text = "这是一个很好的问题。**结论**：因此可以直接上线。希望这对你有帮助。"
        self.assertEqual(make_plugin().humanize(text), "结论：所以可以直接上线。")

    def test_empty_input_passthrough(self):
        self.assertEqual(make_plugin().humanize(""), "")
        self.assertEqual(make_plugin().humanize("**"), "**")


if __name__ == "__main__":
    unittest.main(verbosity=2)
