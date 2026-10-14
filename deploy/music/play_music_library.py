"""播放音乐库（Navidrome / Subsonic）里的曲目。

与 bookorbit_audiobook 同一条链路（FILE 型音频入队），差别在两点：

1. **拿音频走 HTTP**：曲目由音乐库的 Subsonic 接口按 id 流式取出（format=raw
   不转码），落到服务端 tmp/ 再入队。不用本地路径，是因为接口返回的 path
   字段可能是索引里的旧值（实测踩过：文件已重排，path 还是旧布局），
   而 id → 文件 的映射是准的。
2. **一次可能多首**：「播放音乐」这类泛化请求会挑几首随机曲，一首一首排进队列。

为什么不是让 LLM 调这个函数：Hermes 的 api_server 收不到插件函数列表，
function call 这条路是断的（详见 music_intent.py 的说明）。入口是 connection.py
里注入的确定性预判，它把解析好的曲目直接塞进 tool_call 参数。
"""

import os
import time
import urllib.parse
import urllib.request

from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType, ContentType
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__

# 下载落点。必须用**相对**路径：base.py 删临时文件的判断是
#     tts_file.startswith(self.output_file)
# 而配置里 output_dir 就是相对的 "tmp/"——传绝对路径永远匹配不上，
# 每个文件都会留在磁盘上（每个可能上百 MB，实测踩过）。
# 服务端进程的 CWD 是项目根目录，所以相对路径能同时被下写入队和清理两条路认。
# 若你把 output_dir 改成了别处，这里跟着设 XIAOZHI_TMP_DIR。
TMP_DIR = os.environ.get("XIAOZHI_TMP_DIR", "tmp")
# 单曲上限，防一个异常大文件把磁盘吃满
MAX_BYTES = int(os.environ.get("NAVIDROME_MAX_BYTES", 320 * 1024 * 1024))
# 兜底清扫阈值：正常播放 base.py 播完就删，超过这个时长还躺着的就是残留
STALE_SECONDS = 6 * 3600

music_function_desc = {
    "type": "function",
    "function": {
        "name": "play_music_library",
        "description": (
            "播放音乐库里的音乐。用于「播放音乐」「放首歌」这类泛化请求，"
            "以及「播放某某歌」「播放某某的歌」这类点播。播放的是库里的原始"
            "音频文件，不是语音合成。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["random", "artist", "song"],
                    "description": "random=随机、artist=某歌手的歌、song=指定一首。",
                },
                "label": {"type": "string", "description": "播报用的短标签。"},
                "songs": {
                    "type": "array",
                    "description": "要播的曲目，按顺序。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "artist": {"type": "string"},
                        },
                    },
                },
            },
            "required": ["songs"],
        },
    },
}

# 服务端按标点切句，提示语内部带标点会让尾段等到音频入队时才补发出去，
# 于是提示语和音乐撞在一起（与有声书同一个坑）。内部标点全部压成空格。
_SPLIT_PUNCT = "，~、,？?！!；;：:"
# 标题里常见分隔符，念出来会变成奇怪的停顿
_TITLE_NOISE = "·•-–—_/\\|"


@register_function("play_music_library", music_function_desc, ToolType.SYSTEM_CTL)
async def play_music_library(conn: "ConnectionHandler", songs=None, kind="song",
                             label="", **kwargs):
    songs = [s for s in (songs or []) if isinstance(s, dict) and s.get("id")]
    if not songs:
        return ActionResponse(action=Action.RESPONSE, result="no songs",
                              response="没找到要播的音乐")

    _sweep_stale()

    # 先播报、再下载：下载要几百毫秒到几秒，播报正好盖住这段空档
    _say(conn, _announce_text(kind, songs, label))

    played = 0
    for song in songs:
        try:
            path = _fetch(conn, song["id"])
        except Exception as e:
            conn.logger.bind(tag=TAG).warning(
                f"音乐下载失败，跳过 {song.get('title')!r}: {e}")
            continue
        _enqueue_file(conn, path)
        played += 1
        conn.logger.bind(tag=TAG).info(
            f"[音乐] 入队 {song.get('artist')} - {song.get('title')} → {path}")

    if not played:
        return ActionResponse(action=Action.RESPONSE, result="download failed",
                              response="音乐库那边没取到音频")

    conn.logger.bind(tag=TAG).info(f"[音乐] 本次共入队 {played} 首（kind={kind}）")
    return ActionResponse(action=Action.RECORD, result=f"playing {played} songs",
                          response="")


def _announce_text(kind, songs, label):
    """播报文案。内部标点压成空格，只留末尾一个句号（切句规则见上）。"""
    first = songs[0]
    artist = _clean_field(first.get("artist"))
    title = _clean_field(first.get("title"))
    # 标题里已经带了歌手名就别重复念（库里有「歌手 - 歌名」这种标题）
    if artist and title.startswith(artist):
        title = title[len(artist):].lstrip(" -–—·")

    if label:
        body = label
    elif kind == "random":
        body = f"随机播放 {len(songs)} 首"
    elif kind == "artist":
        body = f"播放 {artist} 的歌 {len(songs)} 首"
    else:
        body = f"播放 {artist} 的 {title}" if artist else f"播放 {title}"

    for ch in _SPLIT_PUNCT:
        body = body.replace(ch, " ")
    for ch in _TITLE_NOISE:
        body = body.replace(ch, " ")
    return " ".join(body.split()) + "。"


def _clean_field(value):
    text = str(value or "").strip()
    for ch in _TITLE_NOISE:
        text = text.replace(ch, " ")
    return " ".join(text.split())


def _say(conn, text):
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.TEXT,
            content_detail=text,
        )
    )


def _enqueue_file(conn, abs_path):
    """复用 play_music 已验证的 FILE 分支（base.py 收到 FILE 就走音频文件流）。"""
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.FILE,
            content_file=abs_path,
        )
    )
    if conn.intent_type == "intent_llm":
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.LAST,
                content_type=ContentType.ACTION,
            )
        )


def _sweep_stale():
    """清掉早前残留的下载物。

    正常情况 base.py 播完就把文件删了（靠相对路径匹配 output_dir），
    这里只兜底「进程被杀 / 播放中途异常」留下的残file——
    一个文件可能上百 MB，不兜底会把磁盘慢慢吃掉。
    """
    try:
        if not os.path.isdir(TMP_DIR):
            return
        now = time.time()
        for name in os.listdir(TMP_DIR):
            if not name.startswith("music-"):
                continue
            path = os.path.join(TMP_DIR, name)
            try:
                if now - os.path.getmtime(path) > STALE_SECONDS:
                    os.remove(path)
            except OSError:
                pass
    except Exception:
        pass


def _stream_url(song_id):
    """Subsonic token 鉴权：口令本身不进 URL。"""
    import hashlib
    import secrets

    base = os.environ["NAVIDROME_URL"].rstrip("/")
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
        # format=raw：原样返回原始文件，不在音乐库那侧转码（省 CPU、保质量）
        "id": song_id,
        "format": os.environ.get("NAVIDROME_STREAM_FORMAT", "raw"),
    }
    return f"{base}/rest/stream.view?" + urllib.parse.urlencode(query)


def _fetch(conn, song_id):
    """把曲目取回本地，返回可入队的绝对路径。"""
    os.makedirs(TMP_DIR, exist_ok=True)
    suffix = ".audio"
    out_path = os.path.join(TMP_DIR, f"music-{song_id}{suffix}")

    req = urllib.request.Request(_stream_url(song_id))
    total = 0
    with urllib.request.urlopen(req, timeout=120) as resp:
        # 从响应头猜真实扩展名，pydub 按扩展名选解码器
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
        suffix = {
            "audio/flac": ".flac", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            "audio/x-m4a": ".m4a", "audio/aiff": ".aiff", "audio/x-aiff": ".aiff",
            "audio/wav": ".wav", "audio/x-wav": ".wav",
        }.get(ctype, "")
        if not suffix:
            raise ValueError(f"不认识的音频类型: {ctype!r}")
        out_path = os.path.join(TMP_DIR, f"music-{song_id}{suffix}")

        with open(out_path, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError(f"文件超过上限 {MAX_BYTES} 字节")
                f.write(chunk)

    if total == 0:
        raise ValueError("下载到 0 字节")
    conn.logger.bind(tag=TAG).info(f"[音乐] 下载完成 {total/1e6:.1f}MB → {out_path}")
    return out_path
