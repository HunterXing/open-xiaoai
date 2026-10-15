#!/usr/bin/env python3
"""生成 core/connection.py 的意图预判补丁（有声书 + 音乐库）。

从容器内原文件读起，注入两处代码后整文件写回宿主。
用 python 整块替换而不是多次小 patch——1852 行的大文件局部叠加
容易把改动搞乱（见 dev-discipline-superpowers 铁律 3）。

两处注入：
  A) llm_responses 之前：命中意图就自己造 tool_call，跳过 LLM
  B) 文件末尾：意图匹配器（惰性加载；书名清单缓存 10 分钟，曲库现查）

命中顺序：有声书 → 音乐库 → LLM。有声书是纯文本匹配（快、零请求），
音乐库要问一次搜索接口，所以放后面，命中不了再交回 LLM。
"""

import re
import sys

SRC = "patches/connection.py.orig"
DST = "plugins/connection_intent_patch.py"

PRELUDE = '''
# ---- BookOrbit 有声书意图预判（补丁起点）--------------------------------
# 为什么需要这层：Hermes 的 api_server 建 agent 时用自己 config 的
# toolsets，请求 body 里的 tools 只进了请求指纹用于幂等去重，从没传给
# agent。所以 xiaozhi 通过 response_with_functions(functions=...) 下发的
# 插件函数列表，Hermes 侧永远看不到——实测 tool_choice=required 也一个
# tool_call 都不产出，模型只会去 tool_search 搜「play audiobook」然后放弃。
# LLM 选工具这条路在 Hermes 后端上是断的，改成确定性前置匹配。
_AUDIOBOOK_MATCHER = None
_AUDIOBOOK_TITLES = None
_AUDIOBOOK_TITLES_AT = 0.0
_MUSIC_MATCHER = None


def _intent_unwrap(text):
    """把 ASR 的 JSON 外壳剥掉，取出真正的用户话语。

    真机实测的入参长这样（不是纯文本）：
        {"content": "播放音乐。", "language": "zh", "emotion": "😶"}
    不解包的话匹配器会把整串拿去搜曲库，永远搜不到，用户听到的就是
    「我放不了」。非 JSON 或没有 content 字段就原样返回。
    """
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
        except Exception:
            return text
        if isinstance(data, dict) and isinstance(data.get("content"), str):
            return data["content"]
    return text
'''

HELPERS = '''

    def _audiobook_match(self, text):
        """返回 (title, mode) 或 None。匹配器惰性构建、书名清单缓存 10 分钟。"""
        global _AUDIOBOOK_MATCHER, _AUDIOBOOK_TITLES, _AUDIOBOOK_TITLES_AT
        import time
        text = _intent_unwrap(text)
        if _AUDIOBOOK_MATCHER is None or time.time() - _AUDIOBOOK_TITLES_AT > 600:
            try:
                titles = _load_audiobook_titles()
                _AUDIOBOOK_TITLES = titles
                _AUDIOBOOK_TITLES_AT = time.time()
                _AUDIOBOOK_MATCHER = AudiobookIntent(titles)
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"有声书意图匹配器初始化失败: {e}")
                return None
        try:
            return _AUDIOBOOK_MATCHER.match(text)
        except Exception as e:
            # 匹配异常必须留痕：静默 None 的结果是用户听到「我放不了」，
            # 而日志里一个字都没有（真机上就这么错过一次）。
            self.logger.bind(tag=TAG).warning(f"有声书意图匹配异常，交回 LLM: {e}")
            return None

    def _music_match(self, text):
        """返回 dict(kind/label/songs) 或 None。解析器惰性构建（不用缓存：
        曲库靠搜索接口现查，比冷清单准确）。命中不了就返回 None 交回 LLM。"""
        global _MUSIC_MATCHER
        text = _intent_unwrap(text)
        if _MUSIC_MATCHER is None:
            try:
                _MUSIC_MATCHER = MusicIntent(_load_music_resolver())
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"音乐意图匹配器初始化失败: {e}")
                return None
        try:
            return _MUSIC_MATCHER.match(text)
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"音乐意图匹配异常，交回 LLM: {e}")
            return None
'''

LOADER = '''

def _load_audiobook_titles():
    """从 BookOrbit 库取有声书书名（library_id=4）。取不到就退回目录扫描。"""
    import os
    try:
        import psycopg2
        pg = psycopg2.connect(
            host=os.environ["BOOKORBIT_PG_HOST"],
            dbname=os.environ["BOOKORBIT_PG_DB"],
            user=os.environ["BOOKORBIT_PG_USER"],
            password=os.environ["BOOKORBIT_PG_PASSWORD"],
            connect_timeout=5,
        )
        pg.autocommit = True
        try:
            with pg.cursor() as cur:
                cur.execute(
                    "SELECT folder_path FROM books "
                    "WHERE library_id = %s AND status = 'present' ORDER BY folder_path",
                    (int(os.environ.get("BOOKORBIT_LIBRARY_ID", 4)),),
                )
                rows = [r[0].rsplit("/", 1)[-1] for r in cur.fetchall()]
                if rows:
                    return rows
        finally:
            pg.close()
    except Exception:
        pass
    # 退化路径：直接扫挂载目录，ASR 错字纠正仍能兜住常用书名
    root = os.environ.get("BOOKORBIT_TITLES_FALLBACK_DIR", "/books")
    try:
        return sorted(
            d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))
        )
    except Exception:
        return []


def _load_music_resolver():
    """音乐库解析器：Subsonic 搜索 + 随机取曲。

    不做缓存——曲库靠搜索接口现查，比拿一份冷清单准；
    单次查询十几毫秒，比误判便宜得多（缓存清单反而会因重排而变旧）。
    """
    import hashlib
    import json as _json
    import os
    import secrets
    import urllib.parse
    import urllib.request

    class _NavidromeResolver:
        def __init__(self):
            self.base = os.environ["NAVIDROME_URL"].rstrip("/")

        def _call(self, endpoint, **params):
            # Subsonic token 鉴权：口令不进 URL，也不落日志
            salt = secrets.token_hex(8)
            token = hashlib.md5(
                (os.environ["NAVIDROME_PASSWORD"] + salt).encode("utf-8")
            ).hexdigest()
            query = {
                "u": os.environ["NAVIDROME_USER"],
                "t": token,
                "s": salt,
                "v": "1.16.1",
                "c": "xiaozhi",
                "f": "json",
                **params,
            }
            url = f"{self.base}/rest/{endpoint}?" + urllib.parse.urlencode(query)
            # 超时压短：这是对话请求路径上的一步，不能把回复卡住
            with urllib.request.urlopen(url, timeout=4) as resp:
                return _json.loads(resp.read().decode("utf-8"))["subsonic-response"]

        def search(self, query, limit=20):
            body = self._call("search3.view", query=query, songCount=limit)
            return ((body.get("searchResult3") or {}).get("song")) or []

        def random_songs(self, limit=3):
            body = self._call("getRandomSongs.view", size=limit)
            return ((body.get("randomSongs") or {}).get("song")) or []

    return _NavidromeResolver()
'''

# ---- 注入 A：LLM 之前命中则直接造 tool_call ----
ANCHOR_A = '''            if self.intent_type == "function_call" and functions is not None:
                # 使用支持functions的streaming接口
                llm_responses = self.llm.response_with_functions('''

INJECT_A = '''            # BookOrbit 有声书意图预判：命中就自己造 tool_call，不问 LLM。
            # 放在 LLM 之前是因为 Hermes 侧收不到 functions，让 LLM 选工具
            # 只会得到一句「我放不了」；这里确定性地把它变成播放指令。
            #
            # 形状必须是 **OpenAI 流式 chunk**，不能直接塞 tool_call 字典：
            # 下面按 `content, tools_call = response`（1159 行）解包每个 yield，
            # 其中 tools_call 走 `tools_call.get("function", {})` 读出
            # name/arguments，再经 _merge_tool_calls 入队。直接给 dict 会让
            # response.get("choices") 取空、对 None 做 >= 而抛
            # "'>=' not supported between builtin_function_or_method and int"。
            _ab_hit = self._audiobook_match(query or "")
            _hit_tool_call = None
            if _ab_hit is not None and functions:
                _ab_title, _ab_mode = _ab_hit
                _ab_label = (
                    "最近在听" if _ab_title == "__recent__" else f"《{_ab_title}》"
                )
                self.logger.bind(tag=TAG).info(
                    f"有声书意图预判命中：{_ab_label} mode={_ab_mode}（跳过 LLM）"
                )
                _ab_args = json.dumps(
                    {"title": _ab_title, "mode": _ab_mode}, ensure_ascii=False
                )
                # 形状必须对齐 openai.py:167-170 的 `yield content, tool_calls`：
                #   content    = delta.content（字符串或 None）
                #   tools_call = delta.tool_calls —— **裸列表**，不是 chunk 包装
                # 下游 `for tool_call in tools_call:` 直接迭代这个列表，
                # 元素须有 .index / .id / .function.name / .function.arguments
                # 属性（_merge_tool_calls 用 getattr 读属性，不取键）。
                # 包一层 .choices 会让迭代到包装对象，getattr 取不到 index，
                # 退化分支把 dict 的 .function 当内置方法，`>=` 抛 TypeError。
                _ab_tool_call = SimpleNamespace(
                    index=0,
                    id="call_audiobook",
                    type="function",
                    function=SimpleNamespace(
                        name="play_audiobook",
                        arguments=_ab_args,
                    ),
                )
                _hit_tool_call = _ab_tool_call
            # Navidrome 音乐库意图预判：接在有声书之后。
            # 与有声书不同，这里的「命中」是先问过音乐库的——搜索接口搜不到
            # 就返回 None 交回 LLM（曲库近千首，不可能像书名那样全塞进内存
            # 做文本匹配；搜索即校验，代价是十几毫秒的本地请求）。
            # 有声书优先：两边都可能命中时（比如书名撞上歌名）以有声书为准。
            _mu_hit = (
                self._music_match(query or "")
                if _ab_hit is None and functions
                else None
            )
            if _mu_hit is not None:
                _mu_songs = _mu_hit.get("songs") or []
                self.logger.bind(tag=TAG).info(
                    f"音乐库意图预判命中：{_mu_hit.get('label')}"
                    f"（{len(_mu_songs)} 首，跳过 LLM）"
                )
                _mu_args = json.dumps(
                    {
                        "kind": _mu_hit.get("kind", "song"),
                        "label": _mu_hit.get("label", ""),
                        "songs": [
                            {
                                "id": s.get("id"),
                                "title": s.get("title"),
                                "artist": s.get("artist"),
                                "duration": s.get("duration"),
                            }
                            for s in _mu_songs
                        ],
                    },
                    ensure_ascii=False,
                )
                _mu_tool_call = SimpleNamespace(
                    index=0,
                    id="call_music_library",
                    type="function",
                    function=SimpleNamespace(
                        name="play_music_library",
                        arguments=_mu_args,
                    ),
                )
                _hit_tool_call = _mu_tool_call
            #
            # 链条注意：命中判定走「单一 _hit_tool_call + 末尾 if/elif」。
            # 别改成两个各自独立的 if——那样后面那个的 elif 会抢走 LLM 分支，
            # 有声书命中时仍会真实调用 LLM 并覆盖预判结果（预判白做）。
            if _hit_tool_call is not None:
                llm_responses = iter([(None, [_hit_tool_call])])
            elif self.intent_type == "function_call" and functions is not None:
                # 使用支持functions的streaming接口
                llm_responses = self.llm.response_with_functions('''

# ---- 注入 B：实例方法 + 模块级加载器 ----
ANCHOR_B = "    def _merge_tool_calls(self, tool_calls_list, tools_call):"


def main():
    with open(SRC, encoding="utf-8") as f:
        text = f.read()

    # 导入两个意图匹配器（分开 try：一个缺了不影响另一个）
    import_anchor = "TAG = __name__"
    assert text.count(import_anchor) >= 1, "TAG anchor not found"
    text = text.replace(
        import_anchor,
        import_anchor + "\n"
        "try:\n"
        "    from plugins_func.functions.bookorbit_intent import AudiobookIntent\n"
        "except Exception:  # 预判不可用时降级为纯 LLM 对话，不影响其它功能\n"
        "    AudiobookIntent = None\n"
        "try:\n"
        "    from plugins_func.functions.music_intent import MusicIntent\n"
        "except Exception:\n"
        "    MusicIntent = None",
        1,
    )

    # A: 意图预判
    assert text.count(ANCHOR_A) == 1, f"A anchor count={text.count(ANCHOR_A)}"
    text = text.replace(ANCHOR_A, INJECT_A, 1)

    # B: 挂实例方法（缩进 4 空格，属于 ConnectionHandler）
    assert text.count(ANCHOR_B) == 1, f"B anchor count={text.count(ANCHOR_B)}"
    text = text.replace(ANCHOR_B, HELPERS.rstrip() + "\n\n" + ANCHOR_B, 1)

    # PRELUDE + LOADER 放在 import 区之后、类定义之前
    assert text.count(ANCHOR_B) == 1
    marker = "\nclass ConnectionHandler"
    if marker in text:
        text = text.replace(marker, PRELUDE + LOADER + "\n" + marker, 1)
    else:
        # 退化：找不到类定义行就追加到末尾
        text = text.rstrip() + "\n" + PRELUDE + LOADER + "\n"

    # json 用于 tool_call 参数序列化
    if "\nimport json" not in text:
        text = text.replace("import sys", "import sys\nimport json", 1)

    # SimpleNamespace 用于伪造 OpenAI 形状的 tool_call 对象
    # （_merge_tool_calls 读属性而非取键，必须给对象）
    #
    # 注意：判断条件必须查**完整的 import 语句**，不能查 "SimpleNamespace"——
    # 此时 INJECT_A 已注入文件，里面就含 SimpleNamespace 这个词，
    # 用词判断会永远为假、import 加不进去（实测踩过：
    # `name 'SimpleNamespace' is not defined`）。
    if "from types import SimpleNamespace" not in text:
        assert "import json" in text, "json import anchor missing"
        text = text.replace(
            "import json",
            "import json\nfrom types import SimpleNamespace",
            1,
        )
    assert "from types import SimpleNamespace" in text

    with open(DST, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {DST}")

    # 语法自检
    import ast
    ast.parse(text)
    print("syntax OK")

    # 大括号/圆括号配平（铁律 4：括号数数法）
    for ch, name in (("{", "brace"), ("(", "paren")):
        print(f"  {name}: {text.count(ch)} open / "
              f"{text.count('}' if ch == '{' else ')')} close")


if __name__ == "__main__":
    sys.exit(main())
