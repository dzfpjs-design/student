# 短回应合成音频来源

仅供隔离开发测试，不是儿童声音、家庭录音或真实ASR准确率基准，不进入软件教学素材或原音资格库。

- 已安装macOS Flo中文声，以300wpm完整合成“对”，再由ffmpeg转16kHz/单声道/16bit WAV。
- short-mandarin-dui.wav：完整475ms，15266字节；SHA-256：9e2f53b24869195f0032e243f159da4ad5dc6d65a50ae07a228434c07ba7d8b3。
- short-mandarin-dui-trimmed.wav：仅去外部静音，保留20ms边缘，195ms；ffmpeg过滤参数为silenceremove=start_periods=1:start_threshold=-55dB:start_silence=0.02:stop_periods=-1:stop_threshold=-55dB:stop_silence=0.02。不是截取长句片段或编造儿童短词。
- 去静音版本6330字节，SHA-256：14e7a6d5ccfd4f6de18d8c71e2a3afe5ddcec9773b6b60245bbcc87c85d082b5。
- 本机原生产WebRTC VAD对两个文件均报告5个语音帧/最长5帧，并拒绝进入ASR。该反例支持修复准入时长，不支持声学回声消除或人声身份声明。
