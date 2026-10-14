#!/usr/bin/env python3
"""文字直送端到端探针：把一句话当作用户输入喂进服务端，观察完整链路。

用法（容器内）：python3 ws_text_probe.py "播放音乐" [监听秒数]

判定"真的在推音频"的证据 = 收到二进制音频帧的数量（>0 即链路通）。
"""
import asyncio
import json
import sys
import uuid

import websockets

URL = "ws://127.0.0.1:8000/xiaozhi/v1/"
DEVICE_ID = "AA:BB:CC:DD:EE:FF"  # 探针专用假设备号，避免与真机抢连接
CLIENT_ID = str(uuid.uuid4())


async def main(text, listen_seconds):
    headers = {
        "device-id": DEVICE_ID,
        "client-id": CLIENT_ID,
        "protocol-version": "1",
    }
    async with websockets.connect(URL, additional_headers=headers, max_size=None) as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        }))
        hello = await asyncio.wait_for(ws.recv(), timeout=10)
        print("<< HELLO:", hello if isinstance(hello, str) else f"<{len(hello)}B binary>")

        await ws.send(json.dumps({
            "type": "listen",
            "state": "detect",
            "mode": "auto",
            "text": text,
        }))
        print(f">> TEXT: {text!r}")

        audio_frames = 0
        audio_bytes = 0
        deadline = asyncio.get_event_loop().time() + float(listen_seconds)
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if isinstance(msg, bytes):
                audio_frames += 1
                audio_bytes += len(msg)
                continue
            try:
                obj = json.loads(msg)
            except Exception:
                print("<< raw:", msg[:200])
                continue
            t = obj.get("type")
            if t == "tts":
                print(f"<< tts state={obj.get('state')} text={(obj.get('text') or '')[:80]!r}")
            elif t == "stt":
                print(f"<< stt text={(obj.get('text') or '')[:80]!r}")
            elif t == "llm":
                print(f"<< llm emotion={obj.get('emotion')} text={(obj.get('text') or '')[:80]!r}")
            elif t == "mcp":
                payload = json.dumps(obj.get("payload", {}), ensure_ascii=False)
                print(f"<< mcp {payload[:160]}")
            else:
                print(f"<< {t}: {json.dumps(obj, ensure_ascii=False)[:160]}")

        print(f"\n=== 结果：二进制音频帧 {audio_frames} 个 / {audio_bytes} 字节 ===")
        print("=== 判定：", "✅ 服务端真的在推音频" if audio_frames else "❌ 没有音频推出", "===")


if __name__ == "__main__":
    text = sys.argv[1] if len(sys.argv) > 1 else "播放音乐"
    seconds = sys.argv[2] if len(sys.argv) > 2 else "20"
    asyncio.run(main(text, seconds))
