import asyncio
import os
import threading
import time

from config import APP_CONFIG
from xiaozhi.event import EventManager
from xiaozhi.ref import get_xiaozhi, set_kws
from xiaozhi.services.audio.kws.sherpa import SherpaOnnx
from xiaozhi.services.audio.stream import MyAudio
from xiaozhi.services.protocols.typing import AudioConfig, DeviceState
from xiaozhi.utils.base import get_env


class _KWS:
    def __init__(self):
        set_kws(self)

    def start(self):
        if not get_env("CLI"):
            return

        self.audio = MyAudio.create()
        self.stream = self.audio.open(
            format=AudioConfig.FORMAT,
            channels=1,
            rate=16000,
            input=True,
            frames_per_buffer=AudioConfig.FRAME_SIZE,
            start=True,
        )

        # 启动 KWS 服务
        self.paused = False
        self.thread = threading.Thread(target=self._detection_loop, daemon=True)
        self.thread.start()

    def get_file_path(self, file_name: str):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(current_dir, "../../../models", file_name)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def _detection_loop(self):
        SherpaOnnx.start()
        self.stream.start_stream()
        while True:
            # 读取缓冲区音频数据
            frames = self.stream.read(
                AudioConfig.FRAME_SIZE, exception_on_overflow=False
            )

            # [本地补丁 2026-10-14] 播放有声书/音乐期间也要能用唤醒词打断。
            # 原版在 LISTENING/SPEAKING 下一律跳过 KWS，而长音频（有声书/音乐）
            # 全程都处于 SPEAKING —— 于是「你好小爱」在播放期间完全失效，
            # 只能靠小爱自己的 ASR（说话即触发 on_interrupt）来停。
            # 现在只在「原生音色念回复」时保持跳过（防自触发）：
            # 服务端在推音频帧（bridge 打的时间戳）说明是长音频在放，放行唤醒词。
            xiaozhi = get_xiaozhi()
            state = xiaozhi.device_state
            streaming = (
                time.monotonic() - getattr(xiaozhi, "_ab_last_frame_at", 0.0)
            ) < 1.5
            if (
                not frames
                or self.paused
                or state == DeviceState.LISTENING
                or (state == DeviceState.SPEAKING and not streaming)
            ):
                time.sleep(0.01)
                continue

            result = SherpaOnnx.kws(frames)
            if result:
                print(f"🔥 触发唤醒: {result}")
                self.on_message(result)

    def on_message(self, text: str):
        asyncio.run_coroutine_threadsafe(
            EventManager.wakeup(text, "kws"),
            get_xiaozhi().loop,
        )


KWS = _KWS()
