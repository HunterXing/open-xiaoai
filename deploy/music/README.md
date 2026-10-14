# 音乐库播放（xiaozhi-esp32-server 侧）

让语音助手播放**自建音乐库里的原始音频文件**，而不是让大模型去哼唱或只能打开
音箱自带曲库。音乐库侧只要支持 Subsonic API（Navidrome、Airsonic、gonic 等），
播放链路走 xiaozhi-esp32-server 已有的 FILE 型音频机制（与自带的 `play_music`
同一分支），不经过 TTS 合成。

```
说「播放音乐」（泛化）        → 随机挑几首，一次排进队列
说「播放海阔天空」（歌名）     → 匹配到就播这一首
说「播放陈奕迅的歌」（歌手）   → 排该歌手若干首
        │
        ▼
connection.py（打了补丁）── 调 LLM 之前先做确定性意图预判
        │   候选词丢给音乐库的搜索接口「搜索即校验」，命中才造 tool_call
        ▼
plugins/play_music_library.py
        │   按 id 从音乐库流式取回原始文件 → 落 tmp/
        │   提示语入队(TEXT) + 音频文件入队(FILE)
        ▼
xiaozhi-esp32-server 的 TTS 队列 ──► 桥接 ──► 音箱 aplay 出声
```

与有声书那套是同一个机制、同一条音频通路，区别只在数据面：书只有几十本，可以把
书名全塞进内存做文本匹配；曲库动辄上千首、还有歌名/歌手/专辑多种说法，所以改成
**搜索即校验**——把候选词交给音乐库自己的搜索引擎，搜到且足够像才拦，搜不到一律
交给 LLM 正常回答。

## 为什么不是让 LLM 选工具

和有声书同一个原因：后端把 `tools` 交给 LLM 选工具这条路在这套组合里是**断的**。
Hermes 的 api_server 建 agent 时用自己的 toolsets，请求体里的 `tools` 只进请求指纹
做幂等，LLM 根本看不到 `play_music_library`。所以改在 `connection.py` 里、**调 LLM
之前**做确定性预判，命中就自己造 tool_call，绕开这条断掉的通路。

## 接入四步

### 1. 音乐库侧：建一个专用 API 用户

**不要用管理员账号**，给语音助手单独建一个普通用户：

```bash
./nd_create_api_user.sh xiaozhi        # Navidrome：容器内需要真 TTY，脚本已处理
```

脚本会随机生成口令、只在 600 权限文件里流转、输出按值打码，最后把口令文件路径
打印出来，自己 `cat` 一次填进 `.env` 即可（口令别贴进聊天窗口/工单）。

> Navidrome 的 `user create` 必须接真 TTY（`docker exec` 要带 `-it`），否则会失败；
> 而且它的口令在库里是**可逆加密**存储（Subsonic token 鉴权需要明文校验），
> 所以**不能**跳过 CLI 直接往 SQLite 里写哈希。踩过的坑都在脚本注释里。

### 2. 服务端：合并 compose 片段

把 [`docker-compose.additions.yml`](docker-compose.additions.yml) 的 `environment` /
`env_file` / `volumes` 三段合并进你现有的 `xiaozhi-esp32-server` 服务定义。

⚠️ **音频落点必须是相对路径 `tmp`**：服务端清理临时文件的判断是
`tts_file.startswith(output_dir)`，而配置里 `output_dir` 就是相对的 `tmp/`。
填绝对路径会永远匹配不上——每首歌在磁盘上留一份，一首可能上百 MB。

⚠️ 音乐库**不需要**给容器挂目录：曲目是按 id 经 HTTP 取回的原始文件，
容器看不到音乐库的文件系统也没关系。

### 3. 函数白名单

按 [`../bookorbit/config-snippet.yaml`](../bookorbit/config-snippet.yaml) 往
`data/.config.yaml` 的 `Intent.function_call.functions` 里加 `play_music_library`
（那里同时写着有声书那一行，两个功能共用同一份白名单）。

⚠️ 这个列表是**整体替换**语义，必须把镜像自带的函数名一起写全，否则其余工具会静默失效。

### 4. 凭据

按 [`env.example`](env.example) 建 `.env`（`chmod 600`，不要提交）。

### 5. 生成 connection.py 补丁并启动

补丁脚本与有声书**共用同一份**（`../bookorbit/patches/make_connection_patch.py`），
它按「有声书 → 音乐库 → LLM」的顺序注入两段代码：

```bash
# 基线：从镜像里取原版 connection.py
docker cp xiaozhi-esp32-server:/opt/xiaozhi-esp32-server/core/connection.py \
    ../bookorbit/patches/connection.py.orig
# 生成（产物 plugins/connection_intent_patch.py，被 compose 挂进容器）
python3 ../bookorbit/patches/make_connection_patch.py

docker compose up -d --force-recreate     # 改的是 bind mount 文件，必须重建
```

### 6. 验证

```bash
# ① 匹配器对着真实曲库跑正反例（宿主上跑，读 .env）
python3 test_music_intent.py

# ② 容器内端到端：真搜索 / 真下载 / 真解码
docker exec -i xiaozhi-esp32-server python3 - < probe_music_in_container.py

# ③ 文字直送端到端（不吵人、可反复跑）：把一句话当用户输入喂进服务端，
#    数回来的二进制音频帧。注意用 **真机格式**（ASR 的 JSON 外壳）才算数。
docker cp ws_text_probe.py xiaozhi-esp32-server:/tmp/
docker exec xiaozhi-esp32-server python3 /tmp/ws_text_probe.py \
  '{"content": "播放音乐", "language": "zh", "emotion": "😶"}' 20
```

`probe_music_in_container.py` 会在容器里真的 import `core.connection`、真查一次
音乐库、真跑一遍匹配器和 tool_call 解包、真下载一首歌并用**服务端自己的解码链路**
解出 PCM 帧数——它不复刻代码（复刻会掩盖真实的 import 缺失）。

`ws_text_probe.py` 是**验证链路的最终裁判**：输出里出现
`stt text='% play_music_library'` 说明预判命中并生成了播放指令，
末尾的「二进制音频帧 > 0」说明服务端真的在往设备推音频。
只用纯文本测会漏掉「ASR 外壳没解包」这类只在生产入参下才暴露的失败，
两种格式都要跑。

## 音箱侧：提示语不与音乐重叠

与有声书共用同一条修法，见 [`../../examples/xiaozhi/`](../../examples/xiaozhi/)：

- **插件侧（治本）**：提示语内部不带标点、末尾只留一个句号，让服务端按切句规则
  **整段一次**切出去；音频还要等下载+解码才起，天然错开。
- **桥接侧（兜底）**：`xiaozhi.py` 在提示语没念完前先把 PCM 帧囤住，念完再补写。

实测时延（127MB / 24bit 高码率单曲）：本地流式取回 1.4 秒，解码 0.9 秒，
播报这 2~3 秒正好把等待盖住。同一首歌让音乐库转成 mp3-320 反而要 5.1 秒
（转码吃 CPU），所以默认 `format=raw` 原样取回，不转码。

## 播放期间的交互

- 播放中**可以打断**：小爱自身的 ASR，以及修好的唤醒词路径（见有声书文档说明）。
- 一批播完**不会立刻退出**：服务端起新一轮会话等 `wakeup.timeout`，
  这期间可以继续说「播下一首」「换一首」。

## 已知边界

- **匹配不到就交给 LLM 正常回答**，不会瞎放。「播放新闻」这类词搜不到就直接放行。
- 泛化请求（「播放音乐」「放首歌」）挑随机曲时会**避开明显坏标签**（`[Unknown Artist]`、
  `Track NN`），但标签本身的质量取决于你的曲库。
- 歌手名歧义时可能取到翻唱/合辑（曲库里同名歌曲多，按相似度取最优一条）。
- 单曲默认上限 320MB（`NAVIDROME_MAX_BYTES`），超了跳过该曲并记日志。

## 排错

| 症状 | 查什么 |
| --- | --- |
| 说「播放 X」没反应，正常回答了 | `data/.config.yaml` 白名单是否含 `play_music_library`；容器重建过没有 |
| 说歌名播不出来，但说「播放音乐」正常 | 音乐库搜索接口搜不到该词——先在后端 UI 里搜一次，确认曲目已入库 |
| 提示语说完等很久才出声 | 曲目文件特别大；或音乐库不在本机、网络慢。可改 `NAVIDROME_STREAM_FORMAT=mp3` 换小文件（代价是转码 CPU） |
| 磁盘被占满 | `output_dir` 是否被改过、`XIAOZHI_TMP_DIR` 是否填了绝对路径（见上文⚠️） |
| 容器起来就报 `cannot import name 'all_function_registry'` | 探针里别桩 `plugins_func.register`，会让 import 链断掉 |
| 日志里 `音乐意图匹配器初始化失败` | `NAVIDROME_URL` / 用户 / 口令是否都对；容器内能不能连到音乐库 |
