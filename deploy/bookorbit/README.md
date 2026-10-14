# 有声书播放（xiaozhi-esp32-server 侧）

让语音助手直接播放**书库里的原始音频文件**，而不是让大模型去朗读文本。
数据来源是 BookOrbit 的库（Postgres 里的书目录 + 挂载进容器的音频文件），
播放链路完全走 xiaozhi-esp32-server 已有的 FILE 型音频机制（与自带的
`play_music` 同一分支），不经过 TTS 合成。

```
说「播放有声书三体」
        │
        ▼
connection.py（打了补丁）── 调 LLM 之前先做确定性意图预判
        │   命中 → 自己造一个 play_audiobook 的 tool_call 塞进推理流
        ▼
plugins/bookorbit_audiobook.py
        │   查 Postgres：书名 → book_id → book_files.absolute_path / 进度
        │   提示语入队(TEXT) + 音频文件入队(FILE，续播时先裁掉已听部分)
        ▼
xiaozhi-esp32-server 的 TTS 队列 ──► 桥接 ──► 音箱 aplay 出声
```

## 为什么不用 function call

后端把 `tools` 交给 LLM 选工具这条路，在这套组合里是**断的**：Hermes 的
api_server 用自己的 toolsets，请求体里的 `tools` 只进请求指纹做幂等，LLM 根本
看不到 `play_audiobook`。所以改成在 `connection.py` 里、**调 LLM 之前**做
确定性意图预判：命中就自己造 tool_call，绕开这条断掉的通路。

预判只做两级匹配（完整书名 / 核心名），宁可漏判交给 LLM 正常回答，也不能把
「三体讲了什么」误判成播放指令。

## 接入三步

### 1. 服务端 compose

把 [`docker-compose.additions.yml`](docker-compose.additions.yml) 里的 `env_file`
和 `volumes` 两段合并进你现有的 `xiaozhi-esp32-server` 服务定义。

书库根目录要**只读**挂载，并且**容器内路径必须与数据库里 `book_files.absolute_path`
的前缀一致**（都是 `/books/...`）——插件会做路径越界校验，不一致会直接拒绝播放。

### 2. 函数白名单

按 [`config-snippet.yaml`](config-snippet.yaml) 往 `data/.config.yaml` 的
`Intent.function_call.functions` 里加一行 `play_audiobook`。

⚠️ 这个列表是**整体替换**语义，必须把镜像自带的函数名一起写全，否则其余工具会静默失效。

### 3. 凭据

按 [`env.example`](env.example) 建 `.env`（`chmod 600`，不要提交）。

### 4. 生成 connection.py 补丁

```bash
# 1) 从镜像里取出原版 connection.py 作为基线
docker cp xiaozhi-esp32-server:/opt/xiaozhi-esp32-server/core/connection.py \
    patches/connection.py.orig

# 2) 生成补丁版（脚本会把两段注入插到确定的锚点上，找不到锚点会 assert 报错）
python3 patches/make_connection_patch.py

# 3) 产物即 plugins/connection_audiobook_patch.py，被 compose 挂载进容器
```

注入分两段：**A** 在 `response_with_functions` 之前拦一道意图预判；
**B** 模块级补上 `_load_audiobook_titles()` 和匹配器。脚本对锚点做了
`assert old in text` 检查，上游改版后锚点失配会立刻报错而不是静默跳过。

### 5. 启动与验证

```bash
docker compose up -d --force-recreate      # 改的是 bind mount 文件，必须重建
python3 patches/probe_final_in_container.py
```

探针会在**容器内**真的 import `core.connection`，检查补丁依赖的模块级名字是否
都在、真查一次书库、真跑一遍匹配器和 tool_call 解包。它不复刻代码（复刻会掩盖
真实的 import 缺失）。

## 音箱侧：两句提示语不与书声重叠

服务端的 `_process_remaining_text_stream(is_last=True)` 会把**提示语尾段和音频
背靠背入队**，而音箱上「原生音色念提示语」与「播放音频帧」是两条独立通路，
开头必然叠在一起。修法见本仓库 `examples/xiaozhi/`：

- **插件侧（治本）**：提示语内部不带标点、末尾只留一个句号，让服务端按切句规则
  **整段一次**切出去；音频还要等解码才起，天然错开。
- **桥接侧（兜底）**：`xiaozhi.py` 在提示语没念完前先把 PCM 帧囤住，念完再补写
  （`_ab_write_or_hold`）。

## 播放期间的交互

- 播放中**不听**用户说话（VAD 暂停 + 录音流停），避免书声把自己触发。
- **可以打断**：小爱自身的 ASR（说话即触发 `on_interrupt`），以及修好的唤醒词
  「你好小爱」——`examples/xiaozhi/xiaozhi/services/audio/kws/__init__.py` 里
  放行了「服务端正在推音频帧」时的唤醒词，仍保留「原生音色念回复时跳过」防自触发。
- 一集播完**不会立刻退出**：服务端起新一轮会话等 `wakeup.timeout`（默认 20s），
  这期间说「继续播放 X」可接着听，没人说话才回待命。

## 已知边界

- **一次只播一集**，不自动连播。
- **不写回播放进度**：续播起点是平台记录里的刻度，不是你刚被打断的位置。
- 书库路径按 `library_id` 过滤——同一书名在库里可能同时有电子书与音频多条记录，
  不加这层过滤会取错。

## 排错

| 症状 | 查什么 |
| --- | --- |
| 说「播放 X」没反应，正常回答了 | `data/.config.yaml` 白名单是否含 `play_audiobook`；容器重建过没有 |
| 容器起来就报 `cannot import name 'all_function_registry'` | 探针里别桩 `plugins_func.register`，会让 import 链断掉 |
| 提示语说完了但不出声 | 看桥接日志有没有「收到服务端首帧」；无 = 服务端没推，有 = 播放流没激活 |
| 播出来是「从头再放一遍」 | 续播分支写成了「原始文件 + 裁剪版」都入队，只能二选一 |
| 书名一直匹配不到 | 书名清单来源：先查库，查不到退回扫 `BOOKORBIT_TITLES_FALLBACK_DIR` |
