from __future__ import annotations

import json
import re
import sqlite3
from uuid import uuid4

from .repository import utc_now


CHILD_SAFETY_CLASSIFIER_VERSION = "child_safety_v2"


SAFETY_GUIDANCE = {
    "emotional_distress": {
        "summary": "孩子表达了明显低落或不愿上学。系统只记录风险分类，不在家长首页展示孩子原话。",
        "script": [
            "我感觉你今天可能不太好受，我先陪你坐一会儿。",
            "你现在不用把事情全部说清楚，也不用马上给出原因。",
            "你希望我先安静听，还是陪你找一个你信任的大人？",
            "今晚我们先照顾好你，学习安排可以稍后再调整。",
        ],
        "action": {
            "what": "进行一次低压力安全确认",
            "duration_minutes": 10,
            "steps": ["先确认孩子此刻是否安全", "给孩子安静、倾听或找可信任大人三个选择", "今晚降低学习要求并约定下一次关心时间"],
            "stop_condition": "孩子拒绝时不追问；若出现自伤、他伤或正在发生的危险，立即升级为线下紧急处置。",
        },
        "avoid": [
            {"do_not_say": "你有什么好难过的？", "say_instead": "我先相信你现在确实不好受。"},
            {"do_not_say": "不想上学就是逃避。", "say_instead": "不想去学校可能有具体原因，你准备好时我会听。"},
            {"do_not_say": "马上把原因说清楚。", "say_instead": "你不用一次说完，我们先确认现在需要什么。"},
        ],
    },
    "fear_or_bullying": {
        "summary": "孩子表达了被欺负、威胁或对回家/上学的明显恐惧，需要家长及时线下确认。系统未在本提醒中保存原话。",
        "script": [
            "谢谢你愿意让我知道这件事，你不用一个人处理。",
            "我先确认一件事：你现在安全吗？相关的人现在在附近吗？",
            "你可以只说愿意说的部分，我不会责怪你，也不会马上去质问任何人。",
            "接下来我会和你一起找合适的大人，先保护你，再决定怎么处理。",
        ],
        "action": {
            "what": "当天完成安全确认并安排可信任大人介入",
            "duration_minutes": 15,
            "steps": ["先确认当前地点和即时安全", "记录孩子愿意提供的最少事实，不诱导细节", "与孩子共同选择家长、学校负责人或其他可信任大人", "明确下一次反馈时间，不让孩子独自等待"],
            "stop_condition": "若危险正在发生、孩子无法回到安全地点或出现伤害风险，停止普通沟通并联系当地紧急服务。",
        },
        "avoid": [
            {"do_not_say": "你是不是也招惹别人了？", "say_instead": "我先确认你的安全，不把责任推给你。"},
            {"do_not_say": "我现在就去找对方算账。", "say_instead": "我会先和你一起确定安全、稳妥的处理顺序。"},
            {"do_not_say": "把所有细节马上说出来。", "say_instead": "你只说现在愿意说的部分，剩下的可以由专业大人慢慢了解。"},
        ],
    },
    "immediate_danger": {
        "summary": "孩子表达了自伤、他伤或正在发生危险的线索，需要家长立即接手并确认现实安全。系统未在本提醒中保存原话。",
        "script": [
            "我现在先陪着你，安全是第一件事。",
            "请告诉我你现在在哪里，身边有没有可能伤害你的东西或人。",
            "我们现在一起去找身边可信任的大人，不让你一个人待着。",
            "其他事情都可以暂停，先获得现场帮助。",
        ],
        "action": {
            "what": "立即确认孩子安全并由现实中的大人接手",
            "duration_minutes": 0,
            "steps": ["马上到孩子身边或联系现场可信任大人", "移开可能造成伤害的物品并避免孩子独处", "如有立即危险，联系当地急救或警方", "安全稳定后再联系专业支持"],
            "stop_condition": "不能以软件提示替代现场确认、专业评估或紧急服务。",
        },
        "avoid": [
            {"do_not_say": "你别胡思乱想就行了。", "say_instead": "我会认真对待，先和你一起保证现在安全。"},
            {"do_not_say": "你敢这样做试试。", "say_instead": "你不需要一个人扛着，我现在就陪你找现实中的帮助。"},
            {"do_not_say": "答应我以后绝对不会这样想。", "say_instead": "我们先处理此刻的危险，再一步步安排后续支持。"},
        ],
    },
}


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _remove_non_self_context(text: str) -> str:
    risk = r"(?:不想活|想死|自杀|伤害自己|打死|有危险|很害怕|被欺负)"
    contextual_patterns = (
        rf"(?:老师|英语课).{{0,14}}(?:解释|讲的是|讲了|这个词).{{0,14}}{risk}",
        rf"(?:课文|新闻|电影|电视剧|书里|故事里).{{0,14}}(?:写|说|讲|台词|有人|的人|是).{{0,14}}{risk}",
        rf"(?:作文题目?|排练|舞台台词|台词).{{0,16}}{risk}",
        r"游戏里.{0,16}(?:打死|杀死|消灭).{0,8}(?:怪物|敌人|Boss|boss)",
    )
    clauses = re.split(r"([，。！？；,.!?;])", text)
    kept: list[str] = []
    for index in range(0, len(clauses), 2):
        clause = clauses[index]
        separator = clauses[index + 1] if index + 1 < len(clauses) else ""
        if any(re.search(pattern, clause) for pattern in contextual_patterns):
            continue
        kept.append(clause + separator)
    return "".join(kept)


def _remove_explicit_denials(text: str) -> str:
    cleaned = text
    denial_patterns = (
        r"(?:从来|根本|真的)?(?:没有|没)(?:想过|想要|想|打算)?(?:伤害自己|自杀|想死|结束生命)",
        r"不想死",
        r"(?:没有|没)被.{0,8}(?:欺负|威胁|打|伤害)",
        r"不(?:再)?害怕(?:回家|去学校|上学)",
        r"(?:老师|家长|妈妈|爸爸)?问.{0,10}(?:有没有人|是否有人)(?:打|欺负|威胁)我.{0,8}(?:我回答|我说)?没有",
    )
    for pattern in denial_patterns:
        cleaned = re.sub(pattern, "", cleaned)
    return cleaned


def _normalized_safety_text(text: str) -> str:
    contextual = _remove_non_self_context(text)
    denied = _remove_explicit_denials(contextual)
    return re.sub(r"\s+", "", denied)


def _safety_action(action_type: str, risk_level: str, category: str) -> dict:
    return {
        "type": action_type,
        "risk_level": risk_level,
        "category": category,
        "classifier_version": CHILD_SAFETY_CLASSIFIER_VERSION,
    }


def classify_child_safety(text: str) -> tuple[str, str, dict] | None:
    compact = _normalized_safety_text(text)
    immediate_danger = _contains_any(compact, (
        "不想活", "想死", "自杀", "伤害自己", "不想存在", "想伤害别人", "想杀人",
        "想杀了", "打死他", "结束这一切", "活着没有意思", "活着没意思", "割伤自己",
        "划伤自己", "吃了很多药", "吞了很多药", "从楼上跳", "跳下去", "手里拿着刀",
        "手里有一把刀", "手里有刀", "拿着刀追我", "拿刀追我", "正在打我", "正在伤害我",
        "现在有人打我", "现在有人伤害我", "我现在有危险", "被锁在房间", "现在出不去",
        "流血很多", "还在流血",
    )) or bool(re.search(r"(?:现在|马上|这就).{0,8}(?:要去)?(?:打死|杀了|伤害)(?:他|她|别人)", compact)) \
        or bool(re.search(r"(?:拿着|拿)刀.{0,10}追我", compact)) \
        or bool(re.search(r"手里.{0,5}(?:有|拿着).{0,3}刀", compact))
    if immediate_danger:
        return (
            "urgent_safety_support",
            "我很在意你现在说的这件事。它不会发送给云模型。现在安全最重要：请不要独处，马上去找身边可信任的大人，把刚才的话原样告诉他；如果危险正在发生，请让大人联系当地急救或警方。我已在这台电脑的家长中心保存一条不含你原话的紧急提醒，但它不能代替你现在去找大人。",
            _safety_action("find_parent_urgent", "R3", "immediate_danger"),
        )
    fear_or_bullying = _contains_any(compact, (
        "有人欺负我", "欺负我", "被欺负", "有人威胁我", "威胁我", "被威胁", "害怕回家",
        "不敢回家", "害怕去学校", "不敢去学校", "害怕上学", "不敢上学", "有人摸我",
        "让我保密", "叫我保密", "不许告诉", "别说出去", "有人打我", "每天都推我",
        "总是打我", "经常打我", "会打我", "掐我", "踢我", "抢我的东西", "堵在厕所",
        "堵我", "要钱", "逼我给钱", "围着不让我走", "锁门不让我", "碰我不想让",
        "不想让别人碰的地方", "奇怪的视频", "这是秘密", "没穿衣服的照片", "跟着我走回家",
        "跟踪我", "等着收拾我", "让我好看",
    )) or bool(re.search(
        r"(?:爸爸|妈妈|哥哥|姐姐|老师|教练|家里人|亲戚|叔叔|阿姨).{0,12}(?:打|掐|踢|推|摸|伤害)我",
        compact,
    )) or bool(re.search(r"网上认识的人.{0,12}让我发.{0,12}照片", compact))
    if fear_or_bullying:
        return (
            "high_risk_child_safety",
            "这件事听起来让你不舒服，也可能需要大人帮助。它不会发送给云模型。你不用一个人处理，请现在去找身边可信任的大人；你可以只说愿意说的部分。我已在这台电脑的家长中心保存一条不含你原话的关注提醒，但它不能代替你现在去找大人。",
            _safety_action("find_parent", "R2", "fear_or_bullying"),
        )
    if _contains_any(compact, (
        "很难过", "好难过", "很伤心", "想哭", "不想去学校", "不想上学", "没有人喜欢我",
        "我很害怕", "很害怕", "心情特别低落", "心情很低落", "每天都不开心", "什么都不想做",
        "没有人愿意和我玩", "总是一个人", "什么都做不好", "到学校门口我就特别紧张",
        "一点用都没有", "自己没用", "只想消失", "不想见任何人", "睡不着", "一直担心",
        "很孤单", "都不喜欢我", "大家不喜欢我",
    )):
        return (
            "emotional_distress_support",
            "听起来你今天真的不太好受。你不用马上说清楚，也不会把这段话发送给云模型。我们可以先做一个很小的选择：喝水、安静五分钟，或者去告诉一个你信任的大人今天需要被陪一会儿。家长中心会看到一条不含你原话的低压力关注提醒。",
            _safety_action("none", "R1", "emotional_distress"),
        )
    study_context = _contains_any(compact, (
        "作业", "卷子", "这道题", "这题", "数学", "背单词", "考试", "补习班", "上了一天课",
    ))
    study_pressure = _contains_any(compact, (
        "不想写作业", "不想写学校作业", "不想学习", "不想学", "今天好烦", "烦死了", "压力好大", "今天很累",
        "不想做了", "不想继续学", "不想背", "一点精神都没有", "我就很烦", "越做越烦",
        "好生气", "不想碰它", "累趴了", "烦坏了", "很崩溃",
    ))
    if study_pressure and (study_context or _contains_any(compact, ("不想学习", "不想学", "今天好烦", "今天很累"))):
        return (
            "ordinary_study_pressure",
            "听起来你现在很烦，我先不讲道理。你可以选一个：喝口水、安静五分钟，或者把任务缩成只做第一小步。这个普通学习压力不会生成家长安全提醒。",
            _safety_action("none", "R0", "ordinary_pressure"),
        )
    return None


def persist_safety_event(
    connection: sqlite3.Connection,
    child_id: str,
    session_id: str,
    agent_turn_id: str,
    action: dict,
    created_at: str,
) -> None:
    risk_level = action.get("risk_level")
    category = action.get("category")
    if risk_level not in {"R1", "R2", "R3"} or category not in SAFETY_GUIDANCE:
        return
    connection.execute(
        """
        INSERT OR IGNORE INTO child_safety_event(
          safety_event_id, child_id, session_id, agent_turn_id, risk_level,
          category, status, guidance_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            str(uuid4()), child_id, session_id, agent_turn_id, risk_level,
            category,
            json.dumps(
                {**SAFETY_GUIDANCE[category], "classifier_version": action.get("classifier_version", CHILD_SAFETY_CLASSIFIER_VERSION)},
                ensure_ascii=False,
            ),
            created_at,
        ),
    )


class ChildSafetyService:
    def __init__(self, database):
        self.database = database

    def list_events(self, child_id: str | None = None, limit: int = 50) -> list[dict]:
        where = "WHERE cse.child_id = ?" if child_id else ""
        parameters = (child_id, max(1, min(limit, 100))) if child_id else (max(1, min(limit, 100)),)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT cse.safety_event_id, cse.child_id, cp.nickname AS child_nickname,
                       cse.risk_level, cse.category, cse.status, cse.guidance_json,
                       cse.created_at, cse.acknowledged_at
                FROM child_safety_event cse
                JOIN child_profile cp ON cp.child_id = cse.child_id
                {where}
                ORDER BY CASE cse.status WHEN 'pending' THEN 0 ELSE 1 END,
                         CASE cse.risk_level WHEN 'R3' THEN 0 WHEN 'R2' THEN 1 ELSE 2 END,
                         cse.created_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["guidance"] = json.loads(item.pop("guidance_json"))
            results.append(item)
        return results

    def get_event(self, safety_event_id: str, child_id: str | None = None) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT cse.safety_event_id, cse.child_id, cp.nickname AS child_nickname,
                          cse.risk_level, cse.category, cse.status, cse.guidance_json,
                          cse.created_at, cse.acknowledged_at
                   FROM child_safety_event cse JOIN child_profile cp ON cp.child_id=cse.child_id
                   WHERE cse.safety_event_id=?""", (safety_event_id,),
            ).fetchone()
        if row is None or child_id is not None and row['child_id'] != child_id:
            raise UnknownSafetyEventError(safety_event_id)
        result = dict(row)
        result['guidance'] = json.loads(result.pop('guidance_json'))
        return result

    def acknowledge(self, safety_event_id: str, client_action_id: str) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reused = connection.execute(
                "SELECT safety_event_id FROM child_safety_event WHERE acknowledgement_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if reused and reused["safety_event_id"] != safety_event_id:
                raise SafetyActionReuseError(client_action_id)
            event = connection.execute(
                "SELECT child_id, status FROM child_safety_event WHERE safety_event_id = ?",
                (safety_event_id,),
            ).fetchone()
            if not event:
                raise UnknownSafetyEventError(safety_event_id)
            if event["status"] == "pending":
                connection.execute(
                    """
                    UPDATE child_safety_event SET status = 'acknowledged', acknowledged_at = ?,
                      acknowledgement_action_id = ? WHERE safety_event_id = ?
                    """,
                    (now, client_action_id, safety_event_id),
                )
                connection.execute(
                    """
                    INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                    VALUES (?, 'parent', 'child_safety_event_acknowledged', ?, ?, ?)
                    """,
                    (str(uuid4()), event["child_id"], json.dumps({"safety_event_id": safety_event_id}), now),
                )
            connection.commit()
        return self.get_event(safety_event_id, event['child_id'])


class UnknownSafetyEventError(Exception):
    pass


class SafetyActionReuseError(Exception):
    pass
