# VRChat-to-API

把 VRChat 里的真人「变成」一个 OpenAI 兼容的 API。

收到 `chat/completions` 请求后，取**最后一条 user 消息**通过 OSC 发到 VRChat 聊天框；
然后用 Soniox 实时语音识别捕获（系统音频里）别人说的话，作为 API 的返回结果。

## 工作流程

```
POST /v1/chat/completions
  └─ 按需启动 STT 流 + 系统音频采集（不在请求间监听）
     └─ 取最后一条 user 消息，分页 + 循环翻面发到 VRChat /chatbox/input
        └─ 收集请求之后的 final 文本
           └─ 结束判定：endpoint（含 <end> token）且静音≥min_silence
              ；若 endpoint 始终不来，静音超过 fallback 秒也返回；上限 max_wait
              └─ 流式：逐段回传已确认内容；非流式：汇总后一次返回
                 └─ 请求结束后停止 STT 流 + 采集
```

**按需监听**：只在处理请求时开流、采音，完事即停——请求之间不监听。

## 安装

```powershell
pip install -r requirements.txt
copy .env.example .env   # 然后编辑 .env
```

`.env` 至少要配 Soniox：填 `SONIOX_API_KEY`（永久 key）**或** `SONIOX_TEMP_KEY_URL`（临时 key ）。

## 运行

确保 VRChat 已开启 OSC（默认监听 `127.0.0.1:9000`）。

```powershell
python main.py
```

启动后服务在 `http://127.0.0.1:8080/v1`。

## 调用示例

非流式（curl）：

```bash
curl http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"vrchat-human","messages":[{"role":"user","content":"你好"}]}'
```

流式：加 `"stream": true`，用 `curl -N` 观察逐段（已确认部分）输出。

OpenAI Python SDK：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="不校验")
resp = client.chat.completions.create(
    model="vrchat-human",
    messages=[{"role": "user", "content": "在吗？"}],
)
print(resp.choices[0].message.content)
```

任何兼容 OpenAI 的前端（SillyTavern 等）把 base_url 指过来即可。

## 音频源

- 默认 `AUDIO_SOURCE=system`：捕获系统音频回环（loopback），天然只录别人的声音（不含自己的麦克风）。
- **只录 VRChat**：把 VRChat 的输出在 Windows「应用音量和设备首选项」里指到一个独立/虚拟输出设备
  （如 VB-Cable），再设 `LOOPBACK_DEVICE_NAME=<设备名(可部分匹配)>`，即可只捕获该设备的音频。
- 也支持 `microphone` / `mix`（见 `.env.example`）。

## 回复结束判定

一次请求的回复在**同时满足**「Soniox 端点检测」（端点信号包括 `endpoint_detected` 标志
**或** `<end>` token）与「静音 ≥ `CAPTURE_MIN_SILENCE_SECONDS`」时结束。

兜底：若 Soniox 始终不发端点（连续 loopback 噪声下可能发生），讲完后静音超过
`CAPTURE_SILENCE_FALLBACK_SECONDS`（默认 4s）也会返回，避免一直干等到上限。
最长等待 `CAPTURE_MAX_WAIT_SECONDS`，仍无人回复则返回 `CAPTURE_NO_REPLY_MESSAGE`。

## 长消息聊天框轮播

请求文本超过 VRChat 144 字上限时，按逗号/句号等断句符分页，在聊天框循环翻面播放，
直到该请求的回复返回才停止并清空。每页停留 = `max(CHATBOX_MIN_PAGE_SECONDS, cjk/CJK_CPS + other/LATIN_CPS)` 秒。

## 悬浮窗口

启动后默认弹出一个**置顶悬浮窗**（Tkinter，无需额外依赖），实时显示：

- **状态**：`● 监听中`（绿）/ `● 空闲`（灰）——是否正在听
- **发送**：本次发到 VRChat 聊天框的消息
- **识别**：实时识别结果（已确认为白色，临时假设为灰色）
- **回复**：本次请求最终返回的内容（端点检测命中时带 ⏹ 标记）

窗口可拖动，点右上角 `✕` 关闭（关闭即退出程序）。

- **开关**：在 `.env` 里设 `SHOW_OVERLAY=false` 即可关闭窗口、纯无头运行；不设或为 true 则默认显示。
- **高 DPI 适配**：自动声明进程 DPI 感知，并按真实 DPI（96=100%）缩放窗口尺寸与字体，
  在 150%/175%/200% 缩放的屏幕上都清晰不模糊、大小合适。`OVERLAY_OPACITY` 可调透明度。
- 若运行环境无显示 / Tk 不可用，会自动降级为无头模式，不影响服务。

## 文件结构

| 文件 | 作用 |
|------|------|
| `config.py` | 配置（env 读取） |
| `soniox_client.py` | 临时/永久 key 获取 + STT config |
| `audio_router.py` | 无缝轮转的音频路由 + 静音检测（移植） |
| `audio_capture.py` | loopback / 麦克风 / 混音采集（移植） |
| `stt_engine.py` | 按需 STT 引擎 + 无缝流轮转 + 事件发布 |
| `osc_sender.py` | OSC 聊天框发送 + 分页轮播 |
| `capture.py` | 单次请求的回复捕获与结束判定 |
| `api_server.py` | FastAPI OpenAI 兼容端点 |
| `overlay.py` | 置顶悬浮窗（实时识别 + 状态，高 DPI 适配） |
| `main.py` | 启动入口 |

## 注意

- `ten-vad` 为可选依赖（更准的静音检测），缺失时自动退化为能量检测，不影响功能。
- 物理上只有一个真人，请求按全局锁串行处理。
