# -*- coding: utf-8 -*-
"""活人感出站润色。

只改「普通正文」：代码围栏、行内代码、URL、平台组件标签先切出来保护，
其余文字才套规则，所以代码示例、链接、@ 卡片一个字节都不会动。
"""

import re

from astrbot.api import AstrBotConfig
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register

try:  # Plain 组件在 4.x 里换过位置，能 import 哪个用哪个，都不行就按类名兜底
    from astrbot.api.message_components import Plain as _Plain
except Exception:  # noqa: BLE001 - 插件要能在不同小版本上加载
    try:
        from astrbot.core.message.components import Plain as _Plain
    except Exception:  # noqa: BLE001
        _Plain = None

# ---------------------------------------------------------------- 保护段
# 顺序有意义：围栏代码要整体吃掉，否则里面的 ` 和 # 会被后面的规则误伤。
PROTECT = re.compile(
    r"```[\s\S]*?```"
    r"|`[^`\n]*`"
    r"|https?://[^\s<>\"'）)）】》〉”’，。！？；：、……～~「」『』]+"
    r"|<[A-Za-z/][^>\n]*>"
)

# 成对加粗标记（`**x**`，顺带 `***x***`），只吃标记留内容。两条限制都是必须的：
#   1. 标记内侧不能挨着空白 —— 否则 `2 ** 10`、`a ** b` 这种幂运算/分隔符会被当加粗拆掉；
#   2. 首尾不能紧贴 ASCII 字母数字 —— 否则 `x**2**3` 这种连写幂运算、`**kwargs` 类代码会被误吃。
# 内容不跨行、不含星号；未成对的孤立 `**` 原样保留。单星号斜体和下划线不做处理：
# `*args`、`snake_case` 在聊天正文里太常见，剥标记的收益远小于把标识符改坏的风险。
BOLD_PAIR = re.compile(r"(?<![0-9A-Za-z*])\*{2,3}(?=\S)([^*\n]+?)(?<=\S)\*{2,3}(?![0-9A-Za-z*])")

# ---------------------------------------------------------------- 规则表
# 开场套话：只认开头，删掉整段，不碰后文。
OPENERS = [
    re.compile(r"^\s*(?:这|那)(?:确实|真)?(?:是|算)?(?:一个|个)?(?:很|挺|特别|非常|相当)?(?:好|不错|棒|有意思)(?:的)?(?:问题|疑问)[。！？!?，,、\s]*"),
    re.compile(r"^\s*(?:好问题|问得好|这个问题问得好)[。！？!?，,\s]*"),
    re.compile(r"^\s*作为(?:一个|一名)?(?:AI|人工智能|语言模型|大模型|智能助手|AI助手)[^。！？\n]{0,12}[，,。！？!?\s]*"),
    re.compile(r"^\s*(?:那么[，,]?)?(?:让|我们)们?(?:一起)?(?:来)?(?:看看|来看看|聊聊|聊一聊|说说|谈谈|深入了解一下|分析一下)[^。！？\n]{0,6}[。！？：:，,\s]*"),
    re.compile(r"^\s*在(?:当今|如今|现在这个)(?:社会|时代|快节奏的?时代)[^。！？\n]{0,12}[，,]\s*"),
    # 铺垫式开头（stop-slop-zh「开头套话」）：只认开头，且必须自带逗号，避免误吃正文里的同形词。
    re.compile(r"^\s*众所周知[，,]?\s*"),
    re.compile(r"^\s*不可否认(?:的是)?[，,]\s*"),
    re.compile(r"^\s*(?:近年来|近些年来|这几年)[，,]\s*"),
    re.compile(r"^\s*随着[^。！？\n]{0,24}?的(?:不断)?(?:发展|进步|普及|深入)[，,]\s*"),
    re.compile(r"^\s*在[^。！？\n]{0,12}?的(?:大背景|浪潮|大潮)下[，,]\s*"),
]

# 收尾套话：只认结尾。
CLOSERS = [
    re.compile(r"\s*(?:希望|愿)(?:这|以上|这些|上面|我的回答|本回答|回答)?(?:能|可以|对)?(?:帮|帮助|对)(?:到)?(?:你|您)(?:有所|有)?(?:帮助|用)?[^。！？\n]{0,12}[。！？!?～~]*\s*$"),
    re.compile(r"\s*(?<![倘假如设])(?:如果|倘若|假若|如若|若)(?:你|您)(?:还)?(?:有|需要)(?:其他|其它|任何|别的)?(?:问题|疑问|帮助|需要)[^。！？\n]{0,15}[。！？!?～~]*\s*$"),
    re.compile(r"\s*如有(?:其他|其它|任何)?(?:问题|疑问)[^。！？\n]{0,15}[。！？!?～~]*\s*$"),
    re.compile(r"\s*(?:欢迎|随时)(?:随时)?(?:提问|交流|追问|继续问|联系)[^。！？\n]{0,10}[。！？!?～~]*\s*$"),
]

# 只起转场作用的短语，删掉不影响信息。
CONNECTIVES = [
    re.compile(r"(?:总的来说|总而言之|综上所述|简而言之|概括来说|简单来说就是)[，,：:]?\s*"),
    re.compile(r"(?:需要注意的是|值得(?:一提|注意)的是|需要指出的是|需要说明的是|要注意的是)[，,：:]?\s*"),
    re.compile(r"(?m)^\s*首先[，,]\s*"),
    re.compile(r"(?m)^\s*(?:其次|再次|最后|然后)[，,]\s*"),
]

# 书面词 → 口语词。中文没有空格可依赖，纯子串替换会把构词拆坏（非常规→挺规、
# 是否定句→是不是定句、除此之外→除此另外），所以每对词都带一条边界的反向证据：
# 后面紧跟的字符能拼成别的词、或前面是另一个词的尾字时，就当成构词不换。
# 不做分词：插件要保持零依赖，而这些词的可拆位置是有限且能枚举的。
REPLACEMENTS = [
    # 虚词连词仅在句首、分句或标点/空白之后替换，避免误伤固定成语/专有名词（如“油然而生”、“自然而然”、“斐然而成”等）
    (r"(?:^|(?<=[，。；：！？、,.;:!?\s~～(（\[【《“'\"`]))因此", "所以"),
    (r"(?:^|(?<=[，。；：！？、,.;:!?\s~～(（\[【《“'\"`]))然而", "不过"),
    (r"(?<!此)此外", "另外"),            # 除此之外
    (r"与此同时", "同时"),
    (r"并且", "而且"),
    (r"是否(?!是|定(?![期时]))", "是不是"),  # 是否定句保留；是否定期/定时照换
    # 「即可」两种用法要分开：落在句末/标点前是谓语补语，换「就行」；
    # 后面还跟着动词是前置连动（运行脚本即可启动），换「就行」会变成不通顺的
    # 「运行脚本就行启动」，这类只换副词「就能」。
    # 同时注意：排除「立即」（如「立即可用」）；句末判定不能贪婪吃行间普通空格，仅限标点、换行或文本末尾。
    (r"(?<!立)即可(?=[，。；：！？、,.;:!?～~」』）)】》〉”’]|(?:\s*[\r\n]|\s*$))", "就行"),
    (r"(?<!立)即可", "就能"),
    # 「务必」：前驱排除「事务/服务/财务/业务/劳务/法务/政务/医务/警务/税务」等复合名词后缀；
    # 且后驱排除「须/定/然/将/备」，避免将「*务必须/必定/必然」等误拆为「*务 + 一定 + 须/定」
    (r"(?<![事服财务业劳法政医警税])务必(?!须|定|然|将|备)", "一定"),
    (r"较为", "比较"),
    (r"极其", "特别"),
    (r"非常(?![规态见识]|时(?![尚]))", "挺"),  # 非常规/非常态/非常时期；非常时尚照换
    (r"以及(?!时(?![间候段长]))", "和"),  # 以及时止损保留，以及时间照换
]
REPLACEMENT_PATTERNS = [(re.compile(a), b) for a, b in REPLACEMENTS] + [
    # 「若」单字多义：既是连词，又是成语构件（欣喜若狂/置若罔闻/若无其事/若即若离/
    # 泰然自若）。只替换两种高置信位置，其余一律不动：
    #   1. 整词「倘若/假若/如若/设若/若是」，顺带吃掉后面那个「是」；
    #   2. 「若 + 主语或否定词」，且必须落在分句开头（行首、标点或空格之后，或跟在
    #      「但/而」后面）——「视若他人」「判若两人」这类构词因此不会被拆。
    (re.compile(r"(?:倘若|假若|如若|设若)是?"), "要是"),
    (re.compile(r"若是"), "要是"),
    (re.compile(r"(?:^|(?<=[，。；：！？、,.;:!?\s但而]))若(?=[你我您他她它咱们各位不])"), "要是"),
]

# ---------------------------------------------------------------- AI 陈词（v1.0.8）
# 词表与句式来自 D:\去AI味 的 stop-slop-zh（references/phrases.md）与
# humanizer-zh-chat（references/zh-hans.md §1-2）。取舍标准只有一条：
# 只收「删掉/替换后信息量不变」的项。需要判断频率的（单个「其实」、单个破折号、
# 单次「不是…而是」在人类文本里都是正常现象）一律不进规则——正则没法数聚簇。
CLICHES = [
    # 同义反复：两个词说的是同一件事
    (r"目前现状", "现状"),
    (r"免费赠送", "赠送"),
    (r"必须要", "必须"),
    (r"人为的故意", "故意"),
    # 填充短语：删掉不损失任何信息（只吃带逗号的整句，正文里同形词不动）
    (r"从本质上(?:来说|讲)[，,]?", ""),
    (r"从根本上(?:来说|讲|说)[，,]?", ""),
    (r"归根结底[，,]?", ""),
    (r"客观(?:来看|地说)[，,]?", ""),
    (r"严格(?:来说|意义上讲)[，,]?", ""),
    # 营销腔 / 中式学术腔：换成平实的那个词
    (r"致力于打造", "做"),
    (r"深入探讨", "聊"),
    (r"深入剖析", "分析"),
    (r"全面提升(?=[^，。！？\n]{0,8}(?:效率|质量|能力|水平|体验|速度))", "提高"),
    # 互联网黑话：换成字面意思。只收不会被别的义项误伤的写法
    (r"((?:竞争|商业|技术|品牌|产品|成本))护城河", r"\1优势"),
    (r"第二增长曲线", "新的增长点"),
    (r"全链路", "全流程"),
    (r"破圈", "出圈"),
    (r"引爆点", "爆点"),
    (r"抓手", "办法"),
    (r"心智", "认知"),
]
CLICHE_PATTERNS = [(re.compile(a), b) for a, b in CLICHES]

# 句末空泛升华：「A，为 B 奠定了基础。」这类尾巴只宣布重要性，删到句号为止。
# 只认「逗号 + 为… + 固定空泛谓语 + 句末」，正文中段的同形句不碰。
EMPTY_TAIL = re.compile(
    r"[，,；;]\s*(?:这|该|此|它)?[^，。！？；\n]{0,8}?为[^，。！？；\n]{1,20}?"
    r"(?:奠定了基础|指明了方向|开辟了新的篇章|打开了新的大门|注入了新的活力|"
    r"留下了深刻的启示|敲响了警钟)[。！!]?\s*$"
)

# 元叙事标签：「直觉：」「直观理解：」这类先宣布要说什么的引导词。
META_LABEL = re.compile(
    r"(?m)^[ \t]*(?:直觉|直观理解|核心观察是|核心思想是|基本想法是|关键性质|关键等式)[：:][ \t]*"
)

# 聊天口癖清理（short_chat）：真人在 QQ/微信里回话不会把每句都收尾标好。
# 只去「整条消息末尾」的句号/感叹号，问号保留；句中的标点不动。
TAIL_PUNCT = re.compile(r"[。！!～~]+$")

# 复述型尾巴：「……，也就是说 XXX」把前面刚说过的话换个说法，删掉不丢信息。
# 只认这几个纯复述引导词，且必须带逗号开新分句。
RESTATEMENT = re.compile(r"[，,]\s*(?:也就是说|换句话说|说白了|简单来说|简单说|具体来说)[^。！？\n]*")

# 顺带一提型插入语：只删引导语本身，后面那句真正的内容留着。
ASIDE_LEAD = re.compile(r"(?:^|(?<=[。！？\n]))\s*(?:顺便(?:说一下|说一句|提一下|提一句|一提)|顺带(?:一提|说一句))[，,]\s*")

SENTENCE_SPLIT = re.compile(r"[^。！？\n]+[。！？]?")

TOOLCALL_HINT = re.compile(
    r"<\s*(?:function_calls?|tool_calls?|invoke|function|parameter)\b", re.IGNORECASE
)


def _segments(text: str):
    """把文本切成 [(是否受保护, 片段)]，受保护段原样保留。"""
    out, pos = [], 0
    for m in PROTECT.finditer(text):
        if m.start() > pos:
            out.append((False, text[pos:m.start()]))
        out.append((True, m.group(0)))
        pos = m.end()
    if pos < len(text):
        out.append((False, text[pos:]))
    return out


def _is_plain(comp) -> bool:
    if _Plain is not None and isinstance(comp, _Plain):
        return True
    return type(comp).__name__ == "Plain"


def _plain_components(resp):
    """取出消息链里的文本段（位置不动）和整条链。没有链时返回空列表。"""
    chain = getattr(getattr(resp, "result_chain", None), "chain", None) or []
    return [comp for comp in chain if _is_plain(comp)], chain


def _clip_sentences(s: str, limit: int) -> str:
    """只留前 limit 句（句子按 。！？ 或换行切，句末标点算在句内）。"""
    if limit <= 0:
        return s
    parts = [p for p in SENTENCE_SPLIT.findall(s) if p.strip()]
    if len(parts) <= limit:
        return s
    return "".join(parts[:limit]).rstrip()


def _clean_markdown(s: str) -> str:
    s = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]+", "", s)   # 行首 # 标题符号
    s = BOLD_PAIR.sub(r"\1", s)                      # 只拆成对的加粗标记，保留内容
    s = re.sub(r"—{2,}", "——", s)                   # 长破折号压一个
    s = re.sub(r"\n{3,}", "\n\n", s)                 # 多余空行
    return s


@register(
    "astrbot_plugin_humanizer",
    "Maxsun",
    "出站前抹掉 AI 味话术壳（开场白/总结句/书面连接词/陈词黑话/markdown 痕迹），代码块、链接、@ 卡片逐字节保留。",
    "1.0.10",
)
class HumanizerPlugin(Star):
    """只在普通正文上做位置式改写，受保护片段原样穿过。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super(HumanizerPlugin, self).__init__(context)
        self.config = config
        self._load(config)

    # ------------------------------------------------------------ 配置
    def _load(self, config) -> None:
        self.enable = bool(config.get("enable", True))
        self.strip_openers = bool(config.get("strip_openers", True))
        self.strip_closers = bool(config.get("strip_closers", True))
        self.strip_connectives = bool(config.get("strip_connectives", True))
        self.colloquial = bool(config.get("colloquial_swap", True))
        self.strip_cliches = bool(config.get("strip_ai_cliches", True))
        self.short_chat = bool(config.get("short_chat", True))
        try:
            self.max_sentences = int(config.get("max_sentences", 0) or 0)
        except (TypeError, ValueError):
            self.max_sentences = 0
        self.clean_md = bool(config.get("clean_markdown", True))
        self.llm_rewrite = bool(config.get("llm_rewrite", False))
        self.llm_provider = str(config.get("llm_provider_id", "") or "").strip()
        self.llm_style = str(config.get("llm_style", "") or "").strip()

        raw = config.get("custom_remove", []) or []
        if isinstance(raw, str):
            raw = [raw]
        self.custom: list = []
        for item in raw:
            item = str(item or "").strip()
            if not item:
                continue
            try:
                self.custom.append(re.compile(item))
            except re.error as exc:
                logger.warning(f"[humanizer] 自定义正则无效已跳过：{item}（{exc}）")

    # ------------------------------------------------------------ 核心
    def humanize(self, text: str, at_start: bool = True, at_end: bool = True,
                 allow_empty: bool = False) -> str:
        """纯函数，便于离线回归：输入原文，输出润色后的正文。

        at_start / at_end 控制开场、收尾套话要不要认——图文混排时只有第一段算开头、
        最后一段算结尾；allow_empty 给「消息链里还有别的组件」的场景用，允许这一段被清空。
        """
        if not text or not text.strip():
            return text

        parts = _segments(text)
        prose_idx = [i for i, (protected, _) in enumerate(parts) if not protected]
        if not prose_idx:
            return text

        first, last = prose_idx[0], prose_idx[-1]
        for i in prose_idx:
            protected, chunk = parts[i]
            parts[i] = (
                False,
                self._rewrite_chunk(chunk, at_start=(at_start and i == first), at_end=(at_end and i == last)),
            )

        result = "".join(chunk for _, chunk in parts)
        result = re.sub(r"\n{3,}", "\n\n", result).strip()
        if self.max_sentences > 0 and "```" not in result:
            # 带围栏代码的回复不截断：按句号切会切到代码里
            result = _clip_sentences(result, self.max_sentences).strip()
        if self.short_chat and at_end:
            # 只收整条消息的收尾标点；后头还有图片/下一段时，那个句号是分隔符，留着。
            result = TAIL_PUNCT.sub("", result).strip()
        if result.strip():
            return result
        # 规则把整条吃空时：单条消息保留原文，宁可不改也不发空消息；
        # 消息链里还有别的段时这一段可以为空，否则收尾套话永远删不掉。
        return "" if allow_empty else text

    def _rewrite_chunk(self, s: str, at_start: bool, at_end: bool) -> str:
        if not s.strip():
            return s
        if at_start and self.strip_openers:
            for pattern in OPENERS:
                s, hit = pattern.subn("", s, count=1)
                if hit:
                    break
        if at_end and self.strip_closers:
            for pattern in CLOSERS:
                s, hit = pattern.subn("", s, count=1)
                if hit:
                    break
        if self.strip_connectives:
            for pattern in CONNECTIVES:
                s = pattern.sub("", s)
        for pattern in self.custom:
            s = pattern.sub("", s)
        if self.colloquial:
            for pattern, repl in REPLACEMENT_PATTERNS:
                s = pattern.sub(repl, s)
        if self.strip_cliches:
            s = EMPTY_TAIL.sub("。", s)
            for pattern, repl in CLICHE_PATTERNS:
                s = pattern.sub(repl, s)
            s = META_LABEL.sub("", s)
        if self.short_chat:
            s = ASIDE_LEAD.sub("", s)
            s = RESTATEMENT.sub("", s)
        if self.clean_md:
            s = _clean_markdown(s)
        return s

    # ------------------------------------------------------------ 可选：模型润色
    async def _llm_polish(self, text: str, original: str) -> str:
        if len(text.strip()) < 8 or not self.llm_provider:
            return text
        style = self.llm_style or (
            "只改口气，保留全部事实、数字和代码。像人在 QQ 里随手打字："
            "能一句说完就一句，最多三句，每句尽量 20 字以内；不复述问题、不解释自己、不加结尾总结。"
            "只输出改写后的正文。"
        )
        prompt = f"{style}\n\n待改写的回复：\n{text}"
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.llm_provider, prompt=prompt
            )
        except Exception as exc:  # 模型档挂了不影响主流程
            logger.warning(f"[humanizer] 润色调用失败，保留规则结果：{exc}")
            return text
        out = (getattr(resp, "completion_text", "") or "").strip()
        # 四道闸：空输出、把工具调用写成正文、缩水过头、比原文还长（润色不等于扩写）
        if not out:
            return text
        if TOOLCALL_HINT.search(out):
            logger.warning("[humanizer] 润色结果含工具调用语法，已丢弃")
            return text
        if not (len(text) * 0.4 <= len(out) <= len(text) * 1.15 + 10):
            logger.warning(f"[humanizer] 润色结果长度异常（{len(text)}→{len(out)}），已丢弃")
            return text
        return out

    # ------------------------------------------------------------ 钩子
    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if not self.enable:
            return
        source = getattr(resp, "completion_text", "") or ""
        if not source.strip():
            return

        plains, chain = _plain_components(resp)
        if plains:
            # 原位改每个 Plain，绝不碰 completion_text：
            # 它的 setter 会剔掉所有 Plain 再往索引 0 插一整段，图文顺序会被打乱。
            allow_empty = len(chain) > 1
            last = len(plains) - 1
            for i, comp in enumerate(plains):
                raw = getattr(comp, "text", "") or ""
                new = self.humanize(
                    raw,
                    at_start=(i == 0),
                    at_end=(i == last),
                    allow_empty=allow_empty,
                )
                if self.llm_rewrite:
                    new = await self._llm_polish(new, raw)
                if new != raw:
                    comp.text = new
            logger.debug(f"[humanizer] 按 {len(plains)} 段正文原位润色")
            return

        cleaned = self.humanize(source)
        if self.llm_rewrite:
            cleaned = await self._llm_polish(cleaned, source)

        if cleaned != source:
            # 没有消息链（纯字符串响应）时才走整体赋值。
            resp.completion_text = cleaned
            logger.debug(f"[humanizer] 润色 {len(source)} → {len(cleaned)} 字")
