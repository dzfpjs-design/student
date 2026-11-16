# 家庭学习陪伴软件最低电脑配置与验收基线

日期：2026-08-27  
状态：工程基线，等待目标Windows电脑实测冻结

## 1. 结论

家庭版不要求独立显卡。语言模型继续使用千问或DeepSeek云API；家庭影像、作业图像和普通语音在本地处理。最低受支持电脑必须满足下表，而不是以“能安装Windows”为判断标准。

| 项目 | 最低受支持配置 | 建议配置 | 说明 |
|---|---|---|---|
| 操作系统 | Windows 11 64位，22H2或更新版本 | Windows 11 24H2 64位 | 首版只验收x64，不承诺Windows ARM |
| CPU | 4核8线程、支持AVX2的2020年后移动/桌面CPU；参考Intel Core i5-1135G7或AMD Ryzen 5 5500U及以上 | Intel Core Ultra 5 125H、AMD Ryzen 7 7840HS或同级及以上 | 购买时按本文件负载测试，不以型号名称单独判定 |
| 内存 | 16GB双通道 | 32GB双通道 | 8GB不进入支持范围 |
| 系统盘 | 512GB NVMe SSD，安装后至少保留120GB可用空间 | 1TB NVMe SSD | 禁止以机械硬盘或eMMC作为系统和数据库盘 |
| 完整视频语料 | 不放在512GB系统盘；使用外置SSD或只保留索引和按需片段 | 2TB本地/外置SSD，或1TB系统盘加1TB语料盘 | 现有视频约278GB，尚未计入索引、备份和增长空间 |
| 显卡 | 集成显卡即可 | 现代集成GPU/NPU；独显可选 | 当前发布路径不依赖CUDA |
| USB | 至少2个可同时工作的USB 3.x端口 | 3个USB 3.x端口或带独立供电的合格扩展坞 | 观察摄像头与4K高拍仪必须双路并发验证 |
| 网络 | 稳定宽带，模型API可访问 | 有线网络或稳定Wi-Fi 6 | 图像和连续音视频默认不上云，带宽不是主要瓶颈 |
| 显示器 | 1920x1080 | 1920x1080或更高，21.5至27英寸 | 保证讲题、作业确认和家长设置可读 |

明确不推荐：Intel N100/N95同级低功耗机、8GB内存、仅256GB硬盘、机械硬盘系统盘、Windows ARM设备。OpenVINO或ONNX Runtime“能够支持”某个处理器不代表本产品在该处理器上具有可接受的并发体验。

## 2. 负载依据

当前主程序为Electron、React和Python本地服务组合。本地运行负载为：

- 观察摄像头维持1080p预览，每2秒抽取一帧，分析前降采样；不对30FPS原始流逐帧运行视觉模型。
- 高拍仪保留4K预览和单帧拍摄，仅在拍题、检查和错题录入时执行OCR。
- RapidOCR 3.9.2通过ONNX Runtime CPU执行；OCR是按需突发负载，不持续占用GPU。
- 本地中英ASR使用约72MB的Sherpa-ONNX Zipformer INT8模型，当前配置为2个CPU线程。
- SQLite、FTS和知识图谱投影在CPU上运行；向量索引采用本地量化嵌入和有界召回。
- 千问、DeepSeek等通用语言模型不在家庭电脑运行。
- 自然TTS优先采用经家长启用的云合成或轻量本地引擎；不得把本地大模型TTS偷偷纳入最低配置。

RapidOCR官方项目明确提供ONNX Runtime CPU路径；ONNX Runtime提供Windows x64 CPU包。OpenVINO支持Windows 10/11上的CPU，并可在受支持的Intel硬件上选用GPU或NPU，但加速器需要额外驱动。因此首版以CPU路径为发布基线，NPU和集成GPU加速只能作为优化，不能成为隐藏依赖。

参考：

- [ONNX Runtime安装与CPU包](https://onnxruntime.ai/docs/install/)
- [ONNX Runtime Windows支持](https://onnxruntime.ai/docs/get-started/with-windows.html)
- [RapidOCR官方仓库](https://github.com/RapidAI/RapidOCR)
- [OpenVINO系统要求](https://docs.openvino.ai/2025/about-openvino/release-notes-openvino/system-requirements.html)
- [Sherpa-ONNX离线转写模型说明](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-transducer/zipformer-transducer-models.html)

## 3. 为保持无独显必须遵守的算法边界

1. 行为观察使用轻量人体、姿态或时序特征模型，输入不高于640x640，常态推理不高于2FPS。
2. 4K只用于高拍仪取证和OCR，不把4K帧送入连续行为模型。
3. OCR、ASR、知识索引和行为推理必须有任务队列和并发预算；OCR突发时可降低观察分析频率，但不能断开摄像头。
4. 家庭电脑不运行7B及以上本地LLM、本地多模态大模型、扩散图像模型或持续视频大模型。
5. 拟人形象采用2D动画或轻量WebGL；不得把实时3D数字人渲染作为最低配置功能。
6. NPU/OpenVINO/DirectML优化必须保留CPU回退，并在界面中如实显示当前执行设备。

如果以后把本地多模态大模型、复杂手写数学识别或高帧率全身动作理解变成硬要求，必须建立新的“增强配置”，不能悄悄提高本文件的最低线。

## 4. 最低配置验收场景

候选电脑只有通过以下真实Windows连续负载，才能列入支持范围：

1. Electron孩子端、Python服务和SQLite同时启动，冷启动到可交互不超过15秒。
2. 观察摄像头1080p预览、高拍仪4K预览和麦克风同时打开60分钟，无冻结、设备掉线或应用崩溃。
3. 观察分析以0.5FPS运行60分钟；稳态整机CPU平均不高于65%，内存峰值不高于12GB。
4. 10秒儿童语音完成本地转写，结束说话后的P95结果时间不超过2秒；转写期间界面保持响应。
5. 真实A4/A3中文作业单页OCR P95不超过5秒；OCR峰值允许短时使用90% CPU，但不得造成音频丢失或摄像头断流。
6. 同时执行观察、语音转写、一次OCR、知识检索和云模型流式回复时，按钮反馈不超过200毫秒，界面动画不连续停顿超过500毫秒。
7. 知识点和错题混合检索P95不超过500毫秒；数据库写入、备份和应用重启后数据保持。
8. 连续运行2小时后无持续内存增长，峰值温度或降频不导致上述延迟门槛失效。
9. 断网后本地计划、OCR、ASR、观察和历史查询仍可用；云模型功能显示可恢复错误。
10. 系统盘始终保留至少20%可用空间；不足时阻止大规模语料导入并给出迁移建议。

## 5. 冻结决策

当前可用于采购和开发的结论为：**16GB内存、512GB NVMe、现代4核8线程x64 CPU、无独显**是功能裁剪后的绝对最低线；**32GB内存、1TB NVMe、Core Ultra 5/Ryzen 7同级、无独显**是建议长期使用线；如果完整保存约278GB视频语料，建议总SSD容量2TB。

这仍是工程评估，不是目标Windows真机通过。最终最低配置只有在至少一台最低档候选电脑完成第4节全部测试后才能从“暂定”改为“已验证”。
