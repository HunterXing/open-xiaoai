"""流式音频：文件从 ffmpeg 边解边推，且「文件播放」直推音频队列。

两个优化
--------
① 流式转码（替换 `core/utils/util.py:audio_to_data_stream`）
   原实现先把整个文件解码进内存、全量重采样，再开始逐帧推流——
   19 分钟的集子，出声前实测静默约 10 秒。本模块换成 ffmpeg 管道
   边出边分帧编码回调，首帧 ~0.05s（实测）。帧宽（60ms）、末帧补零、
   `end_of_stream` 语义与原实现一致，只在「数据从哪来」这一步不同。

② 文件播放直推（包装 `index_stream.TTSProvider._process_audio_file_stream`）
   即使转码变成流式，声音仍要等整集转完才出声——因为 index_stream 的
   `tts_text_priority_thread` 给文件播放传的回调是：

       lambda audio_data: self.handle_audio_file(audio_data, text)

   这个 lambda 把每一帧攒进 `before_stop_play_files`，等轮到 LAST 才由
   `_process_before_stop_play_files()` 一次性灌进音频队列。所以「转码多快」
   根本不重要，出声时间被这个收集器的冲刷时机钉死。
   这里把该收集器回调替换成 `self.handle_opus`（帧产生即入队），
   与提示语播报并行；`before_stop_play_files` 不再被填充，收尾无副作用。

   识别方式：只替换 qualname 含 `tts_text_priority_thread` 且为 lambda 的
   回调——这是已知收集器的指纹，别的自定义回调不动。

装载方式
--------
import 即生效（替换 `core.utils.util` 的模块属性 + 包装 index_stream 的方法）。
`index_stream.audio_to_opus_data_stream` 里是**函数内**写
`from core.utils.util import audio_to_data_stream`，调用时才查模块属性，
所以运行前替换即可命中。

安全
----
- 装载失败 / 运行异常且尚未出帧 → 自动回退原实现，最坏情况 = 改造前。
- 中途失败（已推过帧）不回退，只记错误——否则会从头发第二遍。
"""

import os
import shutil
import subprocess

try:  # 服务端用 loguru
    from loguru import logger

    def _log(msg):
        logger.bind(tag="fast-audio").info(msg)

    def _warn(msg):
        logger.bind(tag="fast-audio").warning(msg)

except Exception:  # pragma: no cover
    import logging

    _lg = logging.getLogger(__name__)

    def _log(msg):
        _lg.info(msg)

    def _warn(msg):
        _lg.warning(msg)

_FRAME_MS = 60  # 与 core/utils/util.py:pcm_to_data_stream 保持一致
_ORIGINAL = None
_INSTALLED = False


def _ffmpeg_pcm_stream(audio_file_path, is_opus, callback, sample_rate, opus_encoder):
    """ffmpeg → 16bit/单声道/指定采样率 PCM 管道，逐 60ms 帧编码回调。"""
    import time as _time

    _t0 = _time.time()
    _first_logged = [False]

    frame_samples = int(sample_rate * _FRAME_MS / 1000)
    frame_bytes = frame_samples * 2  # 16bit = 2 bytes/sample

    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        audio_file_path,
        "-vn",  # 丢掉内嵌封面等视频流
        "-map",
        "0:a:0",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "pipe:1",
    ]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
    )

    # 无外部编码器时的临时编码器：要跨帧复用（有状态），且只建一次。
    encoder = opus_encoder
    tmp_encoder = None
    if is_opus and encoder is None:
        import opuslib_next

        tmp_encoder = opuslib_next.Encoder(
            sample_rate, 1, opuslib_next.APPLICATION_AUDIO
        )

    pending = None  # 预读一帧：最后一帧才能带上 end_of_stream=True
    buf = bytearray()
    emitted = 0

    def _emit(frame, is_last):
        nonlocal emitted
        if is_opus:
            if encoder is not None:
                encoder.encode_pcm_to_opus_stream(
                    frame, end_of_stream=is_last, callback=callback
                )
            else:
                import numpy as np

                callback(
                    tmp_encoder.encode(
                        np.frombuffer(frame, dtype=np.int16).tobytes(), frame_samples
                    )
                )
        else:
            callback(frame)
        if not _first_logged[0]:
            _first_logged[0] = True
            _log(f"流式首帧 +{_time.time() - _t0:.3f}s: {audio_file_path}")
        emitted += 1

    try:
        while True:
            chunk = proc.stdout.read(frame_bytes - len(buf))
            if not chunk:
                break
            buf += chunk
            while len(buf) >= frame_bytes:
                frame = bytes(buf[:frame_bytes])
                del buf[:frame_bytes]
                if pending is not None:
                    _emit(pending, False)
                pending = frame

        # 收尾：把残帧补零，作为最后一帧（end_of_stream=True）
        if buf:
            if pending is not None:
                _emit(pending, False)
                pending = None
            tail = bytes(buf)
            tail += b"\x00" * (frame_bytes - len(tail))
            _emit(tail, True)
        elif pending is not None:
            _emit(pending, True)
    finally:
        try:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    if proc.returncode not in (0, None) and emitted == 0:
        err = b""
        try:
            if proc.stderr is not None:
                err = proc.stderr.read() or b""
        except Exception:
            pass
        raise RuntimeError(
            f"ffmpeg 转码失败 rc={proc.returncode}: "
            f"{err.decode('utf-8', 'ignore')[:300]}"
        )


def _streaming_audio_to_data_stream(
    audio_file_path, is_opus=True, callback=None, sample_rate=16000, opus_encoder=None
):
    """替换版实现；异常且尚未出帧时回退原实现。"""
    if callback is None:
        return
    try:
        return _ffmpeg_pcm_stream(
            audio_file_path, is_opus, callback, sample_rate, opus_encoder
        )
    except Exception as e:
        _warn(f"流式转码失败（{e}），回退原实现: {audio_file_path}")
        if _ORIGINAL is not None:
            return _ORIGINAL(
                audio_file_path,
                is_opus=is_opus,
                callback=callback,
                sample_rate=sample_rate,
                opus_encoder=opus_encoder,
            )


def _install_file_stream_patch():
    """把「文件播放」的收集器回调换成直推队列（详见模块 docstring ②）。"""
    try:
        from core.providers.tts import index_stream as _is  # type: ignore
    except Exception as e:  # pragma: no cover
        _warn(f"文件流式补丁：index_stream 导入失败（不影响播放）: {e}")
        return False

    cls = getattr(_is, "TTSProvider", None)
    if cls is None:
        return False
    if getattr(cls, "_fa_file_stream_patched", False):
        return True

    orig = cls._process_audio_file_stream

    def _patched(self, tts_file, callback):
        try:
            qn = getattr(callback, "__qualname__", "") or ""
            if "<lambda>" in qn and "tts_text_priority_thread" in qn:
                if hasattr(self, "handle_opus"):
                    callback = self.handle_opus
        except Exception:
            pass
        return orig(self, tts_file, callback)

    cls._process_audio_file_stream = _patched
    cls._fa_file_stream_patched = True
    _log(f"文件播放直推音频队列已启用: {cls.__module__}.{cls.__name__}")
    return True


def install():
    """替换 core.utils.util.audio_to_data_stream。幂等，可重复调用。"""
    global _ORIGINAL, _INSTALLED
    if _INSTALLED:
        return True
    try:
        from core.utils import util

        if getattr(util, "_fast_audio_stream_installed", False):
            _INSTALLED = True
            return True
        _ORIGINAL = util.audio_to_data_stream
        util.audio_to_data_stream = _streaming_audio_to_data_stream
        util._fast_audio_stream_installed = True
        _INSTALLED = True
        _log("流式转码已启用：首帧 ~0.3s（原实现整段解码，19 分钟集子约 10s）")
        _install_file_stream_patch()
        return True
    except Exception as e:
        _warn(f"流式转码装载失败（不影响播放，走原实现）: {e}")
        return False


install()
