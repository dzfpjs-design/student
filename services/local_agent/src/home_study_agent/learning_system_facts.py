from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable

from .database import Database


FACT_SCOPES = ("resources", "plan", "memory", "progress", "requests", "all")
TRACKS = ("pet", "competition_math", "school", "all")


@dataclass(frozen=True)
class LearningSystemQuery:
    scope: str
    track: str


def _dialogue_text(recent_dialogue: Iterable[dict[str, Any]]) -> str:
    values: list[str] = []
    for item in list(recent_dialogue)[-6:]:
        content = item.get("content")
        if isinstance(content, str):
            values.append(content)
    return " ".join(values)


def detect_learning_system_query(
    transcript: str, recent_dialogue: Iterable[dict[str, Any]] = ()
) -> LearningSystemQuery | None:
    """Recognize questions about product-owned facts, not learning content."""

    current = transcript.strip()
    history = _dialogue_text(recent_dialogue)
    combined = f"{history} {current}".lower()
    track = (
        "pet"
        if re.search(r"(?<![a-z0-9_])pet(?![a-z0-9_])|派特|剑桥\s*b1", combined, re.IGNORECASE)
        else "competition_math"
        if re.search(r"奥数|数学竞赛|竞赛数学", combined)
        else "school"
        if re.search(r"校内|学校|课内|中考", combined)
        else "all"
    )

    scope: str | None = None
    if re.search(r"题库|知识库|教材|课程资料|学习资料|练习资源|之前.{0,8}(喂给|给过|导入|上传)", current, re.IGNORECASE):
        scope = "resources"
    elif re.search(
        r"(?:学习计划|复习计划).{0,6}(?:吗|还在|有吗|是什么|有哪些|怎么安排|进度)|"
        r"今天.{0,6}(?:计划|安排)(?:是什么|有哪些|还在|有吗|怎么安排)|"
        r"今天.{0,6}(?:有什么|有啥).{0,4}(?:计划|安排)|"
        r"(?:查看|告诉我|说说|核对|看看)今天.{0,6}(?:计划|安排)|"
        r"接下来.{0,6}(?:学|练|做什么)",
        current,
    ):
        scope = "plan"
    elif re.search(r"还记得|记忆.{0,4}(还在|有吗|保存)|你记住|我的记录.{0,4}(还在|有吗)", current):
        scope = "memory"
    elif re.search(r"掌握.{0,4}(多少|情况)|薄弱点|学习进度|提高了多少|现在水平", current):
        scope = "progress"
    elif re.search(r"(?:有|有没有|还在|能用|找得到|保存着).{0,8}(题|计划|记录|资料|知识)", current):
        scope = "all"
    elif re.fullmatch(r"(?:还有吗|还在吗|能找到吗|我之前喂给你的|我之前给过你的)[？?。！!\s]*", current):
        if re.search(r"题库|知识库|资料|教材|派特|\bpet\b|奥数", history, re.IGNORECASE):
            scope = "resources"
        elif re.search(r"计划|安排", history):
            scope = "plan"
        elif re.search(r"记忆|记录|记得", history):
            scope = "memory"

    return LearningSystemQuery(scope=scope, track=track) if scope else None


class LearningSystemFacts:
    """Read-only projection for facts the language model is not allowed to guess."""

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _table(connection, name: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def inspect(self, child_id: str, session_id: str, scope: str, track: str, *, exclude_turn_id=None) -> dict[str, Any]:
        if scope not in FACT_SCOPES or track not in TRACKS:
            raise ValueError("INVALID_LEARNING_SYSTEM_QUERY")
        if scope == 'requests':
            from .agent_request_receipt import read_recent_request_receipts
            facts = {'scope': scope, 'track': track, **read_recent_request_receipts(
                self.database, child_id, session_id, exclude_turn_id=exclude_turn_id)}
            requests = facts['requests']
            labels = {'prepared': '完整原话保存未确认', 'saved_unconfirmed': '原话已存但交接未确认',
                      'cancelled': '原话撤回已确认', 'unconfirmed': '原记录未核对成功',
                      'recorded_completed': '处理回执已保存', 'recorded_failed': '处理未完成',
                      'interpreted': '理解已存但操作未完成', 'received': '已到达但处理未确认'}
            counts = {status: sum(row['status'] == status for row in requests) for status in labels}
            response = ('最近一条：' + requests[0]['response_text'] + ' 最近核对的记录中，'
                        + '，'.join(f'{count}条{labels[status]}' for status, count in counts.items() if count) + '。'
                        if requests else '本机没有找到可核对的最近请求记录；这不能证明没有执行过，不会自动重做。')
            if facts['has_more']:
                response += '这里只核对了最近八条，其他历史记录仍然保留。'
            return {'status': 'inspected', 'response_text': response, 'facts': facts,
                    'evidence': ['learning_system_facts_read']}
        with self.database.connect() as connection:
            session = connection.execute(
                "SELECT child_id FROM daily_session WHERE session_id=?", (session_id,)
            ).fetchone()
            if not session or session["child_id"] != child_id:
                raise ValueError("LEARNING_SYSTEM_QUERY_OWNER_MISMATCH")

            params: tuple[Any, ...] = () if track == "all" else (track,)
            where = "" if track == "all" else " AND kn.track=?"
            resource_rows = [dict(row) for row in connection.execute(
                f"""SELECT kn.track,COUNT(*) AS item_count,
                           COUNT(DISTINCT kn.knowledge_node_id) AS node_count
                    FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
                    WHERE di.status='active' AND kn.status='active'{where}
                    GROUP BY kn.track ORDER BY kn.track""",
                params,
            )]

            snapshot = None
            if self._table(connection, "specialized_program_snapshot"):
                snapshot_row = connection.execute(
                    """SELECT track,snapshot_date,payload_json,generated_at
                       FROM specialized_program_snapshot
                       WHERE child_id=? AND (?='all' OR track=?)
                       ORDER BY generated_at DESC LIMIT 1""",
                    (child_id, track, track),
                ).fetchone()
                if snapshot_row:
                    payload = json.loads(snapshot_row["payload_json"])
                    snapshot = {
                        "track": snapshot_row["track"],
                        "snapshot_date": snapshot_row["snapshot_date"],
                        "domain_count": int(payload.get("catalog_domain_count") or 0),
                        "evidence_domain_count": int(payload.get("evidence_domain_count") or 0),
                        "next_action": ((payload.get("next_actions") or [{}])[0].get("title")),
                        "stage": (payload.get("stage_plan") or {}).get("stage"),
                    }

            plan_row = connection.execute(
                """SELECT plan_id,plan_date,status,total_minutes FROM daily_plan
                   WHERE child_id=? ORDER BY plan_date DESC,version DESC LIMIT 1""",
                (child_id,),
            ).fetchone()
            plan = dict(plan_row) if plan_row else None
            if plan:
                plan["items"] = [dict(row) for row in connection.execute(
                    """SELECT subject,title,status,source_type,expected_minutes FROM plan_item
                       WHERE plan_id=? ORDER BY position""",
                    (plan["plan_id"],),
                )]
                plan.pop("plan_id", None)

            memory_count = connection.execute(
                "SELECT COUNT(*) FROM companion_memory WHERE child_id=? AND status='active'",
                (child_id,),
            ).fetchone()[0] if self._table(connection, "companion_memory") else 0
            knowledge_nodes = connection.execute(
                "SELECT COUNT(*) FROM knowledge_node WHERE status='active'"
            ).fetchone()[0]
            knowledge_sources = connection.execute(
                "SELECT COUNT(*) FROM knowledge_source WHERE current_version=1"
            ).fetchone()[0] if self._table(connection, "knowledge_source") else 0
            curriculum_resources = connection.execute(
                "SELECT COALESCE(SUM(resource_count),0) FROM curriculum_corpus WHERE status='ready'"
            ).fetchone()[0] if self._table(connection, "curriculum_corpus") else 0
            graph_corpora = connection.execute(
                "SELECT COUNT(*) FROM kg_corpus WHERE review_status IN ('approved','unreviewed')"
            ).fetchone()[0] if self._table(connection, "kg_corpus") else 0
            pet_cards = connection.execute(
                "SELECT COUNT(*) FROM pet_vocabulary_card WHERE child_id=? AND status!='suspended'",
                (child_id,),
            ).fetchone()[0] if self._table(connection, "pet_vocabulary_card") else 0

        facts = {
            "scope": scope,
            "track": track,
            "diagnostic_resources": resource_rows,
            "latest_program_snapshot": snapshot,
            "latest_daily_plan": plan,
            "active_memory_count": int(memory_count),
            "active_knowledge_node_count": int(knowledge_nodes),
            "ready_knowledge_source_count": int(knowledge_sources),
            "ready_curriculum_resource_count": int(curriculum_resources),
            "local_graph_corpus_count": int(graph_corpora),
            "pet_vocabulary_card_count": int(pet_cards),
            "local_only": True,
            "cloud_transmission_allowed": False,
        }
        return {
            "status": "inspected",
            "response_text": self._speech(facts),
            "facts": facts,
            "evidence": ["learning_system_facts_read"],
        }

    @staticmethod
    def _speech(facts: dict[str, Any]) -> str:
        scope, track = facts["scope"], facts["track"]
        rows = facts["diagnostic_resources"]
        selected = next((row for row in rows if row["track"] == track), None) if track != "all" else None
        snapshot = facts["latest_program_snapshot"]
        plan = facts["latest_daily_plan"]

        if track == "pet" and scope in {"resources", "all"}:
            items = int((selected or {}).get("item_count") or 0)
            nodes = int((selected or {}).get("node_count") or 0)
            domains = int((snapshot or {}).get("domain_count") or 0)
            if items:
                suffix = f"你的 PET 长期计划覆盖 {domains} 个能力域。" if domains else ""
                vocabulary = (
                    f"个人词汇复习卡现在有 {facts['pet_vocabulary_card_count']} 张。"
                    if facts["pet_vocabulary_card_count"]
                    else "个人词汇复习卡还没有建立。"
                )
                return (
                    f"有。本机目前有 {items} 道已启用的 PET 诊断练习，关联 {nodes} 个知识节点。"
                    f"{suffix}{vocabulary}这些是本机的分项诊断资源，不冒充官方完整真题库；你可以直接让我开始 PET 练习。"
                )
            return "本机当前没有可出题的 PET 练习。我不会把计划框架说成题库；需要先补充并审核可靠题源。"

        if track == "pet" and scope == "plan":
            if snapshot:
                next_action = snapshot.get("next_action") or "先做一项未暴露答案的基线诊断"
                daily = "今天的日计划已经生成。" if plan else "今天的日计划还没有生成。"
                return (
                    f"PET 长期计划还在，覆盖 {snapshot['domain_count']} 个能力域。"
                    f"下一步是：{next_action}。{daily}你可以让我现在把它排进今天的计划。"
                )
            return "本机没有找到 PET 长期计划，我先不凭记忆编一份。"

        if scope == "plan":
            if plan:
                pending = [item for item in plan["items"] if item["status"] not in {"completed", "cancelled"}]
                next_title = pending[0]["title"] if pending else "今天的任务已没有待办项"
                return f"今天的计划还在，共 {len(plan['items'])} 项，下一项是：{next_title}。"
            return "本机还没有生成今天的日计划；长期目标记录不会因此丢失，我可以根据今天作业和当前目标生成。"

        if scope == "memory":
            return (
                f"本机有 {facts['active_memory_count']} 条仍有效的长期陪伴记忆。"
                "我只会根据保存的记录回答，不会把一段聊天误说成长期记忆。"
            )

        if scope == "progress":
            if snapshot:
                return (
                    f"最近的 {snapshot['track']} 计划覆盖 {snapshot['domain_count']} 个能力域，"
                    f"其中已有可靠学习证据的能力域是 {snapshot['evidence_domain_count']} 个。"
                    "没有独立作答证据的部分，我不会说成已经掌握。"
                )
            return "本机没有足够的阶段评估记录，我先不判断当前水平。"

        total_items = sum(int(row["item_count"]) for row in rows)
        return (
            f"本机当前有 {total_items} 道已启用诊断练习、{facts['active_knowledge_node_count']} 个有效知识节点，"
            f"以及 {facts['local_graph_corpus_count']} 个本地图谱语料库。"
            "我会按具体学科和目标查询后再回答，不会凭模型记忆判断资料是否存在。"
        )
