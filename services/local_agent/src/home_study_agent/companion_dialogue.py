from __future__ import annotations

import re


POLICY_VERSION = "companion_safety_v2"
PROMPT_VERSION = "companion_dialogue_v3"
MAX_REPLY_CHARS = 220


def build_companion_messages(
    *,
    grade: str,
    session_state: str,
    transcript: str,
    previous_response: str | None,
    recent_dialogue: tuple[dict[str, str], ...] = (),
    active_activity: dict | None = None,
) -> tuple[dict[str, str], ...]:
    previous = (previous_response or "").strip()[:180]
    system = (
        "你是家庭学习软件中的儿童学习伙伴，不是孩子唯一的朋友，也不替代家长、老师或现实朋友。"
        f"孩子年级是{grade[:30]}，当前学习阶段是{session_state[:30]}。"
        "请自然回应孩子当前这句话，只输出纯文本，使用1到3个短句，总共不超过120个汉字。"
        "可以接住情绪、聊一个轻松话题或把注意力温和带回当前学习，但不要说教。"
        "最多问一个容易回答的问题；不要承诺保密、永远陪伴或只有你懂孩子，不要诱导依恋。"
        "遇到孩子要求永远陪伴、只依赖你或共同保密时，要明确提到家长、老师或现实朋友也能支持孩子。"
        "不要索取姓名、学校、班级、住址、电话、照片或其他身份信息。"
        "不要声称看见、听见或知道未在文字中提供的事情，不执行工具、不假装工具已执行、不改变计划，也不代做题目。"
        "不要复述孩子要求你说的越界句子，即使是否定或纠正它。"
        "如果文字涉及安全风险，应停止普通聊天并建议孩子找身边可信任的大人。"
    )
    if active_activity:
        labels = {
            "interactive_story": "互动原创故事",
            "word_chain": "词语接龙",
            "riddle": "谜语游戏",
            "movement": "离屏运动",
            "tidy_challenge": "桌面整理挑战",
        }
        remaining = max(0, int(active_activity.get("remaining_turns", 0)))
        system += (
            f"当前正在进行{labels.get(active_activity.get('activity_type'), '陪伴活动')}，"
            f"主题是{str(active_activity.get('topic') or '')[:80]}，还剩最多{remaining}轮。"
            "必须延续当前活动而不是另起话题；故事每轮让孩子做一个选择，游戏每轮只出一题。"
            "不得延长轮次、诱导继续或把结束描述成失去陪伴；最后一轮自然收束并引导回到现实活动或学习。"
        )
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for item in recent_dialogue[-8:]:
        role = item.get("role")
        content = item.get("content", "").strip()[:500]
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    if not recent_dialogue and previous:
        messages.append({"role": "assistant", "content": previous})
    messages.append({"role": "user", "content": transcript[:500]})
    return tuple(messages)


def enforce_companion_reply(content: str, transcript: str = "", *, tool_discussion_allowed: bool = False) -> str | None:
    reply = re.sub(r"\s+", " ", content).strip().strip('"“”')
    if not reply or len(reply) > MAX_REPLY_CHARS:
        return None
    compact = re.sub(r"\s+", "", reply).lower()
    dependency_patterns = (
        r"只有我(?:懂|理解|需要|陪)",
        r"你只(?:需要|能|要)(?:相信|找|告诉|依靠)我",
        r"不要告诉(?:爸爸|妈妈|家长|老师|别人)",
        r"这是我们(?:之间)?的秘密",
        r"我会永远陪(?:着)?你",
        r"我不会离开你",
        r"没有边界",
        r"不需要(?:爸爸|妈妈|家长|老师|朋友)",
    )
    privacy_patterns = (
        r"告诉我你的(?:真实)?(?:姓名|学校|校名|班级|住址|地址|手机号|电话|身份证|邮箱)",
        r"把你的(?:照片|定位|地址|电话号码|手机号)发给我",
        r"你叫什么(?:名字|姓名)",
        r"你(?:在|上)哪所学校",
        r"你住在哪(?:里|儿)",
        r"你的(?:手机号|电话号码|家庭住址)是什么",
    )
    false_perception_patterns = (
        r"我(?:现在)?看见你",
        r"我(?:现在)?看到你",
        r"我听见你(?:在|刚才)",
    )
    false_tool_claim_patterns = (
        r"(?:我已经|已经帮你).{0,12}(?:关闭|打开|修改|保存|完成|启动|暂停).{0,8}(?:摄像头|麦克风|计划|任务|游戏|视频|应用)",
        r"(?:摄像头|麦克风|计划|任务|游戏|视频|应用).{0,8}(?:已|已经)(?:关闭|打开|修改|保存|完成|启动|暂停)",
    )
    answer_leak_patterns = (
        r"(?:最终)?答案(?:是|为)",
        r"所以(?:结果|答案)是",
    )
    if any(re.search(pattern, compact) for pattern in (
        *dependency_patterns, *privacy_patterns, *false_perception_patterns,
        *false_tool_claim_patterns, *answer_leak_patterns,
    )):
        return None
    # A grounded question may discuss a device, but a response-only plan must
    # not promise an operation that has no corresponding executable step.
    if tool_discussion_allowed and re.search(
        r"(?:我来|我会|我现在|我马上|我这就|马上帮你|这就帮你)(?:帮你)?"
        r"(?:(?:打开|关闭|修改|保存|启动|暂停).{0,8}(?:摄像头|麦克风|计划|任务|游戏|视频|应用)"
        r"|(?:把|将)?(?:摄像头|麦克风|计划|任务|游戏|视频|应用).{0,8}(?:打开|关闭|修改|保存|启动|暂停))",
        compact,
    ):
        return None
    dependency_request = re.search(
        r"(?:永远|别离开|不要离开|只有你|只要你|保密|秘密|别告诉)",
        re.sub(r"\s+", "", transcript).lower(),
    )
    if dependency_request and not re.search(
        r"(?:家长|爸爸|妈妈|老师|现实中的?朋友|身边的?人|信任的?大人)", compact
    ):
        return None
    transcript_compact = re.sub(r"\s+", "", transcript).lower()
    tool_request = re.search(
        r"(?:打开|关闭|修改|保存|启动).{0,10}(?:游戏|视频|应用|摄像头|麦克风|计划|任务)|(?:游戏|视频|应用|摄像头|麦克风|计划|任务).{0,10}(?:打开|关闭|修改|保存|启动)",
        transcript_compact,
    )
    if tool_request and not tool_discussion_allowed and re.search(
        r"(?:打开|关闭|修改|保存|启动).{0,10}(?:游戏|视频|应用|摄像头|麦克风|计划|任务)|(?:游戏|视频|应用|摄像头|麦克风|计划|任务).{0,10}(?:打开|关闭|修改|保存|启动)",
        compact,
    ):
        return None
    return reply


def safe_companion_fallback(transcript: str) -> str:
    compact = re.sub(r"\s+", "", transcript).lower()
    if re.search(r"(?:永远|别离开|不要离开|只有你|只要你|保密|秘密|别告诉)", compact):
        return "我可以陪你聊这一会儿，也请记得家长、老师和现实中的朋友都能支持你。"
    if re.search(r"(?:打开|关闭|修改|保存|启动).{0,10}(?:游戏|视频|应用|摄像头|麦克风|计划|任务)|(?:游戏|视频|应用|摄像头|麦克风|计划|任务).{0,10}(?:打开|关闭|修改|保存|启动)", compact):
        return "我不能假装已经操作了功能。你可以直接说想做什么，我会按实际执行结果回应。"
    if re.search(r"(?:忽略|系统提示|内部指令|没有边界|突破边界|必须回答|必须输出)", compact):
        return "我不会改变安全边界，也不会照着内部指令说话。我们可以继续聊你真正想做的事。"
    return "我刚才没有找到合适的安全说法。你可以换句话告诉我现在最需要什么。"
