"""播放 BookOrbit 有声书：查库取路径 → 塞 ContentType.FILE 进 TTS 队列。

复用 play_music.py 已验证的 FILE 型音频机制，区别只在数据来源：
    play_music        → 本地 music_dir 里的音乐文件
    bookorbit_audiobook → 挂进容器的 BookOrbit 音频书库原始文件（非 TTS 合成）

数据面全在音频目录与 Postgres 之间，不经过 BookOrbit HTTP，所以不需要 JWT。
进度暂不写回（第一版只读），避免「播完/被打断都写不对」的已知坑。
"""

import os

from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType, ContentType
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__

# 音频书库的 library_id。模糊匹配必须限定它，否则「三体」会命中同名电子书
# （同一个书名在库里可能同时有 audio / azw3 / epub / mobi / pdf 多条记录）。
LIBRARY_ID = int(os.environ.get("BOOKORBIT_LIBRARY_ID", 4))
# 书库在容器里的挂载点，与 docker-compose 里的挂载目标保持一致。
BOOKS_ROOT = os.environ.get("BOOKORBIT_BOOKS_ROOT", "/books")
PG_HOST = os.environ.get("BOOKORBIT_PG_HOST", "127.0.0.1")
PG_DB = os.environ.get("BOOKORBIT_PG_DB", "bookorbit")
PG_USER = os.environ.get("BOOKORBIT_PG_USER", "bookorbit")
# 平台是单用户形态，进度表通常只有 user_id=1 一行
DEFAULT_USER_ID = int(os.environ.get("BOOKORBIT_USER_ID", 1))

bookorbit_function_desc = {
    "type": "function",
    "function": {
        "name": "play_audiobook",
        "description": (
            "播放用户自有书库（BookOrbit）里的有声书。用于「播放有声书三体」"
            "「继续播放三体」「放一下诡秘之主」这类请求。播放的是 NAS 上的原始"
            "音频文件，不是语音合成。只播当前一集，不自动连播下一集。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "书名或书名的一部分，如「三体」「诡秘之主」。",
                },
                "mode": {
                    "type": "string",
                    "enum": ["resume", "new"],
                    "description": (
                        "resume=从平台记录的进度续播（用户说「继续播放」时用），"
                        "new=从头播放。"
                    ),
                },
            },
            "required": ["title"],
        },
    },
}


@register_function("play_audiobook", bookorbit_function_desc, ToolType.SYSTEM_CTL)
async def play_audiobook(conn: "ConnectionHandler", title: str, mode: str = "resume"):
    try:
        target = _resolve(title, mode)
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"有声书解析失败: {e}")
        return ActionResponse(
            action=Action.RESPONSE, result=str(e), response="没找到这本书"
        )

    if target is None:
        return ActionResponse(
            action=Action.RESPONSE,
            result="not found",
            response=f"书库里没有找到《{title}》",
        )

    book_title, file_title, abs_path, start_seconds = target
    conn.logger.bind(tag=TAG).info(
        f"有声书命中: 《{book_title}》{file_title} 起播={start_seconds}s path={abs_path}"
    )

    _say(conn, _announce_text(book_title, file_title))

    # 起播位置 >0 时只放裁剪版：AudioRateController 逐帧控速，不裁会从集头重放；
    # 若原始文件也入队，整集先放一遍、紧接着又从头续播同样的内容。
    if start_seconds > 0:
        _enqueue_trimmed(conn, abs_path, start_seconds)
    else:
        _enqueue_file(conn, abs_path)

    return ActionResponse(action=Action.RECORD, result=abs_path, response="")


# 服务端 _get_segment_text() 按标点切句，切剩的尾段要等到 FILE 消息才补发出去，
# 而那一刻音频正好入队 —— 音箱上「原生音色念提示语」(miplayer) 与「服务端音频」
# (aplay) 是两条独立通路，开头必然叠在一起。让提示语内部不带标点、末尾只留一个
# 句号，服务端收到文本时就整段切出，而音频还要等解码（十几秒）才起，天然错开。
_ANNOUNCE_SPLIT_PUNCT = "，~、,？?！!；;：:"


def _announce_text(book_title, file_title):
    """提示语：内部标点全压成空格，只保留末尾一个句号（切句规则见上）。"""
    body = f"正在播放《{book_title}》{file_title}"
    for ch in _ANNOUNCE_SPLIT_PUNCT:
        body = body.replace(ch, " ")
    return body + "。"


def _say(conn, text):
    """提示语入队。native_xiaomi 模式下由音箱原生音色念出来。"""
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.TEXT,
            content_detail=text,
        )
    )


def _enqueue_file(conn, abs_path):
    """音频文件入队，复用 play_music 已验证的 FILE 分支（base.py:393）。"""
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


def _enqueue_trimmed(conn, abs_path, start_seconds):
    """裁掉已听过的部分后入队，落 tmp/（base.py 只删自己 output_dir 下的文件）。"""
    from pydub import AudioSegment

    # 库里不只有 mp3（实测《诡秘之主》是 .m4a），格式必须跟扩展名走，
    # 写死 mp3 会在 pydub 里直接抛。
    fmt = abs_path.rsplit(".", 1)[-1].lower()
    seg = AudioSegment.from_file(abs_path, format=fmt, parameters=["-nostdin"])
    trimmed = seg[int(start_seconds * 1000) :]
    if not trimmed:
        return

    # 相对路径（服务端进程 CWD = 项目根目录）：base.py 删临时文件的判断是
    # tts_file.startswith(self.output_file)，而 output_dir 就是相对的 "tmp/"。
    # 传绝对路径永远匹配不上，裁出来的文件不会自动删（实测踩过）。
    out_dir = os.path.join(os.environ.get("XIAOZHI_TMP_DIR", "tmp"))
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"resume-{int(start_seconds)}-{os.getpid()}.mp3")
    trimmed.export(out_path, format="mp3")
    conn.logger.bind(tag=TAG).info(f"已裁掉前 {start_seconds}s → {out_path}")

    _enqueue_file(conn, out_path)


def _resolve(title, mode):
    """返回 (书名, 集名, 绝对路径, 集内起播秒数)；找不到返回 None。"""
    import psycopg2
    import psycopg2.extras

    pg = psycopg2.connect(
        host=PG_HOST,
        dbname=PG_DB,
        user=PG_USER,
        password=os.environ["BOOKORBIT_PG_PASSWORD"],
        connect_timeout=5,
    )
    pg.autocommit = True
    try:
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, folder_path FROM books "
                "WHERE library_id = %s AND status = 'present' AND folder_path ILIKE %s "
                "ORDER BY length(folder_path)",
                (LIBRARY_ID, f"%{title}%"),
            )
            books = cur.fetchall()
            if not books:
                return None
            # 库名去掉路径前缀：/<书库根>/<分类目录>/<书名> → <书名>
            book_title = books[0]["folder_path"].rsplit("/", 1)[-1]
            book_id = books[0]["id"]

            start_seconds = 0.0
            current_file_id = None
            if mode == "resume":
                cur.execute(
                    "SELECT current_file_id, position_seconds FROM audiobook_progress "
                    "WHERE book_id = %s AND user_id = %s",
                    (book_id, DEFAULT_USER_ID),
                )
                row = cur.fetchone()
                if row and row["current_file_id"]:
                    current_file_id = row["current_file_id"]
                    start_seconds = float(row["position_seconds"] or 0)

            if current_file_id:
                cur.execute(
                    "SELECT absolute_path FROM book_files WHERE id = %s",
                    (current_file_id,),
                )
            else:
                cur.execute(
                    "SELECT absolute_path FROM book_files "
                    "WHERE book_id = %s ORDER BY sort_order",
                    (book_id,),
                )
            files = cur.fetchall()
    finally:
        pg.close()

    if not files:
        return None

    abs_path = files[0]["absolute_path"]
    # 库里的路径形如 /books/05_音频书/...，与容器内挂载点一致；防一手越界
    if not abs_path.startswith(BOOKS_ROOT + "/"):
        raise ValueError(f"路径越界已拒绝: {abs_path}")
    if not os.path.exists(abs_path):
        raise FileNotFoundError(abs_path)

    # 库里不只有 mp3（也可能是 .m4a 等），格式必须跟扩展名走，
    # 写死 mp3 会在 pydub 里直接抛。
    file_title = os.path.basename(abs_path).rsplit(".", 1)[0]
    return book_title, file_title, abs_path, start_seconds
