import asyncio
import json
import re
import threading
import time

from config import APP_CONFIG
from xiaozhi.event import EventManager
from xiaozhi.ref import set_xiaozhi
from xiaozhi.services.audio.kws import KWS
from xiaozhi.services.audio.vad import VAD
from xiaozhi.services.native_tts import (
    NativeTTSSettings,
    NativeXiaomiTTS,
    resolve_tts_output_mode,
)
from xiaozhi.services.protocols.typing import (
    AbortReason,
    DeviceState,
    EventType,
    ListeningMode,
)
from xiaozhi.services.protocols.websocket_protocol import WebsocketProtocol
from xiaozhi.utils.base import get_env
from xiaozhi.utils.config import ConfigManager
from xiaozhi.xiaoai import XiaoAI


class XiaoZhi:
    """智能音箱应用程序主类"""

    _instance = None

    @classmethod
    def instance(cls):
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = XiaoZhi()
        return cls._instance

    def __init__(self):
        """初始化应用程序"""
        # 确保单例模式
        if XiaoZhi._instance is not None:
            raise Exception("XiaoZhi是单例类，请使用instance()获取实例")
        XiaoZhi._instance = self

        # 获取配置管理器实例
        self.config = ConfigManager.instance()
        tts_output_config = APP_CONFIG.get("tts_output", {})
        self.tts_output_mode = resolve_tts_output_mode(
            tts_output_config,
            get_env("XIAOZHI_TTS_OUTPUT_MODE"),
        )
        native_config = tts_output_config.get("native_xiaomi", {})
        self.native_tts = NativeXiaomiTTS(
            XiaoAI.speaker,
            NativeTTSSettings.from_config(native_config),
        )
        self._native_finish_task = None
        print(f"🔈 TTS 输出模式: {self.tts_output_mode}")

        # 状态变量
        self.device_state = DeviceState.IDLE
        self.voice_detected = False
        self.current_text = ""
        self.current_emotion = "neutral"

        # 音频处理相关
        self.audio_codec = None

        # 事件循环和线程
        self.loop = asyncio.new_event_loop()
        self.loop_thread = None
        self.running = False

        # 任务队列和锁
        self.main_tasks = []
        self.mutex = threading.Lock()

        # 协议实例
        self.protocol = None

        # 回调函数
        self.on_state_changed_callbacks = []

        # 初始化事件对象
        self.events = {
            EventType.SCHEDULE_EVENT: threading.Event(),
            EventType.AUDIO_INPUT_READY_EVENT: threading.Event(),
        }

        # 创建显示界面
        self.display = None
        set_xiaozhi(self)

    def run(self):
        self.protocol = WebsocketProtocol()

        # 创建并启动事件循环线程
        self.loop_thread = threading.Thread(target=self._run_event_loop)
        self.loop_thread.daemon = True
        self.loop_thread.start()

        # 等待事件循环准备就绪
        time.sleep(0.1)

        # 初始化应用程序
        asyncio.run_coroutine_threadsafe(XiaoAI.init_xiaoai(), self.loop)
        asyncio.run_coroutine_threadsafe(self._initialize_xiaozhi(), self.loop)

        # 启动主循环线程
        main_loop_thread = threading.Thread(target=self._main_loop)
        main_loop_thread.daemon = True
        main_loop_thread.start()

        VAD.start()
        KWS.start()

        # 启动 GUI
        self._initialize_display()
        self.display.start()

    def _run_event_loop(self):
        """运行事件循环的线程函数"""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def _initialize_xiaozhi(self):
        """初始化应用程序组件"""

        # 初始化音频编解码器
        self._initialize_audio()

        # 设置协议回调
        self.protocol.on_network_error = self._on_network_error
        self.protocol.on_incoming_audio = self._on_incoming_audio
        self.protocol.on_incoming_json = self._on_incoming_json
        self.protocol.on_audio_channel_opened = self._on_audio_channel_opened
        self.protocol.on_audio_channel_closed = self._on_audio_channel_closed

        # 打开音频通道
        self.device_state = DeviceState.CONNECTING
        await self.protocol.open_audio_channel()

    def _initialize_audio(self):
        """初始化音频设备和编解码器"""
        try:
            from xiaozhi.services.audio.codec import AudioCodec

            self.audio_codec = AudioCodec()
        except Exception as e:
            self.alert("错误", f"初始化音频设备失败: {e}")

        threading.Thread(target=self._audio_input_event_trigger, daemon=True).start()

    def _initialize_display(self):
        """初始化显示界面"""
        if get_env("CLI"):
            from xiaozhi.services.display import no_display

            self.display = no_display.NoDisplay()
        else:
            from xiaozhi.services.display import gui_display

            self.display = gui_display.GuiDisplay()

        # 设置回调函数
        self.display.set_callbacks(
            press_callback=self.start_listening,
            release_callback=self.stop_listening,
            status_callback=self._get_status_text,
            text_callback=self._get_current_text,
            emotion_callback=self._get_current_emotion,
            mode_callback=self._on_mode_changed,
            auto_callback=self.toggle_chat_state,
            abort_callback=lambda: self.abort_speaking(AbortReason.WAKE_WORD_DETECTED),
        )

    def _main_loop(self):
        """应用程序主循环"""
        self.running = True

        while self.running:
            # 等待事件
            for event_type, event in self.events.items():
                if event.is_set():
                    event.clear()

                    if event_type == EventType.AUDIO_INPUT_READY_EVENT:
                        self._handle_input_audio()
                    elif event_type == EventType.SCHEDULE_EVENT:
                        self._process_scheduled_tasks()

            time.sleep(0.01)

    def _process_scheduled_tasks(self):
        """处理调度任务"""
        with self.mutex:
            tasks = self.main_tasks.copy()
            self.main_tasks.clear()

        for task in tasks:
            try:
                task()
            except Exception:
                pass

    def schedule(self, callback):
        """调度任务到主循环"""
        with self.mutex:
            # 如果是中止语音的任务，检查是否已经存在相同类型的任务
            if "abort_speaking" in str(callback):
                # 如果已经有中止任务在队列中，就不再添加
                if any("abort_speaking" in str(task) for task in self.main_tasks):
                    return
            self.main_tasks.append(callback)
        self.events[EventType.SCHEDULE_EVENT].set()

    def _handle_input_audio(self):
        """处理音频输入"""
        if self.device_state != DeviceState.LISTENING:
            return

        encoded_data = self.audio_codec.read_audio()
        if encoded_data and self.protocol and self.protocol.is_audio_channel_opened():
            asyncio.run_coroutine_threadsafe(
                self.protocol.send_audio(encoded_data), self.loop
            )

    def _on_network_error(self, message):
        """网络错误回调"""
        print(f"🌐 网络连接异常，将自动重试：{message}")
        self.set_device_state(DeviceState.IDLE)

    def _on_incoming_audio(self, data):
        """接收音频数据回调"""
        # [本地补丁 2026-10-14] 原版在 native_xiaomi 模式下直接 return，
        # 把服务端推来的所有二进制音频帧丢光——FILE 型音频（有声书/音乐）
        # 走的就是这条通道，于是播放永远静默。
        # native 模式下普通 TTS 只下文本、根本不产音频帧，所以放行二进制帧
        # 不会影响原生 TTS 音色；保留 SPEAKING 判断即安全。
        if self.device_state != DeviceState.SPEAKING:
            # 被打断/播放结束时，把囤着的帧丢掉，别漏进下一次播放
            if getattr(self, "_ab_held", None):
                dropped = len(self._ab_held)
                self._ab_held = []
                print(f"🗑️ [有声书] 已离开播放态，丢弃囤下的 {dropped} 帧")
            # 状态不对时原本静默丢弃，出问题完全看不到，打一次日志
            if not getattr(self, "_ab_drop_logged", False):
                self._ab_drop_logged = True
                print(
                    f"⚠️ [有声书] 服务端音频帧被丢弃（device_state={self.device_state}）"
                )
            return
        if not getattr(self, "_ab_frame_logged", False):
            self._ab_frame_logged = True
            stream = getattr(self.audio_codec, "output_stream", None)
            try:
                active = stream.is_active() if stream is not None else None
            except Exception:
                active = "?"
            # codec.write_audio 内部把所有异常都吞了，且流未激活时是静默 no-op，
            # 所以这里必须把首帧和流状态打出来，否则失败无从判断。
            print(
                f"🔊 [有声书] 收到服务端首帧 {len(data)}B；"
                f"output_stream active={active}；state={self.device_state}"
            )
        self._ab_write_or_hold(data)

    # ---- [本地补丁 2026-10-14] 提示语念完之前先别放书声 ----
    # 音箱上「原生音色念提示语」(miplayer) 和「服务端音频」(aplay) 是两条互不
    # 相知的通路。服务端又是把提示语尾段与音频背靠背入队的
    # （index_stream._process_remaining_text_stream 里先塞文本、紧接
    # _process_before_stop_play_files 塞音频），开头叠音是结构性的。
    # 插件侧已把提示语改成整段提前切出，正常时长下解码（十几秒）足够念完；
    # 这里再兜一层：只要 native TTS 还有提示语没念完，PCM 先囤住，念完按序补写。
    def _ab_say_pending(self):
        """native TTS 是否还有提示语没念完（待切段/待合成/待播/正在播）。

        _generated_files 是最可靠的信号：提示语音频生成时加入、
        miplayer 播完才 discard，非空即代表「还有一句没念完」。
        """
        nt = getattr(self, "native_tts", None)
        if nt is None:
            return False
        try:
            if getattr(nt, "_pending_text", ""):
                return True
            if getattr(nt, "_generated_files", None):
                return True
            for attr in ("_segment_queue", "_audio_queue"):
                queue = getattr(nt, attr, None)
                if queue is not None and queue.qsize():
                    return True
        except Exception:
            return False
        return False

    def _ab_emit(self, chunk):
        """写一帧 PCM 到音箱，并记下时间戳。

        KWS 侧（kws/__init__.py 补丁）靠这个时间戳判断「正在放长音频」，
        从而在 SPEAKING 下也放开唤醒词——否则有声书播放期间打不断。
        """
        self.audio_codec.write_audio(chunk)
        self._ab_last_frame_at = time.monotonic()

    def _ab_write_or_hold(self, data):
        if not self._ab_say_pending():
            held = getattr(self, "_ab_held", None)
            if held:
                self._ab_held = []
                print(f"▶️ [有声书] 提示语已念完，补写囤下的 {len(held)} 帧")
                for chunk in held:
                    self._ab_emit(chunk)
            self._ab_emit(data)
            return

        held = getattr(self, "_ab_held", None)
        if held is None:
            held = self._ab_held = []
            print("⏸️ [有声书] 提示语还在念，音频帧先囤住，念完再放")
        # 兜底：万一信号没清掉（异常路径留下脏状态），囤到 ~18s 就强放，
        # 宁可叠一下也不能整块不出声。
        if len(held) < 300:
            held.append(data)
        else:
            self._ab_emit(data)

    async def _on_incoming_json(self, json_data):
        """接收JSON数据回调"""
        try:
            if not json_data:
                return

            # 解析JSON数据
            if isinstance(json_data, str):
                data = json.loads(json_data)
            else:
                data = json_data

            # 处理不同类型的消息
            msg_type = data.get("type", "")
            if msg_type == "tts":
                await self._handle_tts_message(data)
            elif msg_type == "stt":
                self._handle_stt_message(data)
            elif msg_type == "llm":
                self._handle_llm_message(data)
        except Exception as error:
            print(f"❌ 处理服务端消息失败: {error}")

    async def _handle_tts_message(self, data):
        """处理TTS消息"""
        state = data.get("state", "")
        if state == "start":
            if self.tts_output_mode == "native_xiaomi":
                await self.native_tts.start(data.get("session_id"))
            EventManager.on_tts_start(data.get("session_id"))
            self.schedule(lambda: self._handle_tts_start())
        elif state == "stop":
            if self.tts_output_mode == "native_xiaomi":
                self._native_finish_task = asyncio.create_task(
                    self._finish_native_tts(data.get("session_id"))
                )
            else:
                EventManager.on_tts_end(data.get("session_id"))
                self.schedule(lambda: self._handle_tts_stop())
        elif state == "sentence_start":
            text = data.get("text", "")
            if text:
                print(f"🤖 小智：{text}")

                if self.tts_output_mode == "native_xiaomi":
                    await self.native_tts.add_text(text)

                verification_code = re.search(r"验证码.*?(\d+)", text) or re.search(
                    r"控制面板.*?(\d+)", text
                )
                if verification_code:
                    self.config.update_config_file(
                        "VERIFICATION_CODE", verification_code.group(1)
                    )

                self.schedule(lambda: self.set_chat_message("assistant", text))

    async def _finish_native_tts(self, session_id):
        current_task = asyncio.current_task()
        try:
            completed = await self.native_tts.finish()
            if not completed:
                return
            if self.native_tts.error:
                print(f"⚠️ 原生小爱 TTS 会话存在错误: {self.native_tts.error}")

            # Clear the task reference before starting the next listening
            # session. Otherwise that session can race with this task and try
            # to cancel/await it from another callback path.
            if self._native_finish_task is current_task:
                self._native_finish_task = None
            EventManager.on_tts_end(session_id)
            self.schedule(lambda: self._handle_tts_stop())
        finally:
            if self._native_finish_task is current_task:
                self._native_finish_task = None

    async def abort_tts_output(self):
        if self.tts_output_mode != "native_xiaomi":
            return
        finish_task = self._native_finish_task
        self._native_finish_task = None
        if finish_task and not finish_task.done():
            finish_task.cancel()
            await asyncio.gather(finish_task, return_exceptions=True)
        await self.native_tts.abort()

    def _handle_tts_start(self):
        """处理TTS开始事件"""
        if (
            self.device_state == DeviceState.IDLE
            or self.device_state == DeviceState.LISTENING
        ):
            self.set_device_state(DeviceState.SPEAKING)

    def _handle_tts_stop(self):
        """处理TTS停止事件"""
        pass

    def _handle_stt_message(self, data):
        """处理STT消息"""
        text = data.get("text", "")
        if text:
            print(f"💬 我说：{text}")
            EventManager.on_stt()
            self.schedule(lambda: self.set_chat_message("user", text))

    def _handle_llm_message(self, data):
        """处理LLM消息"""
        emotion = data.get("emotion", "")
        if emotion:
            self.schedule(lambda: self.set_emotion(emotion))

    async def _on_audio_channel_opened(self):
        """音频通道打开回调"""
        self.set_device_state(DeviceState.IDLE)

    def _audio_input_event_trigger(self):
        """音频输入事件触发器"""
        while self.running:
            if self.audio_codec.input_stream.is_active():
                self.events[EventType.AUDIO_INPUT_READY_EVENT].set()
            time.sleep(0.01)

    async def _on_audio_channel_closed(self):
        """音频通道关闭回调"""
        await self.abort_tts_output()
        self.set_device_state(DeviceState.IDLE)
        self.audio_codec.stop_streams()

    def set_device_state(self, state):
        """设置设备状态"""
        self.device_state = state

        VAD.pause()  # 停用 VAD
        self.audio_codec.stop_streams()  # 停用输入输出流

        if state == DeviceState.IDLE:
            self.display.update_status("待命")
            self.display.update_emotion("😶")
        elif state == DeviceState.CONNECTING:
            self.display.update_status("连接中...")
        elif state == DeviceState.LISTENING:
            self.display.update_status("聆听中...")
            self.display.update_emotion("🙂")
            # 停止输出流
            if self.audio_codec.output_stream.is_active():
                self.audio_codec.output_stream.stop_stream()
            # 打开输入流
            if not self.audio_codec.input_stream.is_active():
                self.audio_codec.input_stream.start_stream()
        elif state == DeviceState.SPEAKING:
            self.display.update_status("说话中...")
            # 停止输入流
            if self.audio_codec.input_stream.is_active():
                self.audio_codec.input_stream.stop_stream()
            # 打开输出流
            if not self.audio_codec.output_stream.is_active():
                self.audio_codec.output_stream.start_stream()

        # 通知状态变化
        for callback in self.on_state_changed_callbacks:
            try:
                callback(state)
            except Exception:
                pass

    def _get_status_text(self):
        """获取当前状态文本"""
        states = {
            DeviceState.IDLE: "待命",
            DeviceState.CONNECTING: "连接中...",
            DeviceState.LISTENING: "聆听中...",
            DeviceState.SPEAKING: "说话中...",
        }
        return states.get(self.device_state, "未知")

    def _get_current_text(self):
        """获取当前显示文本"""
        return self.current_text

    def _get_current_emotion(self):
        """获取当前表情"""
        emotions = {
            "neutral": "😶",
            "happy": "🙂",
            "laughing": "😆",
            "funny": "😂",
            "sad": "😔",
            "angry": "😠",
            "crying": "😭",
            "loving": "😍",
            "embarrassed": "😳",
            "surprised": "😲",
            "shocked": "😱",
            "thinking": "🤔",
            "winking": "😉",
            "cool": "😎",
            "relaxed": "😌",
            "delicious": "🤤",
            "kissy": "😘",
            "confident": "😏",
            "sleepy": "😴",
            "silly": "😜",
            "confused": "🙄",
        }
        return emotions.get(self.current_emotion, "😶")

    def set_chat_message(self, role, message):
        """设置聊天消息"""
        self.current_text = message
        # 更新显示
        if self.display:
            self.display.update_text(message)

    def set_emotion(self, emotion):
        """设置表情"""
        self.current_emotion = emotion
        # 更新显示
        if self.display:
            self.display.update_emotion(self._get_current_emotion())

    def start_listening(self):
        """开始监听"""
        self.schedule(self._start_listening_impl)

    def _start_listening_impl(self):
        """开始监听的实现"""
        if not self.protocol:
            return

        self.set_device_state(DeviceState.IDLE)
        asyncio.run_coroutine_threadsafe(
            self._abort_output_and_notify_server(),
            self.loop,
        )
        asyncio.run_coroutine_threadsafe(
            self.protocol.send_start_listening(ListeningMode.MANUAL), self.loop
        )
        self.set_device_state(DeviceState.LISTENING)

    def stop_listening(self):
        """停止监听"""
        self.schedule(self._stop_listening_impl)

    def _stop_listening_impl(self):
        """停止监听的实现"""
        asyncio.run_coroutine_threadsafe(self.protocol.send_stop_listening(), self.loop)
        self.set_device_state(DeviceState.IDLE)

    def abort_speaking(self, reason):
        """中止语音输出"""
        self.set_device_state(DeviceState.IDLE)
        asyncio.run_coroutine_threadsafe(
            self._abort_output_and_notify_server(),
            self.loop,
        )

    async def _abort_output_and_notify_server(self):
        await self.abort_tts_output()
        await self.protocol.send_abort_speaking(AbortReason.ABORT)

    def alert(self, title, message):
        """显示警告信息"""
        if self.display:
            self.display.update_text(f"{title}: {message}")

    def on_state_changed(self, callback):
        """注册状态变化回调"""
        self.on_state_changed_callbacks.append(callback)

    def shutdown(self):
        """关闭应用程序"""
        self.running = False

        if self.loop and self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self.abort_tts_output(), self.loop
            )
            try:
                future.result(timeout=2)
            except Exception:
                pass

        # 关闭音频编解码器
        if self.audio_codec:
            self.audio_codec.close()

        # 关闭协议
        if self.protocol:
            asyncio.run_coroutine_threadsafe(
                self.protocol.close_audio_channel(), self.loop
            )

        # 停止事件循环
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)

        # 等待事件循环线程结束
        if self.loop_thread and self.loop_thread.is_alive():
            self.loop_thread.join(timeout=1.0)

    def toggle_chat_state(self):
        """切换聊天状态"""
        pass

    def _on_mode_changed(self, auto_mode):
        """处理对话模式变更"""
        pass
