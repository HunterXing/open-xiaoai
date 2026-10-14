"""容器内验证音乐库链路：真模块 / 真搜索 / 真下载 / 真解码。

跑法（脚本从 stdin 进容器，不需要额外挂载）：
    docker exec -i xiaozhi-esp32-server python3 - < patches/probe_music_in_container.py

为什么在容器里跑：音乐解析器依赖 NAVIDROME_* 环境变量和容器内的网络，
在宿主上验证通不代表容器里通（这在有声书那轮已经吃过教训）。
"""

import asyncio
import json
import sys
import types

sys.path.insert(0, "/opt/xiaozhi-esp32-server")

import core.connection as C  # noqa: E402

FAIL = []


def check(label, ok, detail=""):
    print(f"   {'✓' if ok else '✗'} {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(label)


print("1) 模块级名字（缺一个都会在运行时炸）")
for name in ("MusicIntent", "_load_music_resolver"):
    obj = getattr(C, name, None)
    check(f"core.connection.{name}", obj is not None, repr(obj)[:60])

# 真实例，但绕过 __init__：只需要方法本身，不需要整个会话状态
h = C.ConnectionHandler.__new__(C.ConnectionHandler)
h.logger = types.SimpleNamespace(
    bind=lambda **kw: types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None))

print()
print("2) 意图匹配（真查音乐库，验证容器内网络可达）")
cases = [
    ("播放音乐", True),
    ("放首歌", True),
    ("播放海阔天空", True),
    ("播放消愁", True),
    ("播放陈奕迅的歌", True),
    ("三体讲了什么", False),
    ("今天合肥天气怎么样", False),
    ("播放新闻", False),
    ("帮我把灯关了", False),
]
hit_song = None
for text, should in cases:
    got = h._music_match(text)
    ok = (got is not None) == should
    label = ""
    if got:
        label = f"{got['kind']} / {got.get('label')}"
        if hit_song is None and got["kind"] == "song":
            hit_song = got
    check(f"{text!r} → {'命中' if got else 'None'}", ok, label)

print()
print("3) tool_call 形状（必须能被真 _merge_tool_calls 解包）")
sample = hit_song or h._music_match("播放音乐")
args = json.dumps(
    {"kind": sample["kind"], "label": sample["label"],
     "songs": [{"id": s["id"], "title": s.get("title"),
                "artist": s.get("artist"), "duration": s.get("duration")}
               for s in sample["songs"]]},
    ensure_ascii=False)
tc = C.SimpleNamespace(
    index=0, id="call_music_library", type="function",
    function=C.SimpleNamespace(name="play_music_library", arguments=args))
merged = []
h._merge_tool_calls(merged, [tc])
check("解包后 name", merged and merged[0]["name"] == "play_music_library",
      str(merged[0]["name"]) if merged else "—")
check("解包后 arguments 可解析",
      bool(json.loads(merged[0]["arguments"]).get("songs")),
      f"{len(json.loads(merged[0]['arguments'])['songs'])} 首")

print()
print("4) 真跑插件（假 conn + 真下载）")
from plugins_func.functions.play_music_library import play_music_library  # noqa: E402


class _Queue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


class _FakeConn:
    sentence_id = "probe"
    intent_type = "intent_llm"

    def __init__(self):
        self.tts = types.SimpleNamespace(tts_text_queue=_Queue())
        self.logger = h.logger


conn = _FakeConn()
# 只取第一首，避免为大文件白等
one = {"kind": sample["kind"], "label": sample["label"],
       "songs": [sample["songs"][0]]}
resp = asyncio.run(play_music_library(
    conn, songs=one["songs"], kind=one["kind"], label=one["label"]))
check("插件返回", resp is not None and "play" in str(getattr(resp, "result", "")),
      str(getattr(resp, "result", resp)))

texts, files = [], []
for item in conn.tts.tts_text_queue.items:
    ct = getattr(item, "content_type", None)
    if str(ct) == "ContentType.TEXT":
        texts.append(item.content_detail)
    elif str(ct) == "ContentType.FILE":
        files.append(item.content_file)
print(f"   播报文本: {texts}")
print(f"   入队音频: {files}")
check("播报文案非空", bool(texts and texts[0].strip()), repr(texts[:1]))
check("文案内部无标点（切句坑）",
      all(t.count("。") <= 1 and not any(p in t for p in "，、！？；：~")
          for t in texts), "结尾句号最多一个")
check("音频已入队", len(files) == 1, f"{len(files)} 个文件")

print()
print("5) 下载物可解码（用服务端自己的解码链路）")
if files:
    import os

    path = files[0]
    check("文件存在且非空",
          os.path.exists(path) and os.path.getsize(path) > 100_000,
          f"{os.path.getsize(path)/1e6:.1f}MB" if os.path.exists(path) else "缺失")
    from core.utils.util import audio_to_data_stream

    chunks = []

    def cb(data):
        if data is not None:
            chunks.append(len(data) if hasattr(data, "__len__") else 1)

    audio_to_data_stream(path, is_opus=False, callback=cb, sample_rate=24000)
    total = sum(chunks)
    # 24kHz / 16bit / 单声道 → 每秒 48000 字节
    secs = total / 48000
    check("解码出 PCM", total > 0, f"{len(chunks)} 分片 / {total/1e6:.1f}MB / ≈{secs:.0f} 秒")

print()
if FAIL:
    print(f"✗ 失败 {len(FAIL)} 项: {FAIL}")
    sys.exit(1)
print("✓ 音乐库链路容器内验证通过")
