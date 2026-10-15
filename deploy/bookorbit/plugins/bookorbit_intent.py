"""BookOrbit 有声书意图预判 —— 在 xiaozhi 调 LLM 之前拦一道。

为什么需要它
------------
Hermes 的 api_server 建 agent 时用自己 config 的 toolsets（L2232
`_get_platform_tools(user_config, "api_server")`），请求 body 里的 `tools`
只进了请求指纹用于幂等去重，**从没传给 agent**。所以 xiaozhi 通过
`response_with_functions(functions=...)` 下发的插件函数列表，Hermes 侧
永远看不到——实测 `tool_choice=required` 也一个 tool_call 都不产出。

结论：LLM 选工具这条路在 Hermes 后端上是断的，不该继续在 provider
层打补丁。改成确定性前置匹配：文本命中就直接造一个 play_audiobook 的
tool_call，绕开 LLM。

设计约束
--------
1. **只拦明确命中**：宁可漏放给 LLM 正常回答，也不要把「三体讲了什么」
   这种普通提问误判成「播放三体」。
2. **不吞对话**：命中后仍把书名/模式作为 tool_call 参数，插件自己回话。
3. 书名从书库真实清单里匹配（构造期注入），不在清单里的一律不拦，
   交给 LLM 用嘴回答「书库里没有」。
4. **容 ASR 的错**：ASR 会把书名写成同音字或阿拉伯数字（实测「三体」
   →「3天」「3题」）。匹配分四层，字面（完整名/核心名）优先，
   拼音兜底；都不过就原样放给 LLM，不硬猜。
"""

import logging
import os
import re
import sys

logger = logging.getLogger(__name__)

# 可选依赖：pypinyin —— 同音兜底。
# 表驱动修字只能覆盖已知变体，拼音比对能兜住没见过的新变体。
# 依赖装在插件同目录的 _vendor（docker-compose 里挂进来），容器重建不丢；
# 装不上也不影响主链：_HAS_PINYIN=False 时退回纯表匹配。
_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)
try:
    from pypinyin import lazy_pinyin

    _HAS_PINYIN = True
except Exception:  # 没有依赖只是少一层容错，不影响主链
    lazy_pinyin = None
    _HAS_PINYIN = False

# 播放意图的触发词。任一命中即进入书名解析。
_PLAY_PATTERNS = [
    re.compile(p) for p in (
        r"播放\s*(?:有声书|音频|书)?",
        # 「重新播放」被 NEW 规则吃掉「重新播」后剩下的「放…」也要认，
        # 所以这里要求「放」后面是书名而不是量词+类型词。
        r"放[一以下]?(?:个|首|下)?(?:有声书|音频|小说|书)?",
        r"听[一以下]?(?:个|首|下)?(?:有声书|小说|书)?",
        r"续播",
        r"接着(?:听|播|看)",
        r"继续(?:听|播|看)",
        r"读[一以下]?(?:本|个|集)?",
    )
]

# 明确要「从头」而非续播。注意：这些是「切书名用」的正则，
# 要能吃掉「重新播」这种把动词劈成两半的残留。
_NEW_PATTERNS = [
    re.compile(p) for p in (
        r"从头",
        r"重新(?:听|播|读|播放)",
        r"重头",
        r"第一集",
        r"第[一1]集",
        r"开头",
    )
]

# ASR 常见同音/近音错字 → 正确写法。实测「三体」被听成过「3天」（首夜）
# 和「3题」（10-15 早上）。带阿拉伯数字的写法在这里也要留全——_clean
# 是逐字替换，发生在 _normalize 之前，不走数字转汉字那张表。
_HOMOPHONE_FIX = {
    "3天": "三体", "三天": "三体", "仨体": "三体", "3提": "三体",
    "三提": "三体", "山体": "三体", "散体": "三体", "3体": "三体",
    "3题": "三体", "三题": "三体", "叁体": "三体",
}

# 书名里的噪声：ASR 常见错字/口头语。「3天」是实测到的「三体」误识。
_NOISE = [
    "有声书", "音频", "小说", "这本书", "那本书", "一下", "一个", "一首",
    "帮我", "给我", "我要", "我想", "麻烦", "现在", "接着", "继续", "从头",
    "开始", "播放",
]

# 归一化要忽略的字符：ASR 不会读出书名号/括号/书名号里的空格。
# 匹配时两边都归一化，所以「播放剑来上」能对上书名「《剑来》上」。
_IGNORED_CHARS = re.compile(r"[《》「」『』（）()\[\]【】·\s]")

# ASR 输出数字时给阿拉伯数字（「3体」），库里书名是汉字（「三体」）。
# 两边都过这张表，数字写法差异就不再影响匹配。
_DIGIT_TO_CN = str.maketrans("0123456789", "零一二三四五六七八九")


def _normalize(text: str) -> str:
    """去掉书名号/括号等 ASR 不会读出的字符，阿拉伯数字转成汉字。"""
    return _IGNORED_CHARS.sub("", text).translate(_DIGIT_TO_CN)


# 核心名的切分点：原书名里的括号/书名号/竖线/冒号。
# 必须在 _normalize 之前切——normalize 会把括号整段删掉，
# 切完就找不到「三体（王明军演播）」里括号前的那截了。
_CORE_SPLIT_RAW = re.compile(r"[（(《》「」『』【】\[\]｜|:：]")


def _core_name(title: str) -> str:
    """取用户口语里会说的那个核心书名。

    入参是**原始**书名。切分点取括号/书名号/竖线/冒号——它们是书名
    内部与「演播者/副标题/卷次」之间的自然分界，比任意前缀可靠。
    切完再归一化，所以「三体（王明军演播）」→「三体」、
    「《剑来》上」→「剑来上」、「余罪：我的刑侦笔记」→「余罪」。
    """
    parts = [p for p in _CORE_SPLIT_RAW.split(title) if p.strip()]
    core = parts[0] if parts else title
    return _normalize(core)


def _clean(raw: str) -> str:
    """从 ASR 文本里剥出书名候选。"""
    text = raw.strip()
    for wrong, right in _HOMOPHONE_FIX.items():
        text = text.replace(wrong, right)
    for p in _PLAY_PATTERNS:
        text = p.sub(" ", text)
    for p in _NEW_PATTERNS:
        text = p.sub(" ", text)
    for n in _NOISE:
        text = text.replace(n, " ")
    # 剩下的标点/空白
    text = re.sub(r"[，。！？、,.!?~]", " ", text).strip()
    # 句尾语气词：「三体吧」「听三体啊」——大模型刚问过「想听哪本」，
    # 用户答书名时几乎一定带个语气词。
    text = re.sub(r"[吧啊呀呢哦嘛哇啦]+$", "", text).strip()
    return text


class AudiobookIntent:
    """匹配器。构造时传入书库真实书名，运行时只做纯文本判断。"""

    def __init__(self, book_titles):
        # 长名优先：三体（王明军演播）要能盖过 三体
        self.titles = sorted(set(book_titles), key=len, reverse=True)
        # 归一化后的一份，供 ASR 文本（无书名号）匹配
        self._norm_pairs = [
            (t, _normalize(t)) for t in self.titles if _normalize(t)
        ]
        # 核心名 = 剥掉书名号/括号/竖线后缀后的整段。
        # 「三体（王明军演播）」→「三体」；「《剑来》上」→「剑来上」；
        # 「余罪：我的刑侦笔记」→「余罪」（无分隔符则不变）。
        # 核心名仍需是完整词组，不能截断——否则长书名会先被短前缀抢走。
        self._core_pairs = []
        for t in self.titles:
            core = _core_name(t)
            if core and core != _normalize(t):
                self._core_pairs.append((t, core))
        # 核心名长的优先（「余罪」要先于更短的候选被判掉）
        self._core_pairs.sort(key=lambda p: len(p[1]), reverse=True)

        # 拼音形式，供同音兜底：ASR 错字时字面对不上、读音对得上
        # （三体 ← 3题/三提/山体）。只收「完整书名」和「核心名」两种形态，
        # 与字面两级同一语义；至少两个音节才参战，单字书名太容易误伤。
        self._py_pairs = []
        if _HAS_PINYIN:
            seen = set()
            for t in self.titles:
                for form in (_normalize(t), _core_name(t)):
                    if not form or form in seen:
                        continue
                    seen.add(form)
                    syl = lazy_pinyin(form)
                    if len(syl) >= 2:
                        self._py_pairs.append((t, "".join(syl)))
            self._py_pairs.sort(key=lambda p: len(p[1]), reverse=True)

        # 「整句就是书名」用：一个字都不差（或拼音一字不差）才算。
        # 供「三体吧」这种没有播放动词、但整句就是书名的回话使用。
        # setdefault + titles 已按长名优先，所以核心名相同时归长书名。
        self._exact_map = {}
        for t in self.titles:
            for form in (_normalize(t), _core_name(t)):
                if form:
                    self._exact_map.setdefault(form, t)
        self._exact_py = {}
        if _HAS_PINYIN:
            for form, t in self._exact_map.items():
                syl = lazy_pinyin(form)
                if len(syl) >= 2:
                    self._exact_py.setdefault("".join(syl), t)

    def _match_title(self, text: str):
        """在给定文本里找书名，返回原始书名或 None。

        两级匹配，都要求**完整**命中，不做「取前 N 字」：
        1. 完整书名（含括号/书名号）
        2. 核心名（剥掉括号后缀，如「三体（王明军演播）」→「三体」）
           —— 用户口语只说核心名，核心名仍需是书名的完整前缀。

        试过「取前 3 字」宽松匹配：会让「三体讲了什么」这类普通提问命中，
        误判代价是音箱直接放音频，比漏判严重得多。核心名至少是用户
        主动说出的完整词组，风险可控。
        """
        nt = _normalize(text)
        for t, n in self._norm_pairs:
            if n and n in nt:
                return t
        for t, n in self._core_pairs:
            if n and n in nt:
                return t
        return None

    def _pinyin_match(self, text: str):
        """同音兜底：字面对不上但读音一致时命中（三体 ← 3题/三提/山体）。

        与字面匹配同语义：书名/核心名的完整拼音串是候选文本拼音串的
        子串才算命中，不做截断。pypinyin 缺席时直接返回 None。
        """
        if not _HAS_PINYIN or not self._py_pairs:
            return None
        py = "".join(lazy_pinyin(_normalize(text)))
        if not py:
            return None
        for t, tpy in self._py_pairs:
            if tpy in py:
                return t
        return None

    def match(self, text: str):
        """返回 (title, mode) 或 None。mode ∈ {"resume", "new"}。"""
        if not text or not self.titles:
            return None

        cleaned = _clean(text)

        if not any(p.search(text) for p in _PLAY_PATTERNS):
            # 没有播放动词时，只有「整句就是书名」才认（「三体吧」「三题吧」）
            # ——大模型问过「想听哪本」后，用户答书名是自然回话。
            # 「三体讲了什么」这种整句对不上书名，仍然放给 LLM。
            if cleaned:
                nc = _normalize(cleaned)
                hit = self._exact_map.get(nc)
                if not hit and _HAS_PINYIN and self._exact_py:
                    hit = self._exact_py.get("".join(lazy_pinyin(nc)))
                if hit:
                    mode = (
                        "new"
                        if any(p.search(text) for p in _NEW_PATTERNS)
                        else "resume"
                    )
                    return hit, mode
            return None

        mode = "new" if any(p.search(text) for p in _NEW_PATTERNS) else "resume"

        # 1) 清洗后的片段做精确/前缀匹配（最严格，误判风险最低）
        if cleaned:
            for t in self.titles:
                nc = _normalize(cleaned)
                if nc == _normalize(t) or nc.startswith(_normalize(t)):
                    return t, mode

        # 2) 清洗后的片段里宽松搜书名
        if cleaned:
            hit = self._match_title(cleaned)
            if hit:
                return hit, mode

        # 3) 原始文本搜书名（ASR 识别错字时 cleaned 会空）
        hit = self._match_title(text)
        if hit:
            return hit, mode

        # 4) 同音兜底：字面全错但读音对得上（ASR 把「三体」写成「3题」）
        for probe in (cleaned, text):
            if not probe:
                continue
            hit = self._pinyin_match(probe)
            if hit:
                return hit, mode

        # 播放意图明确但书名没匹上——大概率 ASR 又写错字了。
        # 记一条，方便按真实变体回填 _HOMOPHONE_FIX。
        # （音乐请求也会走到这里，属正常：它们归音乐匹配器管。）
        logger.info(
            "[audiobook] 播放意图未匹配到有声书名: text=%r cleaned=%r", text, cleaned
        )
        return None
