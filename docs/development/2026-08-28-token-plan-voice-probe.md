# Token Plan 语音能力探测（脱敏）

日期：2026-08-28。探测只使用授权账户的进程环境变量；凭据未写入仓库、数据库、日志或本文件。调用文本固定为“现在开始今天的学习计划。”，不包含孩子身份、家庭数据或音频。

## 实测结果

- `GET /compatible-mode/v1/models`：HTTP 200，约 1.64 秒，列出 12 个模型；语音候选为 `qwen-audio-3.0-tts-plus` 和 `qwen-audio-3.0-realtime-plus`。
- OpenAI-compatible `POST /audio/speech`（TTS 候选、MP3 请求）：HTTP 400 `InvalidParameter`，错误为 URL/protocol 不兼容；没有返回音频。因此现有 OpenAI TTS provider 不可直接复用。
- 专用 WebSocket TTS：`wss://token-plan.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference`，模型 `qwen-audio-3.0-tts-plus`，系统音色 `longanlingxi`，固定文本调用成功；返回 50,971 bytes 的 MP3（24 kHz、单声道、256 kbps）。总时延 3.805 秒，SDK 首包指标约 3.09 秒。
- `qwen-audio-3.0-realtime-plus` 被 `/models` 列出，但需要独立 realtime WebSocket 协议；本次没有上传/录制音频、没有测试语音克隆，也没有把它标为可用。

## 集成边界

需要新增一个只在家长启用后加载的 WebSocket/DashScope-compatible TTS provider，支持流式音频、中文音色参数和 CPU 播放回退。不得把它伪装成 OpenAI `/audio/speech`，不得默认发送孩子录音或任何家庭数据。上线前还需在家庭 Windows 最低配置上测量首包、断网恢复、播放中断和成本。
