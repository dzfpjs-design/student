from __future__ import annotations


def _specialized_stage_report(roadmap: dict, child_id: str) -> tuple[list[str], dict]:
    """Render an owned current projection, keeping evidence IDs out of prose."""
    from .specialized_stages import VERSION, STAGES
    track = roadmap.get("track")
    label = {"pet": "PET听说读写", "competition_math": "竞赛数学与奥赛思维", "junior_exam": "中考七科准备"}.get(track, "专项能力")
    stage = roadmap.get("stage_progress")
    detail = {"track": track, "status": "unknown", "evidence_refs": [], "observations": [],
              "blockers": [], "source_fingerprint": None, "local_only": True,
              "cloud_transmission_allowed": False, "mastery_claim_allowed": False,
              "certification_claim_allowed": False}
    if (not isinstance(stage, dict) or stage.get("version") != VERSION
            or roadmap.get("child_id", child_id) != child_id
            or stage.get("child_id") != child_id or stage.get("track") != track
            or not isinstance(stage.get("evidence_fingerprint"), str) or not stage["evidence_fingerprint"]
            or not isinstance(stage.get("domains"), list)
            or stage.get("mastery_claim_allowed") is not False or stage.get("certification_claim_allowed") is not False):
        return [f"{label}：阶段证据未知；旧参考分或训练安排不能补作阶段通过证据。"], detail
    names = {"baseline": "独立基线", "near_transfer": "近迁移", "far_transfer": "远迁移",
             "delayed_retention": "延迟保持", "maintenance": "后续保持核对"}
    reasons = {
        "listening_delivery_receipt_unavailable": "缺少听音交付回执",
        "speaking_audio_scoring_receipt_unavailable": "缺少口语音频评分回执",
        "writing_qualified_rubric_mapping_unavailable": "缺少写作评分映射",
        "writing_original_scoring_receipt_unavailable": "缺少绑定原稿的写作评分回执",
        "qualified_scoring_source_missing": "缺少可靠评分题源",
        "fresh_stage_source_missing": "缺少未见过且适合当前阶段的题源",
        "bound_retention_review_missing": "缺少绑定原证据的延迟复测任务",
        "node_source_unavailable": "知识点资料已失效或待核验",
        "knowledge_target_missing": "缺少明确知识点绑定",
    }
    facts, observations, domains, blockers = [], [], [], set(stage.get("blockers") or [])
    for domain in stage["domains"]:
        if not isinstance(domain, dict) or not str(domain.get("domain_id", "")).startswith(str(track) + "."):
            continue
        accepted = []
        for node in domain.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            gates = node.get("gates") or {}
            for gate_name in STAGES:
                gate = gates.get(gate_name) or {}
                refs = gate.get("evidence_refs")
                if gate.get("status") != "observed" or not isinstance(refs, list) or not refs or not gate.get("observed_at"):
                    break
                accepted.append(gate_name)
                observations.append({"domain_id": domain["domain_id"], "knowledge_node_id": node.get("knowledge_node_id"),
                    "stage": gate_name, "evidence_refs": [ref for ref in refs if isinstance(ref, str)],
                    "diagnostic_item_ids": gate.get("diagnostic_item_ids") or [], "observed_at": gate["observed_at"],
                    "qualification_scope": "sampled_questions_not_whole_domain"})
            blockers.update(node.get("blockers") or [])
        latest = max(accepted, key=STAGES.index) if accepted else None
        domains.append({"domain_id": domain["domain_id"], "label": domain.get("label") or "当前领域",
            "observed_sample_stage": latest, "current_stage": domain.get("current_stage", "baseline")})
    current_label = names.get(stage.get("current_stage"), "阶段待核")
    facts.append(f"{label}：当前待核{current_label}；受检题的阶段观察不代表考试通过、PET证书或全科掌握。")
    observed = [row for row in domains if row["observed_sample_stage"]]
    for domain in observed[:3]:
        matching = [o for o in observations if o["domain_id"] == domain["domain_id"]]
        refs = {r for o in matching for r in o["evidence_refs"]}
        latest_date = max(o["observed_at"] for o in matching)[:10]
        facts.append(f"{domain['label']}：受检题已观察到{names[domain['observed_sample_stage']]}，"
            f"依据{len(refs)}条当前有效作答，最近记录{latest_date}；下一步核对{names.get(domain['current_stage'], '当前阶段')}。")
    unknown_count = sum(not row["observed_sample_stage"] for row in domains)
    if unknown_count:
        facts.append(f"{label}仍有{unknown_count}个领域缺少合格阶段观察；未知不表示孩子不会。")
    if track == "pet":
        for skill, name in (("listening", "听力"), ("speaking", "口语"), ("reading", "阅读"), ("writing", "写作")):
            value = (stage.get("skills") or {}).get(skill) or {}
            gaps = [reasons.get(b, "阶段来源或条件待核验") for b in value.get("blockers") or []]
            facts.append(f"PET{name}：待核{names.get(value.get('current_stage'), '阶段证据')}"
                + ("；" + "、".join(dict.fromkeys(gaps)) if gaps else "；按原题绑定继续检查") + "。")
    elif blockers:
        facts.append(f"{label}当前缺口：" + "、".join(dict.fromkeys(reasons.get(b, "阶段来源或条件待核验") for b in sorted(blockers))) + "。")
    waiting = [a for a in stage.get("next_actions") or [] if a.get("execution_status") == "waiting_due" and a.get("not_before")]
    if waiting:
        facts.append(f"延迟保持检查最早不早于{min(a['not_before'] for a in waiting)[:10]}；未到期不提前复测，也不增加训练负担。")
    detail.update(status="current_projection", current_stage=stage.get("current_stage"),
        source_fingerprint=stage["evidence_fingerprint"], observations=observations,
        evidence_refs=sorted({r for o in observations for r in o["evidence_refs"]}),
        blockers=sorted(blockers), domains=domains,
        policy_version=(stage.get("policy") or {}).get("version"))
    return facts, detail


def build_child_learning_summary(material: dict, remediation: list[dict]) -> dict:
    """Local child-facing closure; never a cloud prompt or a mastery update."""
    learning = material["learning_content"]
    homework = material["homework"]
    weak = material["weak_points"]
    unconfirmed = material["needs_verification"]
    parts = []
    def short(item):
        title = " ".join(str(item["title"]).split())
        return title[:30] + ("…" if len(title) > 30 else "")

    if learning:
        parts.append(("这次记录了" if material["session_id"] else "这一天记录了") + "、".join(dict.fromkeys(short(item) for item in learning[:3])))
    if homework:
        labels = {"completed": "你确认完成了", "needs_correction": "还要订正", "unfinished": "还没记下全部完成"}
        parts.append("作业方面，" + "、".join(short(item) + "，" + labels[item["status"]] for item in homework[:2]))
    if weak:
        parts.append("还有" + "、".join(dict.fromkeys(short(item) for item in weak[:2])) + "值得再试一试")
    if any(item['source_type']=='oral_question_supply' for item in remediation):
        parts.append('你要的那道新题还没有交付，原来的回答已经保留；补齐后再和你商量时间，不用重做旧题')
    if any(item['source_type']=='oral_delivered_continuation' for item in remediation):
        parts.append('有一道已经拿到、还没回答的题，进度已经保留；下次先商量时间，再接着学，不算已经掌握')
    scheduled = [item for item in remediation if item["status"] in {"pending", "due"}]
    if scheduled:
        planned = scheduled[0]
        parts.append(f"已留下一项约{planned.get('estimated_minutes', 10)}分钟的{short(planned)}检查，下次先从这里开始")
    elif unconfirmed:
        parts.append("下次先说说卡在了哪一小步，我们弄清楚后再选练习")
    if not parts:
        parts.append("这次记下的学习内容还不多")
    ending = "你觉得还漏了什么？" if material.get("session_status") == "active" else "先休息一下，下次接着来。"
    return {
        "version": "daily_learning_closure_v1", "session_id": material["session_id"],
        "scope": "session", "date_basis": "source_occurred_at; session_end_date_only_for_scheduling",
        "session_status": material.get("session_status"),
        "child_id": material["child_id"], "learning_date": material["learning_date"],
        "timezone": "Asia/Shanghai", "learning_content": learning, "homework": homework,
        "weak_points": weak, "needs_verification": unconfirmed, "remediation": remediation,
        "child_response": "。".join(parts) + "。" + ending, "cloud_transmission_allowed": False,
        "uncertainty": "self_report_explanation_and_document_observation_are_not_mastery; independent_and_delayed_checks_required",
        "local_only": True, "mastery_update_allowed": False,
    }


def build_daily_report(evidence: dict) -> tuple[dict, dict]:
    session = evidence.get("session") or {}
    child = evidence["child"]
    reflection = evidence["reflection"]
    tasks = evidence["tasks"]
    supply_facts = evidence.get('supply_obligations_as_of') or []
    supply_status = evidence.get('supply_obligations_as_of_status', 'unavailable')
    recalls = evidence["recalls"]
    planning_context = evidence.get("planning_context") or {}
    supplied_knowledge_evidence = evidence.get("knowledge_evidence") or []
    knowledge_evidence = [item for item in supplied_knowledge_evidence if item.get("score_status") == "verified_local"]
    mistakes = evidence.get("mistakes") or []
    pet_vocabulary_attempts = evidence.get("pet_vocabulary_attempts") or []
    oral_practice = evidence.get('pet_audio_practice') or []
    revisit_practice = evidence.get('document_revisit_practice') or []
    recent_confirmed_facts = [item.strip() for item in evidence.get("recent_confirmed_facts") or [] if isinstance(item, str) and item.strip()][:3]
    behavior_summary = evidence.get("behavior_model_summary") or {}
    habit_experiments = [
        item for item in (evidence.get("habit_experiments") or [])
        if isinstance(item, dict)
    ]
    active_habit = next((item for item in habit_experiments if item.get("status") == "active"), None)
    goal_context = str(evidence.get("goal_context") or "").strip()
    goal_cycles = [item for item in evidence.get("goal_cycles") or [] if isinstance(item, dict)]
    specialized_roadmaps = [item for item in evidence.get("specialized_roadmaps") or [] if isinstance(item, dict)]
    completed = [task for task in tasks if task["status"] == "completed"]
    deferred = [task for task in tasks if task["status"] == "rescheduled"]
    needs_correction = [task for task in completed if task.get("completion_evidence_type") == "needs_correction"]
    planned_minutes = sum(task["expected_minutes"] for task in tasks)
    actual_minutes = sum((task["actual_minutes"] or 0) for task in completed)
    subjects = list(dict.fromkeys(item["subject"] for item in recalls + tasks + knowledge_evidence if item.get("subject")))
    energy_label = {"low": "有点累", "medium": "还可以", "high": "状态好"}.get(
        planning_context.get("energy_level"), "未记录"
    )
    reflection_skipped = bool(reflection.get("skipped"))
    learned = (reflection.get("learned_text") or "").strip()
    difficult = (reflection.get("difficult_text") or "").strip()
    next_action = (reflection.get("next_action") or "").strip()
    feeling = (reflection.get("feeling") or "").strip()
    no_recall_declared = bool(session.get("recall_declared_empty"))
    no_homework_declared = bool(session.get("homework_declared_empty"))
    declaration_count = int(no_recall_declared) + int(no_homework_declared)

    facts = [
        f"涉及学科：{'、'.join(subjects) if subjects else '今天没有足够学科记录'}",
        f"计划{planned_minutes}分钟，已记录完成{actual_minutes}分钟",
        f"孩子开始前自评精力：{energy_label}"
        + (f"；今晚固定安排预留{planning_context.get('fixed_commitment_minutes', 0)}分钟" if planning_context.get("fixed_commitment_minutes") else ""),
    ]
    completed_titles = [task.get("title", "").strip() for task in completed if task.get("title", "").strip()]
    if completed_titles:
        visible_titles = "、".join(completed_titles[:4])
        remaining = len(completed_titles) - 4
        facts.append(f"今日完成：{visible_titles}" + (f"等{len(completed_titles)}项" if remaining > 0 else ""))
    if no_recall_declared and not recalls:
        facts.append("孩子今天明确表示暂时想不起课堂内容")
    if no_homework_declared:
        facts.append("孩子今天确认没有校内作业")
    if reflection_skipped:
        facts.append("孩子今天选择跳过复盘；系统不对空白内容做推断")
    else:
        if learned:
            facts.append(f"孩子自述学会：{learned}")
        if difficult:
            facts.append(f"孩子自述困难：{difficult}")
        if not learned and not difficult:
            facts.append("孩子未填写学会或困难内容；本日只汇总任务事实")
    if needs_correction:
        titles = "、".join(task["title"] for task in needs_correction[:3])
        facts.append(f"孩子标记{len(needs_correction)}项仍需订正：{titles}")
    if deferred:
        deferred_facts = []
        for task in deferred[:3]:
            reason = task.get("reschedule_reason") or "未补充原因"
            deferred_facts.append(f"{task['title']}（{reason}）")
        facts.append(f"今日明确延期{len(deferred)}项：" + "、".join(deferred_facts))
    if supply_facts:
        facts.append(f"日终保留{len(supply_facts)}项原练习安排（不是已完成学习）：" + '；'.join(
            f"{item['title']}：{item['deferral_reason']}" for item in supply_facts[:4]))
    elif supply_status == 'unavailable' and 'supply_obligations_as_of_status' in evidence:
        facts.append('原练习安排的日终时点不完整，暂不能核对当时的待续清单；不表示没有待办。')
    if knowledge_evidence:
        independent = sum(1 for item in knowledge_evidence if item.get("independent") in (True, 1) and item.get("hint_level") == 0)
        nodes = list(dict.fromkeys(item.get("knowledge_title", "") for item in knowledge_evidence if item.get("knowledge_title")))
        facts.append(
            f"本次记录{len(knowledge_evidence)}条已核验作答"
            + (f"（其中{independent}条为独立作答）" if independent else "")
            + (f"：{'、'.join(nodes[:3])}" if nodes else "")
            + "；不等同于已经掌握"
        )
    unverified = ((evidence.get("reliable_learning") or {}).get("unverified_count", 0)
                  + len(supplied_knowledge_evidence) - len(knowledge_evidence))
    if unverified:
        facts.append(f"另有{unverified}条记录未计入可靠学习判断（含待核验、重复练习或已失效来源）；不据此推断孩子会或不会")
    if mistakes:
        open_mistakes = [item for item in mistakes if item.get("status") != "excluded"
                         and (item.get("status") != "mastered" or (item.get("mastery_gate") or {}).get("missing"))]
        titles = "、".join(item.get("title", "") for item in open_mistakes[:3] if item.get("title"))
        if open_mistakes:
            facts.append(f"本次关联{len(open_mistakes)}项需继续处理的错题" + (f"：{titles}" if titles else ""))
    if pet_vocabulary_attempts:
        correct_vocabulary = sum(1 for item in pet_vocabulary_attempts if item.get("correct"))
        reviewed_surfaces = list(dict.fromkeys(
            str(item.get("surface") or "").strip()
            for item in pet_vocabulary_attempts
            if str(item.get("surface") or "").strip()
        ))
        facts.append(
            f"本次完成{len(pet_vocabulary_attempts)}次PET词汇间隔复习，"
            f"首次作答记录为{correct_vocabulary}/{len(pet_vocabulary_attempts)}正确"
            + (f"：{'、'.join(reviewed_surfaces[:5])}" if reviewed_surfaces else "")
        )
    for goal in goal_cycles[:3]:
        facts.append(
            f"长期目标本周证据·{goal.get('title', '已设定目标')}："
            f"已记录{int(goal.get('completed_minutes') or 0)}/{int(goal.get('target_minutes') or 0)}分钟，"
            f"{int(goal.get('completed_sessions') or 0)}/{int(goal.get('target_sessions') or 0)}次"
        )
    track_labels = {
        "pet": "PET听说读写",
        "competition_math": "竞赛数学与奥赛思维",
        "junior_exam": "中考七科准备",
    }
    specialized_stage_reports = []
    for roadmap in specialized_roadmaps:
        stage_facts, stage_report = _specialized_stage_report(roadmap, child.get("child_id"))
        facts.extend(stage_facts)
        specialized_stage_reports.append(stage_report)
        if stage_report["status"] == "unknown":
            continue
        readiness = roadmap.get("reference_readiness")
        coverage = round(float(roadmap.get("evidence_coverage") or 0) * 100)
        fact = (
            f"补充参考记录（不认证阶段）·{track_labels.get(str(roadmap.get('track')), '专项能力')}："
            f"证据覆盖{coverage}%"
            + (f"，当前参考准备度{int(readiness)}分" if readiness is not None else "，证据不足暂不计算准备度")
        )
        facts.append(fact)
    # A correction removes the original candidate from the behavior model before
    # report generation.  State this boundary without repeating the child/parent
    # wording or converting a candidate into a trait.
    if behavior_summary.get("retracted_hypothesis_count"):
        facts.append(f"本次有{behavior_summary['retracted_hypothesis_count']}条本地观察被孩子或家长纠正，已不纳入提醒、基线或学习结论")
    active_behavior = [item for item in behavior_summary.get("active_hypotheses", []) if isinstance(item, dict) and item.get("unconfirmed")]
    if active_behavior:
        facts.append(f"本次有{len(active_behavior)}条未确认的本地可观察候选；不把它们解释为情绪、人格或能力结论")

    if not tasks and supply_facts:
        headline = f"当日保留{len(supply_facts)}项原练习安排，" + ("复盘已跳过" if reflection_skipped else "已保存孩子愿意分享的内容")
    elif not tasks and no_homework_declared:
        headline = "今天没有需要执行的计划，" + ("复盘已跳过" if reflection_skipped else "已保存孩子愿意分享的内容")
    else:
        headline = f"完成{len(completed)}/{len(tasks)}项计划，" + ("今天复盘已跳过" if reflection_skipped else "已保存孩子愿意分享的内容")
    summary = {
        'document_revisit_practice': revisit_practice,
        'document_revisit_practice_fingerprint': evidence.get('document_revisit_practice_fingerprint'),
        'pet_audio_practice': oral_practice,
        'pet_audio_practice_fingerprint': evidence.get('pet_audio_practice_fingerprint'),
        "supply_obligations_as_of": supply_facts,
        "supply_obligations_as_of_status": supply_status,
        "headline": headline,
        "facts": facts,
        "reliable_learning_fingerprint": (evidence.get("reliable_learning") or {}).get("evidence_fingerprint"),
        "mistake_mastery_fingerprint": evidence.get("mistake_mastery_fingerprint"),
        "behavior_source_fingerprint": evidence.get('behavior_source_fingerprint'),
        "reliable_learning_dependencies": evidence.get("reliable_learning_dependencies") or [],
        "specialized_reference_fingerprint": evidence.get("specialized_reference_fingerprint"),
        "specialized_stage_reports": specialized_stage_reports,
        "interpretation": "这是今天一次学习会话的记录，不用于评价孩子的能力或性格。",
        # Goal cycles summarize evidence already represented by completed plan
        # items, so do not count them a second time in the daily evidence total.
        "evidence_count": len(recalls) + len(tasks) + len(knowledge_evidence) + len(mistakes) + len(pet_vocabulary_attempts) + declaration_count + (0 if reflection_skipped else 1),
    }
    for oral in oral_practice:
        facts.append(f"原音练习建议：{oral['question']}；已保存{len(oral['practices'])}次新录音复练，不计为掌握或可靠考试评分。")
    from .document_revisit_memory import describe as describe_revisit
    facts.extend(describe_revisit(item) for item in revisit_practice)

    duration = 10 if child["grade"] == "四年级" else 12
    incomplete = next((task for task in tasks if task["status"] != "completed"), None)
    correction = needs_correction[0] if needs_correction else None
    fallback_action = (
        f"订正「{correction['title']}」中标记的一小步"
        if correction else
        f"确认「{incomplete['title']}」的下一步"
        if incomplete else
        "确认明天最重要的一项任务"
    )
    action_text = next_action or fallback_action
    sufficient_for_specific_guidance = bool(
        tasks or recalls or knowledge_evidence or mistakes or pet_vocabulary_attempts or learned or difficult or next_action
        or no_recall_declared or no_homework_declared
    )
    if not sufficient_for_specific_guidance:
        focus = "insufficient_evidence"
    elif not tasks and no_homework_declared:
        focus = "empty_day"
    elif correction:
        focus = "correction"
    elif incomplete:
        focus = "unfinished_task"
    elif difficult:
        focus = "reported_difficulty"
    elif reflection_skipped:
        focus = "reflection_skipped"
    else:
        focus = "completed_day" if completed else "limited_evidence"
    qualified = {item.get("evidence_id"): item for item in (evidence.get("reliable_learning") or {}).get("items", [])
                 if isinstance(item, dict) and item.get("evidence_id")}
    learning_check = next((item for item in knowledge_evidence
        if item.get("evidence_id") and qualified.get(item["evidence_id"]) == item
        and item.get("knowledge_node_id") and item.get("knowledge_title")
        and type(item.get("correctness")) in (int, float) and 0 <= item["correctness"] < 1), None)
    if learning_check and not next_action and focus in {"completed_day", "limited_evidence"}:
        focus = "verified_learning_check"
        duration = min(duration, 8)
    retention_check = next((item for item in knowledge_evidence
        if item.get('evidence_id') and qualified.get(item['evidence_id']) == item
        and item.get('knowledge_node_id') and item.get('knowledge_title')
        and item.get('retention_status') == 'retention_observed'
        and item.get('retention_basis_refs') and item.get('correctness') == 1
        and item.get('independent') and not item.get('hint_level')), None)
    if retention_check and not next_action and focus in {'completed_day', 'limited_evidence'}:
        focus = 'verified_retention_support'
        duration = min(duration, 5)
    feedback_context = {
        "focus": focus, "child_stage": child.get("grade"),
        "subjects": sorted(subjects), "version": "local_rules_v2",
    }
    if focus == "verified_learning_check":
        feedback_context.update(subjects=[learning_check["subject"]],
            target_knowledge_node_id=learning_check["knowledge_node_id"], action_protocol="verified_learning_check_v1")
    elif focus == 'verified_retention_support':
        feedback_context.update(subjects=[retention_check['subject']],
            target_knowledge_node_id=retention_check['knowledge_node_id'], action_protocol='verified_retention_support_v1')
    # Select comparable, explicitly executed actions before limiting history.
    # Unknown context and report ratings cannot establish intervention effects.
    feedback = [
        item for item in (evidence.get("coaching_feedback") or [])
        if isinstance(item, dict)
        and item.get("binding_status") == "bound"
        and item.get("feedback_kind", "action_execution") == "action_execution"
        and (item.get("executed") is True or (type(item.get("executed")) is int and item["executed"] == 1))
        and item.get("child_id") == child.get("child_id")
        and item.get("feedback_context") == feedback_context
        and any(type(item.get(key)) is int and 1 <= item[key] <= 5 for key in ("effectiveness", "stress_rating"))
    ][:5]
    report_feedback = evidence.get("report_feedback") or []
    coaching_decisions = [
        item for item in (evidence.get("coaching_decisions") or [])
        if isinstance(item, dict) and item.get("decision") in {"adopt", "custom", "skip"}
    ]
    report_safe_behavior_feedback = [
        item for item in (evidence.get("behavior_intervention_feedback") or [])
        if isinstance(item, dict) and isinstance(item.get("summary"), str) and item["summary"].strip()
    ][:3]
    behavior_model_summary = evidence.get("behavior_model_summary") if isinstance(evidence.get("behavior_model_summary"), dict) else {}
    # Legacy records are allowed only when the new report-safe feed is absent;
    # combining them could count one prompt twice during the migration.
    if not report_safe_behavior_feedback and not evidence.get("behavior_context_authoritative"):
        report_safe_behavior_feedback = [
            item for item in (evidence.get("legacy_behavior_intervention_feedback") or [])
            if isinstance(item, dict) and isinstance(item.get("summary"), str) and item["summary"].strip()
        ][:3]
    rated_effectiveness = [item["effectiveness"] for item in feedback if type(item.get("effectiveness")) is int and 1 <= item["effectiveness"] <= 5]
    rated_stress = [item["stress_rating"] for item in feedback if type(item.get("stress_rating")) is int and 1 <= item["stress_rating"] <= 5]
    average_effectiveness = sum(rated_effectiveness) / len(rated_effectiveness) if rated_effectiveness else None
    average_stress = sum(rated_stress) / len(rated_stress) if rated_stress else None
    adaptation_note = "首次使用这类建议，先采用短时、单问题方式，等待家长反馈后再调整。"
    if average_stress is not None and average_stress >= 4:
        duration = max(2 if focus == 'verified_retention_support' else 6, duration - 4)
        adaptation_note = "过去反馈显示沟通压力偏高，本次缩短时长，并保留孩子提前停止的权利。"
    elif average_effectiveness is not None and average_effectiveness <= 2:
        duration = max(2 if focus == 'verified_retention_support' else 7, duration - 3)
        adaptation_note = "过去反馈显示类似建议效果有限，本次只保留一个最小行动，避免重复拉长对话。"
    elif average_effectiveness is not None and average_effectiveness >= 4:
        adaptation_note = "过去反馈显示短时、先让孩子选择的方式较有效，本次继续沿用但不增加时长。"
    latest_decision = coaching_decisions[0] if coaching_decisions else None
    if latest_decision:
        decision = latest_decision["decision"]
        if decision == "skip":
            adaptation_note += " 上次家长选择跳过；这不表示孩子拒绝。本次仍保留可选、低压力的安排。"
        elif decision == "custom":
            adaptation_note += " 上次家长修改了建议；本次保留家长继续调整的空间，并只结合新事实安排。"
        else:
            adaptation_note += " 上次家长采用了建议；本次仍需结合新的学习记录和反馈调整。"
    declined_reminder = next((item for item in report_safe_behavior_feedback if item.get("kind") == "intervention_declined"), None)
    accepted_reminder = next((item for item in report_safe_behavior_feedback if item.get("kind") == "intervention_accepted"), None)
    if declined_reminder:
        adaptation_note += " 近期行为反馈显示孩子曾选择暂不接受提醒；本次不追问原因，先让孩子决定是否继续。"
    elif accepted_reminder:
        adaptation_note += " 近期行为反馈显示孩子曾接受温和提醒；本次仍保留选择权，不据此增加任务。"
    correction_title = correction["title"] if correction else ""
    incomplete_title = incomplete["title"] if incomplete else ""
    if focus == "insufficient_evidence":
        evidence_basis = ["本次没有足够的已确认学习事实"]
        scenario = "本次只有年级与计划背景，没有记录具体学习事实。今晚不把空白记录解释成问题，也不安排补偿性学习。"
        why_this_fits = "证据不足时，最合适的沟通是承认不知道，而不是追问、比较或替孩子下结论。"
        script = [
            "今天的学习记录不够，我不会把空白当成问题。",
            "你愿意补充时，我只听一件具体发生的事。",
            "今晚不追加学习任务；如果你想说，我们明天再一起确认第一步。",
        ]
        action_text = "不追加学习，等待下一次已确认记录"
        duration = 0
        action_steps = ["今晚不临时补练或盘问", "孩子主动补充时，只记录一件具体事实", "没有新事实就结束对话"]
        avoid = [
            {"do_not_say": "你肯定又没认真。", "say_instead": "我现在没有足够记录，不给你下结论。"},
            {"do_not_say": "别人都在学习，你怎么没有记录？", "say_instead": "每个人今天的记录不同；我们只等你愿意确认的事实。"},
            {"do_not_say": "不说清楚就别休息。", "say_instead": "休息不需要拿解释来交换。"},
            {"do_not_say": "明天必须加倍补回来。", "say_instead": "明天按实际任务重新安排，不用用加量补空白。"},
        ]
    elif focus == "empty_day":
        evidence_basis = ["孩子确认今天没有校内作业"]
        if reflection_skipped:
            evidence_basis.append("孩子选择跳过复盘")
        if no_recall_declared and not recalls:
            evidence_basis.append("孩子暂时想不起课堂内容")
        scenario = "孩子今天确认没有校内作业，并选择不补充复盘。今晚不追加练习，也不追问课堂细节。" if reflection_skipped else "孩子今天确认没有校内作业。今晚只做轻量收尾，不临时追加学习负担。"
        why_this_fits = f"这些都是孩子本次明确提供的信息，不用于评价态度、记忆或能力。{adaptation_note}"
        script = [
            "你已经把今天没有校内作业这件事说清楚了，谢谢你如实告诉我。",
            "今天不补作业，也不用补充解释。我们只确认明天最重要的一件事。",
            f"我们用{duration}分钟做“{action_text}”，你说完选择就可以停。",
            "到时间就结束，今晚不临时加练。",
        ]
        action_steps = ["不追加练习，先让孩子确认明天最重要的一件事", "一起准备好明天第一项任务需要的物品", "孩子确认后立即结束，不借机追问课堂细节"]
        avoid = [
            {"do_not_say": "真的没作业？别骗我。", "say_instead": "我按你今天确认的信息记录；如果后来想起了，我们再补充。"},
            {"do_not_say": "既然没作业，就把练习册多做两页。", "say_instead": "今晚不临时加练，只把明天第一件事准备好。"},
            {"do_not_say": "为什么上课讲了什么都想不起来？", "say_instead": "现在想不起来也可以，看到教材后想补充再告诉我。"},
            {"do_not_say": "不复盘就是学习态度有问题。", "say_instead": "今天可以不复盘，我们按已经确认的事实收尾。"},
        ]
    elif focus == "correction":
        evidence_basis = [f"{correction_title}被孩子标记为仍需订正"]
        if difficult:
            evidence_basis.append(f"孩子自述困难：{difficult}")
        scenario = f"孩子主动把“{correction_title}”标记为仍需订正" + (f"，并说“{difficult}”比较困难" if difficult else "") + "。今晚只处理孩子指出的一处不确定。"
        why_this_fits = f"先承认孩子能发现问题，再处理一个最小订正点，避免把订正变成追责。{adaptation_note}"
        script = [
            f"我看到你把“{correction_title}”标成还需要订正，能指出不确定的地方很重要。",
            f"你说“{difficult}”比较卡，我们先圈出最不确定的一步。" if difficult else "你先指出最不确定的一步，不用从头全部重做。",
            f"我们只用{duration}分钟做“{action_text}”，你想先说思路，还是先在纸上标出来？",
            "时间到就停；还没解决的地方写成一个问题，明天再继续。",
        ]
        action_steps = ["请孩子指出自己认为需要订正的第一处", "让孩子先说已经确定的条件或步骤", "只修正一个关键点，并由孩子复述变化"]
        difficult_phrase = difficult or "这一处"
        avoid = [
            {"do_not_say": "怎么又错在这种地方？", "say_instead": f"你已经指出“{correction_title}”需要订正，我们只看第一处不确定。"},
            {"do_not_say": f"这么简单的{difficult_phrase}都不会？", "say_instead": f"你说“{difficult_phrase}”卡住，我们先把已知条件和问题分开。"},
            {"do_not_say": "你看别人订正一次就会了。", "say_instead": "我们只比较你这一次订正前后多弄懂了哪一步。"},
            {"do_not_say": "今天不全部改对就别休息。", "say_instead": f"先做{duration}分钟，没完成的写成问题，到时照样休息。"},
        ]
    elif focus == "unfinished_task":
        defer_reason = incomplete.get("reschedule_reason")
        evidence_basis = [f"{incomplete_title}今天尚未完成"]
        if defer_reason:
            evidence_basis.append(f"延期原因：{defer_reason}")
        if difficult:
            evidence_basis.append(f"孩子自述困难：{difficult}")
        scenario = f"“{incomplete_title}”今天尚未完成" + (f"，记录的延期原因是“{defer_reason}”" if defer_reason else "") + "。今晚先确认明天是否继续，不把未完成解释成拖拉或态度问题。"
        why_this_fits = f"把决定缩小到一个可执行选择，既保留任务责任，也避免用压力换取仓促完成。{adaptation_note}"
        script = [
            f"我看到“{incomplete_title}”今天还没有完成，我们先不追究原因。",
            "你来选：现在做第一小步，还是一起确定明天几点继续？",
            f"如果现在做，我们只用{duration}分钟处理“{action_text}”。",
            "选好后按约定执行，时间到就停。",
        ]
        action_steps = ["让孩子在现在开始和明确延期之间二选一", "如果开始，只写下或完成第一小步", "如果延期，当场确定明天的具体时间和材料"]
        avoid = [
            {"do_not_say": "怎么到现在还没做完？", "say_instead": f"“{incomplete_title}”还没完成，我们现在只决定下一步。"},
            {"do_not_say": "你就是拖拉。", "say_instead": "这项任务今天停在这里，我们看看第一步能不能再缩小。"},
            {"do_not_say": "别人早就写完了。", "say_instead": "我们只看你现在能完成的最小一步。"},
            {"do_not_say": "今晚必须全部做完。", "say_instead": f"先做{duration}分钟，或者明确排到明天，不用靠熬夜解决。"},
        ]
    elif focus == "reported_difficulty":
        evidence_basis = [f"孩子自述困难：{difficult}"]
        if feeling:
            evidence_basis.append(f"孩子自述感受：{feeling}")
        scenario = f"孩子今天说“{difficult}”比较困难" + (f"，感受是“{feeling}”" if feeling else "") + "。今晚只澄清一个小卡点。"
        why_this_fits = f"建议直接使用孩子自己的表述，不扩大成能力或性格结论。{adaptation_note}"
        script = [
            "谢谢你把今天不确定的地方说出来。",
            f"你说“{difficult}”，是哪个小地方最卡？你只要选一个。",
            f"我们用{duration}分钟做“{action_text}”，你想自己先试，还是我陪你看第一步？",
            "到时间就停，没弄懂的部分记成问题，不要求一次解决。",
        ]
        action_steps = ["让孩子用手指或一句话定位最卡的小地方", "先请孩子说已经知道什么", "只补一个提示，再让孩子自己完成下一步"]
        avoid = [
            {"do_not_say": f"{difficult}这么简单，怎么还不会？", "say_instead": f"你说“{difficult}”卡住，我们先找最小的一处。"},
            {"do_not_say": "我都讲几遍了，你到底听没听？", "say_instead": "刚才的说法可能不合适，我们换一种更短的解释。"},
            {"do_not_say": "你就是不认真。", "say_instead": "我只看这一次具体停在哪一步，不给你下结论。"},
            {"do_not_say": "今天必须彻底弄懂。", "say_instead": f"今天先推进{duration}分钟，剩下的问题可以保留。"},
        ]
    elif focus == "verified_learning_check":
        title = learning_check["knowledge_title"]
        evidence_basis = [f"「{title}」的一次已核验作答未完全正确；这不等于稳定薄弱或不会。"]
        scenario = f"本次「{title}」留下了需要核对的作答记录。没有口述困难，不等于没有可核对的地方。"
        why_this_fits = f"只围绕这一条已核验记录邀请孩子选择下一步，不猜测错因，不要求家长重新教学。{adaptation_note}"
        action_text = f"邀请孩子选择明天是否核对「{title}」的一步"
        script = [
            f"这次「{title}」有一步还需要核对，不代表你一直不会。",
            "你愿意指出想核对的那一步，还是今天先停下？两种选择都可以。",
            f"愿意说的话，我们最多用{duration}分钟只记一个问题，不要求现在改对。",
            "明天仍按实际计划和休息安排；需要复核时交给学习伙伴，不临时加题。",
        ]
        action_steps = [f"共同查看「{title}」的这条作答记录，不根据一次结果给孩子贴标签",
            "只问一次愿不愿意指出需要核对的一步；拒绝时立即结束",
            "愿意时记录一个具体问题及所需材料，作为明天是否复核的候选，不要求当晚重做",
            "只记录是否进行了沟通、何时结束和家长感到的压力，不把完成沟通当作学习进步"]
        avoid = [
            {"do_not_say": "你就是这块基础太差。", "say_instead": "这里只看到一次作答，我们只核对你愿意指出的一步。"},
            {"do_not_say": "我讲完了就算你会了。", "say_instead": "是否掌握还需要后续独立作答验证。"},
            {"do_not_say": "今晚必须多做几题补回来。", "say_instead": "今天不临时加题，明天按实际计划决定是否复核。"},
        ]
    elif focus == 'verified_retention_support':
        title = retention_check['knowledge_title']
        evidence_basis = [f'「{title}」有前次独立正确作答，本次间隔到期后的复测也独立通过；不代表所有题型已掌握。']
        scenario = f'「{title}」已经出现隔天保持的证据。今晚可以认可孩子用的方法，不必重新讲一遍或加题。'
        why_this_fits = f'依据同一孩子的有效前置作答和本次复测，支持继续独立尝试，不由一次结果推断性格或全面掌握。{adaptation_note}'
        action_text = f'用一次可选的短对话认可「{title}」的方法，约定下次需要哪种帮助'
        script = [
            f'上次练过「{title}」，这次隔了一段时间你还能自己完成，值得记下来。',
            '你愿意说说这次哪一步帮到了你吗？不想说也没关系，我们就先休息。',
            f'愿意的话，我们最多聊{duration}分钟，只记一个你想保留的方法。',
            '下次碰到新题，你希望先自己试、我等你求助，还是先一起读题？你来选。',
            '今晚不临时加题。后面的新题和复习仍按计划来，需要时我们再调整帮助。',
        ]
        action_steps = [f'先核对报告中「{title}」的这次复测记录，只描述已经发生的事',
            '只邀请一次分享方法；孩子拒绝时立即结束，不追问原因',
            '愿意时记下一个方法，以及孩子希望下次怎样获得帮助，不替孩子解题',
            f'{duration}分钟到时停止；只记录沟通是否执行和压力，不把沟通当作新学习证据']
        avoid = [
            {'do_not_say': '这下你全会了，以后不许再错。', 'say_instead': '这次隔天还能独立做出来，其他题型还可以慢慢验证。'},
            {'do_not_say': '既然做对了，今晚再加十道。', 'say_instead': '今天按原计划结束，不因为做对就增加负担。'},
            {'do_not_say': '一定是我盯着你才有进步。', 'say_instead': '这是你这次独立完成的记录，你愿意说说用了什么办法吗？'},
            {'do_not_say': '你必须马上给我讲明白。', 'say_instead': '现在不想讲也可以，想分享时我再听。'},
        ]
    elif focus == "reflection_skipped":
        evidence_basis = ["孩子选择跳过复盘"]
        if completed:
            evidence_basis.append(f"今天记录完成{len(completed)}项任务")
        scenario = "孩子今天选择跳过复盘。今晚不追问原因，只根据已确认的任务事实做一次低压力收尾。"
        why_this_fits = f"跳过本身不代表态度或能力。按{child['grade']}的负荷，只保留{duration}分钟的可选行动。{adaptation_note}"
        script = [
            f"我看到你今天已经记录完成{len(completed)}项任务，谢谢你把进度留下来。",
            "今天不需要再解释什么，我也不追问。",
            f"我们只用{duration}分钟做“{action_text}”，你可以自己选第一步。",
            "时间到就停；如果今天不想继续，也把明天第一步记下来。",
        ]
        action_steps = ["不追问跳过原因", "让孩子选择确认明日第一步或直接结束", "把选择记录下来后按时停止"]
        avoid = [
            {"do_not_say": "为什么连复盘都不愿意做？", "say_instead": "今天可以跳过，我们只按已经记录的任务收尾。"},
            {"do_not_say": "你是不是有什么事瞒着我？", "say_instead": "你想说时我会听，现在不用解释。"},
            {"do_not_say": "不总结就等于白学。", "say_instead": "今天已经完成的内容仍然算数，明天还可以再回想。"},
            {"do_not_say": "必须说清楚才能休息。", "say_instead": "你可以选择现在结束，休息不需要拿解释来交换。"},
        ]
    else:
        evidence_basis = [f"今天记录完成{len(completed)}项任务"] if completed else ["今天没有记录具体困难"]
        scenario = "孩子今天没有记录具体困难。今晚不补问，只根据已确认的任务做一次低压力收尾。"
        why_this_fits = f"按{child['grade']}的负荷，把行动控制在{duration}分钟，不因今天完成顺利而临时加量。{adaptation_note}"
        script = [
            f"我看到你今天已经记录完成{len(completed)}项任务，谢谢你把进度说清楚。",
            "今天没有需要追问的困难，我们只看明天的第一小步。",
            f"我们用{duration}分钟做“{action_text}”，由你决定从哪里开始。",
            "时间到就停，完成顺利也不临时加任务。",
        ]
        action_steps = ["请孩子说出今天有效的一种做法", "一起确认明天第一小步", "准备好材料后按时结束"]
        avoid = [
            {"do_not_say": "今天做得快，再多做两页吧。", "say_instead": "今天按计划完成了，我们把有效做法留到明天。"},
            {"do_not_say": "这次全对，说明你就是聪明。", "say_instead": "你这次用了有效的方法，具体是哪一步帮了你？"},
            {"do_not_say": "以后每天都必须像今天一样。", "say_instead": "每天状态会不同，我们明天再按实际情况安排。"},
            {"do_not_say": "没说困难就是都懂了。", "say_instead": "今天没有记录困难；以后发现不确定，随时可以补充。"},
        ]

    if goal_context:
        evidence_basis.append(f"学习阶段与目标：{goal_context}")
        why_this_fits += f" 当前学习阶段与目标：{goal_context}"
        action_steps.append("按既定阶段目标收尾，不把今晚变成额外加量。")
    if recent_confirmed_facts:
        evidence_basis.extend(f"近期确认记录：{fact}" for fact in recent_confirmed_facts)
        why_this_fits += " 近期记录只用于理解本次安排，不把它们外推成能力或性格结论。"
    if active_habit:
        evidence_basis.append(f"孩子已同意的七天小实验：{active_habit.get('title', '微习惯实验')}")
        why_this_fits += " 当前只支持孩子已经语音同意的一项微习惯实验，不追加第二项要求。"
        action_steps.append(
            f"只问一次“今天的{active_habit.get('title', '小实验')}用上了吗”，接受孩子回答，不追问。"
        )
        script.insert(-1, f"你同意的小实验今天只看这一件事：{active_habit.get('child_action', '')}")

    coaching = {
        "focus": focus,
        "feedback_context": feedback_context,
        "report_feedback": {
            "feedback_count": len(report_feedback),
            "latest_helpfulness": report_feedback[0].get("helpfulness") if report_feedback else None,
            "latest_stress_rating": report_feedback[0].get("stress_rating") if report_feedback else None,
            "note": "报告评价不代表行动已执行，不计入行动效果或沟通压力均值。",
        },
        "evidence_basis": evidence_basis,
        "scenario": scenario,
        "why_this_fits": why_this_fits,
        "adaptation": {
            "basis": "parent_feedback" if feedback else "cold_start",
            "feedback_count": len(feedback),
            "average_effectiveness": round(average_effectiveness, 1) if average_effectiveness is not None else None,
            "average_stress": round(average_stress, 1) if average_stress is not None else None,
            "note": adaptation_note,
        },
        "personalization": {
            "child_stage": child.get("grade", "当前年级"),
            "goal_context": goal_context or None,
            "recent_confirmed_facts": recent_confirmed_facts,
            "feedback_considered": len(feedback),
            "previous_decision": latest_decision["decision"] if latest_decision else None,
            "active_habit_experiment": active_habit,
        },
        "script": script,
        "action": {
            "what": action_text,
            "when": "本次学习结束后的下一次亲子沟通时；孩子拒绝时不执行。",
            "duration_minutes": duration,
            "steps": action_steps,
            "stop_condition": "到时立即停止；孩子明显烦躁、疲惫或拒绝时提前停止。",
        },
        "avoid": avoid,
    }
    behavior_basis = [item["summary"].strip()[:200] for item in report_safe_behavior_feedback]
    if behavior_basis:
        coaching["evidence_basis"] = list(dict.fromkeys([*coaching["evidence_basis"], *behavior_basis]))
    sources = [
        item for item in (evidence.get("coaching_evidence_sources") or [])
        if isinstance(item, dict) and isinstance(item.get("evidence_ref"), str)
    ]
    if focus == "verified_learning_check":
        from .repository import coaching_evidence_ref
        target = {key: learning_check[key] for key in ("evidence_id", "knowledge_node_id", "knowledge_title", "question_identity")}
        coaching["action"]["learning_target"] = {**target, "mastery_claim_allowed": False,
            "evidence_kind": "qualified_learning_attempt", "next_use": "optional_recheck_candidate"}
        sources.append({"evidence_ref": coaching_evidence_ref(child["child_id"], "learning_evidence", learning_check["evidence_id"]),
            "source": f"本次已核验作答：{learning_check['knowledge_title']}", "occurred_at": learning_check["occurred_at"]})
    elif focus == 'verified_retention_support':
        from .repository import coaching_evidence_ref
        target = {key: retention_check[key] for key in
                  ('evidence_id', 'knowledge_node_id', 'knowledge_title', 'question_identity', 'retention_basis_refs')}
        coaching['action']['learning_target'] = {**target, 'mastery_claim_allowed': False,
            'evidence_kind': 'qualified_mistake_retention', 'next_use': 'optional_independence_support'}
        sources.append({'evidence_ref': coaching_evidence_ref(child['child_id'], 'learning_evidence', target['evidence_id']),
            'source': f"本次到期独立复测：{target['knowledge_title']}", 'occurred_at': retention_check['occurred_at']})
    coaching["evidence_sources"] = list({item["evidence_ref"]: item for item in sources}.values())
    coaching["evidence_refs"] = [item["evidence_ref"] for item in coaching["evidence_sources"]]
    coaching["uncertainty"] = (
        "行为记录只说明一次已记录的提醒反馈，不用于判断情绪、态度、人格或因果。"
        if behavior_basis else
        "建议只依据本次确认的学习记录和已保存反馈；未记录的信息不作推断。"
    )
    if behavior_model_summary.get("uncertainty"):
        coaching["uncertainty"] += " " + str(behavior_model_summary["uncertainty"]).strip()[:500]
    coaching["script_phases"] = _coaching_script_phases(coaching["script"])
    return summary, coaching


def _coaching_script_phases(script: list[str]) -> list[dict]:
    """Give parents a complete, repeatable five-part conversation without a diagnosis."""
    lines = [str(item).strip() for item in script if isinstance(item, str) and item.strip()]
    opening = lines[0] if lines else "我只想一起看一件已经记录的事，不急着下结论。"
    empathy = lines[1] if len(lines) > 1 else "你可以先说自己的感受；现在不想说也可以。"
    choice = lines[2] if len(lines) > 2 else "你来选下一小步，或选择今天先结束。"
    closing = lines[-1] if len(lines) > 3 else "按约定结束，剩下的问题留到下一次有记录时再看。"
    return [
        {"phase": "开场", "text": opening},
        {"phase": "倾听", "text": "先停下来听孩子说完；只确认一件具体发生的事，不追问、不要求解释。"},
        {"phase": "共情", "text": empathy},
        {"phase": "共同选择", "text": choice},
        {"phase": "收尾", "text": closing},
    ]
