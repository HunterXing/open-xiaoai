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
"""

import re

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

# ASR 常见同音/近音错字 → 正确写法。实测「播放3天」被 ASR 听成三体。
_HOMOPHONE_FIX = {
    "3天": "三体", "三天": "三体", "仨体": "三体", "3提": "三体",
    "三提": "三体", "山体": "三体", "散体": "三体", "3体": "三体",
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


def _normalize(text: str) -> str:
    """去掉书名号/括号等 ASR 不会读出的字符，用于书名比较。"""
    return _IGNORED_CHARS.sub("", text)


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

    def match(self, text: str):
        """返回 (title, mode) 或 None。mode ∈ {"resume", "new"}。"""
        if not text or not self.titles:
            return None
        if not any(p.search(text) for p in _PLAY_PATTERNS):
            return None

        mode = "new" if any(p.search(text) for p in _NEW_PATTERNS) else "resume"

        # 1) 清洗后的片段做精确/前缀匹配（最严格，误判风险最低）
        cleaned = _clean(text)
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

        return None
