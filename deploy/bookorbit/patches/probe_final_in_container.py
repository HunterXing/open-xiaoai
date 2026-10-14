"""容器内验证补丁依赖的模块级名字都存在，且预判链路可跑通。

这次吸取教训：不复刻代码（复刻会掩盖真实的 import 缺失），
直接 import 容器内真的 core.connection，检查真模块的真实属性。
"""

import json
import sys
import types

sys.path.insert(0, "/opt/xiaozhi-esp32-server")

# 不要桩 plugins_func.register —— plugin_executor 需要它的
# all_function_registry / module_func_map / Action / ActionResponse，
# 桩掉会导致 connection.py 的 import 链断掉（实测报
# "cannot import name 'all_function_registry'"）。
# 用真的，让整个 import 链按生产路径走。

import core.connection as C

print("1) 模块级名字可用性（缺任何一个都会在运行时炸）")
for name in ("SimpleNamespace", "AudiobookIntent", "json"):
    ok = hasattr(C, name) and getattr(C, name) is not None
    print(f"   {'✓' if ok else '✗'} core.connection.{name} = {getattr(C, name, '<MISSING>')}")
    assert ok, f"{name} 不可用"

for fn in ("_load_audiobook_titles",):
    ok = callable(getattr(C, fn, None))
    print(f"   {'✓' if ok else '✗'} {fn}()")
    assert ok

print()
print("2) 书名清单（真查 BookOrbit）")
titles = C._load_audiobook_titles()
print(f"   拿到 {len(titles)} 本")
assert len(titles) > 0, "书名清单为空"
print("   前 3 本:", titles[:3])

print()
print("3) 意图匹配器（真实例）")
h = C.ConnectionHandler.__new__(C.ConnectionHandler)
# 桩掉 logger，避免依赖会话状态
h.logger = types.SimpleNamespace(
    bind=lambda **kw: types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None))
h._audiobook_match.__func__.__globals__  # 触发属性访问确认存在

# 正例用库里真实存在的书名（书名与「播放」拼起来应命中）
probe_title = titles[0]
hit = h._audiobook_match(f"播放{probe_title}")
print(f"   '播放{probe_title}' → {hit}")
assert hit is not None and hit[0] == probe_title, f"匹配结果不对: {hit}"

# 反例：同一个名字出现在提问句里，不能被拦截
miss = h._audiobook_match(f"{probe_title}讲了什么")
print(f"   '{probe_title}讲了什么' → {miss}")
assert miss is None, "普通提问不应被拦截"

print()
print("4) 端到端：造 tool_call → 真 _merge_tool_calls 解包")
args = json.dumps({"title": hit[0], "mode": hit[1]}, ensure_ascii=False)
tc_obj = C.SimpleNamespace(
    index=0, id="call_audiobook", type="function",
    function=C.SimpleNamespace(name="play_audiobook", arguments=args),
)
tool_calls_list = []
h._merge_tool_calls(tool_calls_list, [tc_obj])
print("   ", tool_calls_list)
assert tool_calls_list[0]["name"] == "play_audiobook"
assert json.loads(tool_calls_list[0]["arguments"])["title"] == hit[0]

print()
print("✓ 容器内全链路验证通过（真模块 / 真查库 / 真解包）")
