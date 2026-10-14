#!/usr/bin/env python3
"""生成 core/connection.py 的有声书意图预判补丁。

从容器内原文件读起，注入两处代码后整文件写回宿主。
用 python 整块替换而不是多次小 patch——1852 行的大文件局部叠加
容易把改动搞乱（见 dev-discipline-superpowers 铁律 3）。

两处注入：
  A) llm_responses 之前：命中意图就自己造 tool_call，跳过 LLM
  B) 文件末尾：意图匹配器（惰性加载，书名清单缓存 10 分钟）
"""

import re
import sys

SRC = "patches/connection.py.orig"
DST = "plugins/connection_audiobook_patch.py"

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
'''

HELPERS = '''

    def _audiobook_match(self, text):
        """返回 (title, mode) 或 None。匹配器惰性构建、书名清单缓存 10 分钟。"""
        global _AUDIOBOOK_MATCHER, _AUDIOBOOK_TITLES, _AUDIOBOOK_TITLES_AT
        import time
        if _AUDIOBOOK_MATCHER is None or time.time() - _AUDIOBOOK_TITLES_AT > 600:
            try:
                titles = _load_audiobook_titles()
                _AUDIOBOOK_TITLES = titles
                _AUDIOBOOK_TITLES_AT = time.time()
                _AUDIOBOOK_MATCHER = AudiobookIntent(titles)
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"有声书意图匹配器初始化失败: {e}")
                return None
        return _AUDIOBOOK_MATCHER.match(text)
'''

LOADER = '''

def _load_audiobook_titles():
    """从 BookOrbit 库取有声书书名。取不到就退回目录扫描。"""
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
            if _ab_hit is not None and functions:
                _ab_title, _ab_mode = _ab_hit
                self.logger.bind(tag=TAG).info(
                    f"有声书意图预判命中：《{_ab_title}》mode={_ab_mode}（跳过 LLM）"
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
                llm_responses = iter([(None, [_ab_tool_call])])
            elif self.intent_type == "function_call" and functions is not None:
                # 使用支持functions的streaming接口
                llm_responses = self.llm.response_with_functions('''

# ---- 注入 B：实例方法 + 模块级加载器 ----
ANCHOR_B = "    def _merge_tool_calls(self, tool_calls_list, tools_call):"


def main():
    with open(SRC, encoding="utf-8") as f:
        text = f.read()

    # 导入 bookorbit_intent
    import_anchor = "TAG = __name__"
    assert text.count(import_anchor) >= 1, "TAG anchor not found"
    text = text.replace(
        import_anchor,
        import_anchor + "\n"
        "try:\n"
        "    from plugins_func.functions.bookorbit_intent import AudiobookIntent\n"
        "except Exception:  # 预判不可用时降级为纯 LLM 对话，不影响其它功能\n"
        "    AudiobookIntent = None",
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
