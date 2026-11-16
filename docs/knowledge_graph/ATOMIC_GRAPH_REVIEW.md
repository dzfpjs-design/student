# 原子知识图谱审核器

在本机启动：

```sh
python3 services/local_agent/atomic_graph_review_server.py --open
```

命令会启动服务并打开 `http://127.0.0.1:8765`。不要直接双击
`atomic_graph_review_web/index.html`；文件页面不能自行读取 SQLite。若误开文件页面，它会自动探测已运行的本地服务并跳转；未探测到服务时会显示启动与重试说明。

服务默认从
`<本机用户目录>/.home-study-companion/knowledge-build/atomic-full-v4.db` 读取当前全量构建结果，并把审核事件写入仓库现有运行库
`<本机用户目录>/.home-study-companion/home-study.db`。

构建库通过 SQLite `mode=ro` 和 `PRAGMA query_only=ON` 双重只读打开；服务不会上传、复制或改写教材、视频、学生或家庭数据。审核决定与不可变事件写入应用库的 `kg_atomic_review_decision` 与 `kg_atomic_review_event`，因此刷新或服务重启后仍会恢复。

界面按年级、学科、教材章节和有效审核状态筛选；每次最多加载 100 个节点。点击节点可查看能力边界、三层诊断、掌握标准、视频逐字证据、教材页证据、课标对齐和有方向的候选关系。质量队列从实时构建数据计算重复标题、孤立节点、前置关系循环、缺失证据和待审核定义。
