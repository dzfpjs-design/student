# 知识图谱 CPU 基准记录（非 Windows 发布证据）

运行时间：2026-08-28；命令：

```bash
PYTHONPATH=services/local_agent/src .venv/bin/python \
  services/local_agent/scripts/benchmark_knowledge_graph.py \
  --resources 100 --chars-per-resource 8000 --vector-dimensions 64 --queries 20
```

| 指标 | 本机实测 |
|---|---:|
| 环境 | macOS arm64，32 CPU，512 GiB RAM |
| 资源 / 去重片段 | 100 / 107 |
| 导入耗时 / 吞吐 | 0.953 s / 104.94 资源每秒 |
| FTS + CPU 向量检索 p50 / p95 | 9.44 / 9.68 ms |
| 峰值 RSS | 24,870,912 bytes |
| 基准 SQLite 大小 | 2,764,800 bytes |
| 原始 64 维向量估算 | 27,392 bytes |
| GPU / 网络依赖 | 无 / 无 |

该结果只证明 CPU 回退路径能运行，**不是** Windows x64、4 核 8 线程、16GB RAM 的性能声明。发布前仍须在该最低机器上以同一命令采集 JSON，并补做外置 SSD 拔出、服务重启、审核保存/撤销后刷新和至少两小时负载观察；这些均为目标设备或产品验收阻塞项。
