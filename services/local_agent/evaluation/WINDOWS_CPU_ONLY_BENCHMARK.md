# Windows x64 无独显最低配置基准

本基准对应主 PRD 的 4 核 8 线程 AVX2、16GB RAM、512GB NVMe 最低线；建议机为 32GB/1TB。测试机必须是 Windows x64，独立显卡既不安装也不作为通过条件。

## 执行规则

1. 以真实 1080p 观察摄像头、高拍仪 4K 预览、麦克风启动 Electron 和 Python 服务，连续 60 分钟。观察模型只接收降采样后的 `<=640x640`、`<=0.5FPS`（硬上限 2FPS）帧；4K 仅用于用户触发的单帧 OCR。
2. 在这 60 分钟中收集至少 20 个 10 秒本地 ASR 样本和至少 20 个脱敏 A4/A3 单页 OCR 样本。记录从语音结束/按下拍摄到本地结果可用的秒数，不保存原媒体。
3. 同时记录整机稳态 CPU 平均、进程/系统内存峰值、摄像头断流、崩溃，以及 2 小时后的内存增长。OCR 可短时冲高，但不得丢音或断流。
4. 把**聚合数值**写进 `eval/minimum_pc_benchmark_template.json` 的副本。在目标 Windows 机器上先合并实测硬件证据（不读取页面或音频）：

```powershell
$env:PYTHONPATH = 'src'
python scripts/collect_minimum_pc_environment.py `
  --runtime-metrics eval\minimum-pc-windows-real.json `
  --output eval\minimum-pc-windows-collected.json
```

`cpuinfo` 可用时自动记录 AVX2 flags；否则只有在已按 CPU 型号核对 AVX2 后才加 `--confirm-avx2`，报告会明确该证据为 `operator_confirmed`。再执行：

```powershell
$env:PYTHONPATH = 'src'
python scripts/evaluate_minimum_pc_runtime.py eval\minimum-pc-windows-collected.json --require-collected-environment --output eval\minimum-pc-verdict.json
```

通过条件：双摄无中断 60 分钟；各至少 20 个真实样本的 ASR P95≤2s、OCR P95≤5s；稳态 CPU≤65%；峰值内存≤12GB；2 小时内存增长≤512MB。记录还必须证明行为输入≤640×640/2FPS、三类任务均走 CPU、OCR 是单帧且没有启动 ≥7B 本地模型或连续视频 VLM。退出码 `0` 才代表目标 Windows 机器通过；`2` 表示真实指标不达标。

## 资源治理验证

- `CpuResourceQueue` 只调度 CPU：行为 1 线程/400MB、ASR 2 线程/900MB、OCR 3 线程/1.8GB，受管总上限 6 线程/4GB。
- OCR 排队或运行时行为分析降至 0.25FPS；摄像头预览不属于该队列，不能被 OCR 停掉。
- OpenVINO、DirectML、NPU 是可选加速。关闭或缺失时必须重跑 CPU 路径且仍满足验收；连续视频 VLM 和 ≥7B 本地模型不允许进入最低配置测试。

## 真实作业集（单独报告各类能力）

将家长批准、去标识化的本地文件路径填入 `eval/local_ocr_real_benchmark_manifest.example.json` 的**未提交副本**，然后运行：

```powershell
$env:PYTHONPATH = 'src'
python scripts/run_local_ocr_real_benchmark.py C:\HSC-eval\manifest.json --data-dir C:\HSC-eval\runtime --output C:\HSC-eval\aggregate.json
```

输出不包含页面或识别文字。它分别报告页面检测、题块、印刷 CER 和每类失败；当前手写、数学结构、批改符号会明确列为“未评测（缺少本地专用模型）”。不得把印刷 CER 写入这些三类指标，也不得把人工修正后的正确文本回填为模型准确率。
