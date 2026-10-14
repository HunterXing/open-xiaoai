"""音乐库意图预判 —— 在 xiaozhi 调 LLM 之前拦一道。

为什么需要它
------------
与 bookorbit_intent 同源：Hermes 的 api_server 建 agent 时用自己 config 的
toolsets，请求 body 里的 `tools` 只进请求指纹用于幂等去重，从没传给 agent。
所以插件函数列表 LLM 侧永远看不到，function call 这条路是断的，
只能在调 LLM 之前做确定性匹配。

与有声书的区别
--------------
有声书只有二十来本，可以把书名全装进匹配器做纯文本判断。
音乐库近千首、且用户会说歌名/歌手/专辑/半句歌词，全量装进内存不合适，
所以改成 **搜索即校验**：把清洗后的候选丢给音乐库的搜索接口，
只有真搜到、且相似度够高才拦截；搜不到一律不拦，交回 LLM 正常回答。

这样做的代价是多一次本地 HTTP（十几毫秒），换来的是零误判——
「播放新闻」搜不到就不会被当成放歌。

设计约束
--------
1. **只拦明确命中**：误判会直接出声，比漏判危险得多。
2. 泛化请求（「播放音乐」「放首歌」）不需要搜索，直接随机。
3. 匹配器不自己发请求：resolver 由外部注入，便于离线测试。
"""

import difflib
import re

# 播放类动作词。没有动作词一律不拦——「陈奕迅是谁」不能变成放歌。
_ACTION = re.compile(r"播放|播|放|来|听|点|换|唱")

# 泛化词：说了等于没说，直接随机播。整段清洗后只剩这些词才算泛化。
_GENERIC = re.compile(
    r"^(?:音乐|歌曲|歌儿|歌|曲子|曲|声|点歌|首歌|一些歌|点音乐|背景音乐|"
    r"随便|随意|随机|什么|啥|都行|都可以)+$"
)

# 清洗时去掉的口头语/量词
_NOISE = [
    "帮我", "给我", "我要", "我想", "麻烦", "现在", "然后", "接着",
    "一下", "一点", "一些", "一个", "一首", "几首", "点", "首", "个",
    "吧", "呢", "啊", "呗", "嘛", "哦",
]

# 归一化忽略的字符（ASR 不会读出的书名号/括号/空白/常见标点）
_IGNORED = re.compile(r"[《》「」『』（）()\[\]【】·、，。！？,.!?~～\s'\"‘’“”-]")


def _norm(text: str) -> str:
    return _IGNORED.sub("", (text or "").lower())


# 坏标签：库里确实混着这类记录，随机抽到会破坏体验，挑随机曲时避开
_BAD_ARTIST = re.compile(r"^\[?unknown", re.I)
_BAD_TITLE = re.compile(r"^track\s*\d+$|^\d+\s*-\s*unknown|^unknown|^未知", re.I)


def _well_tagged(song: dict) -> bool:
    """标签像样的才算好曲目。全是坏标签时调用方会退回未过滤结果。"""
    artist = (song.get("artist") or "").strip()
    title = (song.get("title") or "").strip()
    if not artist or not title:
        return False
    return not (_BAD_ARTIST.match(artist) or _BAD_TITLE.match(title))


def _clean(raw: str) -> str:
    """剥掉动作词与口头语，留下「用户想听的那段词」。"""
    text = raw.strip()
    for p in ("播放", "放一下", "放点", "来点", "来一首", "来首", "我想听", "我想",
              "我要听", "我要", "帮我", "给我", "听一下", "听点", "点一首", "点首",
              "换一首", "换首", "放", "来", "听", "点", "播"):
        text = text.replace(p, " ")
    for n in _NOISE:
        text = text.replace(n, " ")
    # 只剩「的」也去掉：「陈奕迅的歌」→「陈奕迅」
    text = re.sub(r"[，。！？、,.!?~]", " ", text)
    return " ".join(text.split()).strip()


def _candidates(raw: str):
    """给出若干候选查询词，按「整串 → 尾段 → 头段 → 头尾拼接」的顺序。

    搜索接口是**整串子串匹配**（实测 search3("陈奕迅的歌") 返回 0 首），
    所以「A的B」必须拆开才有机会命中；尾段（歌名）比头段（歌手）更具体，
    因此尾段优先。
    """
    out = []

    def push(v):
        v = (v or "").strip()
        if v and v not in out:
            out.append(v)

    cleaned = _clean(raw)
    push(cleaned)
    if "的" in cleaned:
        head, _, tail = cleaned.partition("的")
        push(tail)
        push(head)
        push(f"{head} {tail}")
    push(re.sub(r"[的地得了]$", "", cleaned).strip())
    return out


def _searchable(cand: str) -> bool:
    """这个候选值不值得拿去搜。

    实测 search3("七") 会返回「七年滋养」这种不相关结果，
    所以单字查询和纯泛化词直接跳过——宁可不拦，也不能放错歌。
    """
    n = _norm(cand)
    return len(n) >= 2 and not _GENERIC.match(n)


def _score(query: str, song: dict) -> float:
    """候选词与一首歌的相似度。0 表示不像，1 表示完全一致。"""
    q = _norm(query)
    if not q:
        return 0.0
    best = 0.0
    for field in ("title", "artist", "album"):
        f = _norm(song.get(field) or "")
        if not f:
            continue
        if q == f:
            best = max(best, 1.0)
        elif q in f:
            # 候选是字段的一部分：「海阔天空」命中「海阔天空(Live)」
            best = max(best, 0.95)
        elif f in q and len(f) >= 3:
            # 字段是候选的一部分：「陈奕迅的七」里的「陈奕迅」
            best = max(best, 0.9)
        else:
            # 模糊匹配打折——ASR 错字能兜住，但别让弱相关混进来
            best = max(best, difflib.SequenceMatcher(None, q, f).ratio() * 0.85)
    return best


class MusicIntent:
    """匹配器。resolver 注入，运行时不自己发请求（便于离线测试）。

    resolver 需要提供两个方法：
        search(query, limit) -> [song dict]      按关键词搜
        random_songs(limit)  -> [song dict]      随机取
    song dict 至少含 id / title / artist / duration。
    """

    def __init__(self, resolver, min_score: float = 0.7,
                 random_batch: int = 3, artist_batch: int = 5):
        self.resolver = resolver
        self.min_score = min_score
        self.random_batch = random_batch
        self.artist_batch = artist_batch

    # ---- 对外入口 ----
    def match(self, text: str):
        """返回 dict(kind/label/songs) 或 None。None = 不拦，交给 LLM。"""
        if not text or not _ACTION.search(text):
            return None

        cands = _candidates(text)

        # 1) 泛化请求：只看**主候选**（清洗后的整段），直接随机。
        #    不能逐个候选判——「陈奕迅的歌」会拆出尾段「歌」，
        #    按它判泛化就把「点播歌手」误判成「随机播放」了（实测踩过）。
        if _GENERIC.match(_norm(_clean(text))):
            picked = self._random_pick()
            if picked:
                return {"kind": "random",
                        "label": f"随机播放 {len(picked)} 首",
                        "songs": picked}
            return None

        # 2) 具体请求：搜索即校验
        for c in cands:
            if not _searchable(c):
                continue
            songs = self._safe(self.resolver.search, c)
            if not songs:
                continue
            best, hit_kind = self._best(c, songs)
            if best is None:
                continue
            if hit_kind == "artist":
                # 歌手命中：把该歌手的其它歌一起排上
                artist = best.get("artist") or c
                more = [s for s in songs
                        if _norm(s.get("artist") or "") == _norm(artist)]
                picked = more[: self.artist_batch] or [best]
                return {"kind": "artist", "label": f"{artist} 的 {len(picked)} 首",
                        "songs": picked}
            return {"kind": "song",
                    "label": f"{best.get('artist', '')} 的 {best.get('title', '')}".strip(),
                    "songs": [best]}
        return None

    # ---- 内部 ----
    @staticmethod
    def _safe(fn, *args):
        try:
            return fn(*args) or []
        except Exception:
            # 音乐库不可用 = 不拦，让 LLM 正常回答（不能因为库挂了就说不出话）
            return []

    def _random_pick(self):
        """随机挑曲：多取几首后避开坏标签；全是坏标签就退回未过滤结果。"""
        raw = self._safe(self.resolver.random_songs,
                         max(self.random_batch * 4, 12))
        good = [s for s in raw if _well_tagged(s)]
        return (good or raw)[: self.random_batch]

    def _best(self, query: str, songs):
        """在候选结果里挑最像的一首，并判断是命中歌名还是歌手。"""
        ranked = []
        for s in songs:
            score = _score(query, s)
            if score < self.min_score:
                continue
            # 同名歌很多时优先歌手也像的
            title_like = _score(query, {"title": s.get("title")})
            artist_like = _score(query, {"artist": s.get("artist")})
            kind = "artist" if artist_like > title_like else "song"
            ranked.append((score, kind, s))
        if not ranked:
            return None, None
        ranked.sort(key=lambda r: r[0], reverse=True)
        _, kind, song = ranked[0]
        return song, kind
