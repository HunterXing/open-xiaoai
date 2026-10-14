#!/usr/bin/env python3
"""音乐意图匹配器测试：对着**真实** Navidrome 曲库跑，正反例都验。

用法：
    cd server && set -a; . .env; set +a; python3 tests/test_music_intent.py
"""

import hashlib
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugins"))
from music_intent import MusicIntent  # noqa: E402

BASE = os.environ["NAVIDROME_URL"].rstrip("/")
USER = os.environ["NAVIDROME_USER"]
PASSWORD = os.environ["NAVIDROME_PASSWORD"]


def _url(endpoint, **params):
    salt = secrets.token_hex(8)
    token = hashlib.md5((PASSWORD + salt).encode()).hexdigest()
    q = {"u": USER, "t": token, "s": salt, "v": "1.16.1", "c": "test", "f": "json", **params}
    return f"{BASE}/rest/{endpoint}?" + urllib.parse.urlencode(q)


def _get(endpoint, **params):
    with urllib.request.urlopen(_url(endpoint, **params), timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))["subsonic-response"]


class NavidromeResolver:
    """真实解析器：search3 搜曲 + getRandomSongs 随机。"""

    def search(self, query, limit=20):
        body = _get("search3.view", query=query, songCount=limit)
        return ((body.get("searchResult3") or {}).get("song")) or []

    def random_songs(self, limit=3):
        body = _get("getRandomSongs.view", size=limit)
        return ((body.get("randomSongs") or {}).get("song")) or []


POSITIVE = [
    ("播放音乐", lambda r: r and r["kind"] == "random"),
    ("放首歌", lambda r: r and r["kind"] == "random"),
    ("来点音乐", lambda r: r and r["kind"] == "random"),
    ("随机播放", lambda r: r and r["kind"] == "random"),
    ("播放海阔天空", lambda r: r and r["kind"] == "song"),
    ("放一下消愁", lambda r: r and r["kind"] == "song"),
    ("我想听平凡的一天", lambda r: r and r["kind"] in ("song", "artist")),
    # 泛化判定若看拆出来的尾段「歌」，这条会被误判成随机播放
    ("播放陈奕迅的歌", lambda r: r and r["kind"] in ("artist", "song")),
    ("播放毛不易的消愁", lambda r: r and r["kind"] in ("song", "artist")),
    ("播放BEYOND", lambda r: r is not None),
    ("来一首海阔天空", lambda r: r is not None),
]

# 这些**绝不能**被音乐拦截，必须交回 LLM
NEGATIVE = [
    "三体讲了什么",
    "今天合肥天气怎么样",
    "陈奕迅是谁",
    "播放新闻",
    "播放天气预报",
    "小智现在有点忙",
    "帮我把灯关了",
    "明天几点开会",
    "我想听你说说今天发生了什么",
]


def main():
    r = NavidromeResolver()
    m = MusicIntent(r)
    fails = 0
    print("=" * 66)
    print("正例（应命中）")
    for text, check in POSITIVE:
        got = m.match(text)
        ok = bool(check(got))
        fails += 0 if ok else 1
        if got:
            first = got["songs"][0]
            print(f"  {'✓' if ok else '✗'} {text!r:24} → {got['kind']:6} "
                  f"{len(got['songs'])} 首 | {first.get('artist')} - {first.get('title')}")
        else:
            print(f"  {'✓' if ok else '✗'} {text!r:24} → None")
    print("反例（必须 None）")
    for text in NEGATIVE:
        got = m.match(text)
        ok = got is None
        fails += 0 if ok else 1
        detail = ""
        if got:
            first = got["songs"][0]
            detail = f" ← 误拦！{got['kind']} {first.get('artist')} - {first.get('title')}"
        print(f"  {'✓' if ok else '✗'} {text!r:24} → {'None' if ok else 'HIT'}{detail}")
    print("=" * 66)
    print(f"失败 {fails} 项 / 共 {len(POSITIVE) + len(NEGATIVE)} 项")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
