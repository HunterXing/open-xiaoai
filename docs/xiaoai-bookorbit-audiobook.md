# 小爱音箱直接播放书库有声书（BookOrbit）

## 1. 目标

让语音助手播放**书库里已有的有声书音频文件**，而不是让大模型把文字念出来：

- 说「播放有声书三体」→ 音箱直接出声，播的是书库里的原始音频
- 说「继续播放三体」→ 从平台记录的进度接着播
- 正常对话（问天气、聊天、问「三体讲了什么」）不受影响
- 播放中可以打断

第一版刻意保守：**一次只播一集**、不自动连播、不写回播放进度。

## 2. 架构

```text
                     「播放有声书三体」
                            │
                            ▼
        ┌──────────── xiaozhi-esp32-server ────────────┐
        │                                              │
        │  core/connection.py  ← 补丁：调 LLM 之前先做 │
        │        │               确定性意图预判         │
        │        │ 命中 → 自己造 play_audiobook 的     │
        │        │        tool_call 塞进推理流          │
        │        ▼                                     │
        │  plugins_func/functions/                     │
        │      bookorbit_audiobook.py                  │
        │        │  查 Postgres：书名 → book_id        │
        │        │      → book_files.absolute_path     │
        │        │      → audiobook_progress（续播点） │
        │        ▼                                     │
        │  提示语(TEXT) + 音频文件(FILE) 入 TTS 队列   │
        └──────────────────┬───────────────────────────┘
                           │ PCM 帧
                           ▼
        ┌──────────── open-xiaoai-xiaozhi ─────────────┐
        │  xiaozhi.py  ← 补丁：提示语没念完前先囤帧    │
        │        │        （_ab_write_or_hold）        │
        └──────────────────┬───────────────────────────┘
                           ▼
              音箱 aplay 播放书库原始音频
```

音频数据面**完全不走 BookOrbit 的 HTTP 接口**：书名与路径取自它的 Postgres，
文件本身来自挂载进容器的书库目录，所以不需要 JWT。

## 3. 关键设计决策：为什么不用 function call

后端把 `tools` 交给 LLM 让它自己选工具，在这套组合里是**断的**：
Hermes 的 api_server 用自己的 toolsets，请求体里的 `tools` 只进请求指纹做幂等，
LLM 根本看不到 `play_audiobook` 这个函数。表现就是「明明注册了函数，模型永远不会调」。

所以改成在 `connection.py` 里、**调 LLM 之前**做确定性意图预判：

- 命中 → 自己构造一个 `play_audiobook` 的 tool_call 塞进推理流，后面的链路
  与模型自己调工具完全一致（`_merge_tool_calls` → `unified_tool_handler`）
- 不命中 → 什么都不做，走原来的 LLM 路径

预判只做两级匹配——**完整书名**与**核心名**——并且要求「播放 / 放 / 听 / 续播」
一类动作词在场。宁可漏判交给 LLM 正常回答，也不能把「三体讲了什么」
误判成播放指令：误判比漏判危险得多。

## 4. 服务端改动

| 位置 | 改动 |
| --- | --- |
| `plugins_func/functions/bookorbit_audiobook.py` | 新增：播放插件，FILE 型入队 |
| `plugins_func/functions/bookorbit_intent.py` | 新增：书名匹配器（连接层与插件共用） |
| `data/.config.yaml` | `Intent.function_call.functions` 加 `play_audiobook` |
| `core/connection.py` | 补丁：注入 A（意图预判）+ B（书名装载器） |

`connection.py` 的补丁由 `patches/make_connection_patch.py` 生成，
两段注入都插在确定的锚点上，脚本对锚点做 `assert old in text` 检查——
上游改版后锚点失配会立刻报错，而不是静默跳过。

播放本身复用 `play_music` 已经验证过的 FILE 分支（`base.py` 里
`ContentType.FILE` → `_process_before_stop_play_files()`），
区别只在数据来源与「续播要先裁掉已听部分」。

## 5. 桥接侧改动

音箱上有两套互不知情的播放通路：**原生音色念提示语**（miplayer）与
**播放服务端音频帧**（aplay）。这带来两个必须处理的交互：

### 5.1 提示语不与书声重叠

服务端的 `index_stream._process_remaining_text_stream(is_last=True)` 会把
**提示语尾段和音频背靠背入队**（先塞文本，紧接塞音频），两条通路在音箱上必然撞车。

- **治本（插件侧）**：服务端按标点切句，切剩的尾段要等到 FILE 消息才补发出去，
  而那一刻音频正好入队。让提示语**内部不带标点、末尾只留一个句号**，
  服务端收到文本时就整段切出；音频还要等解码（十几秒）才起，天然错开。
  注意书名/集名本身可能带冒号，拼接前要 sanitize。
- **兜底（桥接侧）**：`xiaozhi.py` 里 `_ab_write_or_hold()` —— 服务端 PCM 帧到达时，
  若提示语还没念完就先把帧囤住，念完再按序补写。「有没有没念完」用
  `native_tts` 现成的 `_generated_files` 生命周期判断（生成时加入、播完才移除），
  不需要改动 `native_tts.py`。

### 5.2 播放期间能用唤醒词打断

`services/audio/kws/__init__.py` 原版在 `LISTENING`/`SPEAKING` 状态下一律跳过
关键词检测（本意是防「助手自己说话触发自己」）。但长音频（有声书、音乐）
全程都处于 `SPEAKING`，于是**整个播放期间唤醒词完全失效**。

改法：只在「**原生音色正在念回复**」时跳过；「**服务端正在推音频帧**」时放行。
桥接每写一帧 PCM 都打一个时间戳（`_ab_last_frame_at`），KWS 侧据此判断
当前是不是长音频在放：

```python
streaming = (time.monotonic() - getattr(xiaozhi, "_ab_last_frame_at", 0.0)) < 1.5
skip = state == DeviceState.LISTENING or (state == DeviceState.SPEAKING and not streaming)
```

代价是播放期间麦克风会听到书声，理论上存在误唤醒；关键词阈值（`KWS_KEYWORDS_THRESHOLD`）
调高即可缓解。

## 6. 踩过的坑

| 症状 | 根因 | 修法 |
| --- | --- | --- |
| 开头提示语和书声重叠 | 提示语尾段与音频背靠背入队 + 音箱两条独立通路 | 提示语整段化（内部无标点）+ 桥接囤帧 |
| 播放中「你好小爱」喊不动 | KWS 在 SPEAKING 状态被整段跳过 | 服务端推帧期间放行唤醒词 |
| 续播时整集先从头放一遍 | 插件把「原始文件」和「裁剪版」**都**入队了 | `if 起播位置 > 0: 裁剪版 else: 原始文件`，二选一 |
| 改了挂载的 .py，容器行为没变 | 编辑器/脚本是原子写（换 inode），bind mount 绑的是旧 inode | `--force-recreate`，并用 `md5sum` 双侧比对确认 |
| 报 `cannot import name 'all_function_registry'` | 验证脚本桩掉了 `plugins_func.register`，import 链断了 | 别桩，让整个 import 链按生产路径走 |
| 同名书取到了电子书 | 库同一书名可能有 audio/azw3/epub/... 多条记录 | 查询限定 `library_id` |
| 非 mp3 的文件 pydub 抛错 | 格式写死 `mp3` | 按文件扩展名传 `format=` |
| 裁剪后文件被删导致播放中断 | 清理逻辑只保留自己 output_dir 下的文件 | 裁剪产物落到服务端 `tmp/` |

**验证方法上的教训**：改完 bind mount 的文件后，「容器里 grep 到新代码」不算数——
必须 `md5sum` 宿主与容器两侧比对。同理，测试不要复制粘贴代码片段来跑，
用 AST 从**要部署的那份源码**里取出方法执行，保证「测的 = 跑的」。

## 7. 端到端时序（实测）

| 时刻 | 事件 |
| --- | --- |
| T+0 | 用户说「播放有声书三体」，意图预判命中 |
| T+0.x | 提示语整段一次切出（46 字） |
| T+7.2 | 提示语念完（耗时 7.19s） |
| T+7.5 | 桥接收到服务端首帧音频，开始出声 |
| T+61 | 用户喊「你好小爱」打断，回到待命 |

播完一集后服务端起新一轮会话，等待 `wakeup.timeout`（默认 20s）：
这期间说「继续播放 X」可接着听，没人说话则回待命并播一句收尾语。

## 8. 已知边界

- **一次只播一集**，不自动连播。
- **不写回播放进度**：续播起点是平台数据库里记录的刻度，不是用户被打断/停止的位置
  （写回要处理「自然播完」与「被打断」两种语义，第一版没做）。
- 播放期间助手**不采集**用户说话（VAD 暂停 + 录音流停），避免书声误触发；
  交互只能通过中断类的入口（唤醒词、小爱自身 ASR）。
- 书名匹配依赖库里的目录名，ASR 错字由核心名匹配 + 模糊匹配兜底，极端口音仍可能漏判。

## 9. 相关文件

- `deploy/bookorbit/` —— 服务端插件、补丁生成器、验证探针、compose/env/配置片段、接入说明
- `examples/xiaozhi/xiaozhi/xiaozhi.py` —— 桥接：提示语囤帧 + 首帧诊断
- `examples/xiaozhi/xiaozhi/services/audio/kws/__init__.py` —— 唤醒词放行
