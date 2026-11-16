# 本地知识图谱运维与验证

导入只读取本机文件。没有 HTTP 客户端、模型网关或云上传路径。

```bash
cd services/local_agent
PYTHONPATH=src python3 scripts/ingest_knowledge_corpora.py \
  --app-db /path/to/home-study.db --corpus-id seyk-2026 --title '苏e优课本地库' \
  --source-kind seyk --transcripts <本机用户目录>/seyk_kb/transcripts \
  --videos <本机用户目录>/seyk_kb/videos --idempotency-key seyk-2026-r1
```

对于 Bilibili，本地元数据库作为可选标题、年级、学科和学期来源：

```bash
PYTHONPATH=src python3 scripts/ingest_knowledge_corpora.py \
  --app-db /path/to/home-study.db --corpus-id bili-2026 --title 'Bilibili本地教育库' \
  --source-kind bilibili --transcripts <本机用户目录>/bili_edu_kb/transcripts \
  --videos <本机用户目录>/bili_edu_kb/videos_local --metadata-db <本机用户目录>/bili_edu_kb/bili.db \
  --idempotency-key bili-2026-r1
```

同一个幂等键可安全重跑。`checkpoint_cursor` 在每一资源提交后更新；异常写入 `kg_ingest_failure`，不会使已提交资源丢失。每次运行持有 SQLite 持久 `run_token` 租约，第二个恢复进程只会返回已有进度，不能同时推进同一 checkpoint；运行中的心跳持续续租，而且资源/映射实际写入事务会再次核验 token，失去所有权的旧 worker 不能提交副作用。正常批次结束会释放租约，进程崩溃后两分钟可安全接管。`processed_count` 和 `failed_count` 分别是当前成功资源数与当前开放失败资源数，重试次数只保存在失败队列，不能把一次资源的多次尝试计入覆盖率。暂时中止时带 `--max-resources N`，以后用同一个键继续。`--hash-videos` 会以 1 MiB 流式块计算完整 SHA-256；对 240GB 视频集应安排离线维护窗口。未启用时仍记录大小/mtime 指纹，转录始终完整 SHA-256。

课程目录/候选映射可安全重跑；关键词映射 v3 会重建仍为 `unreviewed` 的自动候选：对已有资源学科/适用年级元数据实施硬过滤，并使用收紧后的术语策略。它不会删除原始资源、转录、人工批准或人工拒绝记录：

```bash
PYTHONPATH=src python scripts/bootstrap_knowledge_graph_catalog.py \
  --app-db C:\\HomeStudy\\home-study.db
```

高拍仪资料先在本机 OCR 成每页 UTF-8 `.txt`，再由 `LocalDocumentEvidenceIngestor` 以同一幂等键导入。它复用已有的 checkpoint、失败队列与 CPU 限流；扫描图只保存相对定位和大小/mtime 指纹，只有显式 `hash_images=True` 才流式计算图片 SHA-256。`textbook`、`school_official_practice` 和 `purchased_practice_book` 保留为不同 `document_kind`，供后续课本页、官方练习和购买练习册建立可追溯证据。

生产计数只由同一个 SQLite 在线备份快照生成。提交前先生成报告，再以 `--check` 作为文档—数据库一致性门禁；不要手工改写报告中的数字：

```bash
PYTHONPATH=src python scripts/generate_knowledge_graph_quality_report.py \
  --app-db C:\\HomeStudy\\home-study.db \
  --markdown-output ..\\..\\docs\\knowledge_graph\\GRAPH_QUALITY_REPORT_2026-08-28.md \
  --json-output ..\\..\\docs\\knowledge_graph\\GRAPH_QUALITY_REPORT_2026-08-28.json

PYTHONPATH=src python scripts/generate_knowledge_graph_quality_report.py \
  --app-db C:\\HomeStudy\\home-study.db \
  --markdown-output ..\\..\\docs\\knowledge_graph\\GRAPH_QUALITY_REPORT_2026-08-28.md \
  --json-output ..\\..\\docs\\knowledge_graph\\GRAPH_QUALITY_REPORT_2026-08-28.json --check
```

## 最低配置：Windows x64、4核8线程 AVX2、16GB RAM、512GB NVMe

这是 CPU-only 的硬下限，不要求独立显卡。FTS5、分词、128/96/64 维确定性本地向量和掌握度更新都使用 Python/SQLite CPU 路径；NPU、集显或独显只能加速其他模型，不是本图谱的前提。

- 视频绝不复制到数据库或系统盘：`kg_resource` 只记录外置 SSD/本地盘的相对路径、SHA-256（可选）及大小/mtime 指纹。外置 SSD 拔出时资源仍可被诊断为“媒体不可用”，但已导入的索引和学生证据不丢失。
- 16GB 机器用 `--vector-dimensions 64 --max-resources 25 --throttle-ms 50`；每批可重启/续跑。32GB RAM、1TB NVMe、96/128维向量是建议线，而非必须。
- 保留至少 20GB 可用 NVMe 空间给 SQLite WAL、FTS 和升级备份；先在外置 SSD 或数据盘放视频，再把应用数据库放在有足够余量的 NVMe。不要用视频源盘做临时复制目录。
- 64维向量原始负载约为每去重片段 256 bytes，另加转录文本和 FTS 开销；导入器以单资源事务提交，避免全语料驻留内存。

在目标 Windows 最低配置上执行以下基准，并将 JSON 作为发布证据。验收目标：无 GPU、无网络下完成、检索有命中、进程未因内存不足退出；实际吞吐和 RSS 需以目标机器实测记录，不以开发机数字替代。

```bash
cd services/local_agent
PYTHONPATH=src python scripts/benchmark_knowledge_graph.py --resources 100 --chars-per-resource 8000 --vector-dimensions 64 --queries 20
```

输出中的 `query_p50_ms` / `query_p95_ms`、`peak_rss_bytes` 和 `cpu_only` 是最低机验收记录的一部分；`query_p95_under_500ms` 只能说明该微基准的检索门槛，不能代替整机 2 小时并发负载验收。

部署前再执行只读门禁；它不读取、复制或上传视频：

```bash
PYTHONPATH=src python scripts/verify_knowledge_graph_deployment.py \
  --app-db C:\\HomeStudy\\home-study.db --minimum-free-gb 20
```

再执行一次真实语料纵向验证。该脚本通过 SQLite backup API 创建临时副本，向副本写入一条专用诊断证据并重启读取；生产库和真实孩子记录均不会被写入：

```bash
PYTHONPATH=src python scripts/verify_knowledge_graph_vertical_slice.py \
  --app-db C:\\HomeStudy\\home-study.db
```

升级/回滚演练同样应先在临时副本中完成。以下脚本从最近一次升级前快照读取、在临时目录重放到**当前主 schema v67**并确认 `kg_*` 建表，随后删除副本；它不会覆盖生产库。当前应用只能向前升级，若真的要回到旧应用版本，应在应用完全关闭后由该旧版本兼容的运维流程使用已验证的升级前快照，不能用当前版本重新打开该快照（当前版本会再次升级它）。

```bash
PYTHONPATH=src python scripts/verify_knowledge_graph_upgrade_recovery.py \
  --app-db C:\\HomeStudy\\home-study.db
```

## 监控与容量

- 导入进度：`kg_ingest_job(processed_count, failed_count, checkpoint_cursor)`；失败队列：`kg_ingest_failure`。
- 完整性：`PRAGMA integrity_check`；性能：记录导入耗时、每资源片段数、FTS/向量命中数。
- 容量主要来自片段文本、FTS 和 128 维 float 向量（约 512 bytes/去重片段），不复制视频。先用 1% 语料估算再做全量导入。
- 备份：在应用关闭后做 SQLite online backup；不要复制 WAL 正在写入中的单个 `.db` 文件。

## 已验证纵向样本（2026-08-28）

真实本地初三课程（求根公式）已作为纵向验证样本：脚本确认“本机转录证据候选 → 家长或产品批准映射 → 错题确认 → 原子知识点 → 讲解片段 → 本地教师编写复测题 → 掌握度/前置弱信号 → 持久化复习计划”，并重启读取。它还重放同一 `action_id`，确认只保留一条证据，并对比生产库证据行数前后不变。自动映射只进入待审队列，未批准映射不进入学生检索、讲解或掌握度更新。

这验证了代码、真实语料和持久化纵向切片；不验证自动映射的教学准确率、完整 240GB/38GB 语料的导入时间、视频完整哈希或学生学习效果。这些分别是自动映射质量、容量、素材边界与产品价值的发布阻塞项。
