# 本地 OCR 依赖与许可证核验

本清单仅记录当前开发环境中实际启用的 CPU 路径；它不是对下游模型权重授权的替代。

| 组件 | 当前版本/用途 | 本机安装元数据 | 发布前状态 |
| --- | --- | --- | --- |
| RapidOCR | 3.9.2；本地印刷中英数字识别适配器 | `License-Expression: Apache-2.0` | 库许可证已记录；保留其 NOTICE/版权声明。 |
| ONNX Runtime | 1.19.2；CPU 推理运行时 | `License: MIT License` | 库许可证已记录；随发行包保留许可证文本。 |
| PP-OCRv6 det/rec small ONNX、方向分类 ONNX | RapidOCR 在本机缓存的默认检测、识别与方向模型 | 当前缓存目录未附独立 LICENSE/NOTICE | **不得仅按 RapidOCR 的 Apache-2.0 推断权重授权。** 发布前必须从模型原始发布页/随附 NOTICE 核对模型版本、许可证、再分发条件和归属；缺失则不随产品打包。 |
| Pix2Tex 公式 adapter | 可选，CPU-only，本机显式 checkpoint 路径 | `pix2tex 0.1.4` wheel 元数据：MIT；依赖含 PyTorch | 代码许可已核验；**权重不随包提供、不自动下载且当前未核验**。仅可在记录权重来源、许可、SHA-256、体积和 CPU 500 条基准后启用。 |
| Pix2Text 1.1.5 + MFR-1.5 | 可选，纯公式、ONNX Runtime CPU provider | Pix2Text 代码 MIT；`breezedeus/pix2text-mfr-1.5` 模型卡标为 MIT | 已在工作树外缓存固定 revision `1cef9f0bdcd6a4c63df7de1311fb0894593340cc`，由 `model_manifests/pix2text-mfr-1.5.json` 记录 10 个文件、SHA-256 和 119,665,593 bytes。推理仅接受校验后的本地目录并设置 offline；权重不提交 Git、不自动下载。500 MathWriting 实测质量不达自动录入阈值，必须保留人工公式编辑。 |
| Pix2Text MFD-1.5 | 未启用，整页公式检测候选 | 模型页标为 MIT（待发布包再次核验） | 尚未下载/实测；不能将 MFR 的纯公式结果外推为整页检测能力。 |
| 手写、批改符号模型 | 未启用 | 无 | 未纳入能力声明；引入前需单独完成模型来源、许可证、离线 CPU 性能和脱敏评测核验。 |

## 发布/更新门槛

1. 锁定 Python 包版本及模型哈希，并把包和权重的 LICENSE/NOTICE 归档到发行物料。
2. 对每个可再分发权重保留原始来源、发布日期、许可证文本、归属和下载校验值；无法证明授权时仅允许由家长在本机自行提供，且不随产品分发。
3. 任何可选 OpenVINO、DirectML 或 NPU 运行时均须重新核验许可证；它们只能加速既有 CPU 可用流程。
4. 不以 OCR 库、运行时或印刷模型的许可证推断手写、公式或批改模型的权利。
5. `run_mathwriting_formula_benchmark.py --provider pix2tex` 要求显式 `--checkpoint`；`--provider pix2text_mfr_onnx` 要求 `--model-dir` 与校验后的 `--model-manifest`。缺失、哈希变化或依赖不完整必须返回 `provider_unavailable`，产品保持人工公式编辑。RapidOCR 500 条基线是“非公式模型”的失败边界，不可作为公式能力或许可证证据。

当前运行时可在 Windows CPU 上缺少模型时降级为明确的 `rapidocr_not_installed`/人工校正路径；不得联网自动下载未核验的新权重。

## 共享评测资源（不得随应用打包）

| 资源 | 声明的来源许可/边界 | 本项目用途 |
| --- | --- | --- |
| Mendeley 批改前后试卷 | CC BY 4.0；英语大学数据 | 红墨、勾叉、圈画、分数和布局差分的评测，不用于中文 K-9 结论。 |
| MathWriting 官方 excerpt | CC BY-NC-SA 4.0 | 公式模型的离线评测/预处理；不作为拍摄作业准确率证据。 |
| SCUT-HCCDoc / SCUT-EPT 公共样例 | 非商业研究、完整集需申请 | 仅 smoke 与故障分析；不得报告统计中文手写准确率。 |

共享目录是只读外部评测包，运行报告不得输出原页、OCR 文本或 LaTeX 真值；许可证以 `<本机用户目录>/hsc_eval_assets/README.md` 的来源说明为准，发行前仍需复核原始上游条款。
