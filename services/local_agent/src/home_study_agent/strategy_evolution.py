from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from .database import Database
from .learning_feedback import build_learning_feedback, school_original_observations
from .learning_obligations import question_identity, project_child_mastery_evidence
from .local_supply import LocalSupplyError, validate_item
from .repository import UnknownChildError, utc_now


STRATEGY_KEY = "tutor_hint_pacing"
BASELINE_VERSION = "tutor_layered_v2"
CANDIDATE_VERSION = "tutor_brief_reflective_v1"
CANDIDATE_CONFIG = {
    "max_short_sentences": 3,
    "ask_child_to_restate_known_fact": True,
    "allowed_hint_levels": [1, 2, 3],
    "changes_answer_boundary": False,
    "changes_safety_policy": False,
    "changes_privacy_policy": False,
    "changes_family_rules": False,
}
CANDIDATE_PROMPT = (
    "低风险提示节奏策略已启用：只在第1至3级提示中使用不超过3个短句；"
    "先请孩子复述一个已知条件，再给下一小步。不得改变提示级别、答案边界或安全规则。"
)
STUCK_VERSION = "tutor_single_step_v1"
INDEPENDENT_VERSION = "tutor_independent_first_v1"
CANDIDATES = {
    CANDIDATE_VERSION: (CANDIDATE_CONFIG, CANDIDATE_PROMPT),
    STUCK_VERSION: (
        {**CANDIDATE_CONFIG, "ask_child_to_restate_known_fact": False,
         "pacing_mode": "locate_block_then_one_step"},
        "低风险提示节奏策略已启用：先问孩子具体卡在哪一步，等待回答后只给一个小步骤；"
        "每轮不超过3个短句。不得提高提示级别、透露答案或改变安全与家庭规则。",
    ),
    INDEPENDENT_VERSION: (
        {**CANDIDATE_CONFIG, "ask_child_to_restate_known_fact": False,
         "pacing_mode": "independent_attempt_before_hint"},
        "低风险提示节奏策略已启用：先邀请孩子独立说出下一步并等待作答；"
        "若孩子仍卡住，在当前提示级别内给一个小提示，不强迫继续独立尝试。"
        "每轮不超过3个短句，不得改变提示等级、答案边界、安全或家庭规则。",
    ),
}
FEEDBACK_OUTCOMES = {"understood", "still_stuck", "answer_attempt"}


def _evidence_contract(phase: str) -> dict:
    return {
        "evaluation_kind": {
            "replay": "baseline_review_not_candidate_replay",
            "shadow": "applicability_only",
            "active": "controlled_trial",
        }[phase],
        "understood_rate_evidence_kind": "child_self_report",
        "learning_gain": None,
        "effectiveness_status": "unverified",
        "candidate_execution_verified": False,
    }


class StrategyEvolutionService:
    def __init__(self, database: Database):
        self.database = database

    def list_for_child(self, child_id: str) -> list[dict]:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_child(connection, child_id)
            rows = connection.execute(
                """
                SELECT * FROM strategy_experiment WHERE child_id = ?
                ORDER BY created_at DESC
                """,
                (child_id,),
            ).fetchall()
            result = [self._result(connection, self._refresh_learning(connection, row)) for row in rows]
            connection.commit()
            return result

    def ensure_candidate(self, child_id: str, actor: str = "system") -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_child(connection, child_id)
            existing = connection.execute(
                """
                SELECT * FROM strategy_experiment
                WHERE child_id = ? AND strategy_key = ?
                  AND status IN ('draft', 'replay_passed', 'shadow', 'active')
                ORDER BY created_at DESC LIMIT 1
                """,
                (child_id, STRATEGY_KEY),
            ).fetchone()
            if existing:
                existing = self._refresh_learning(connection, existing)
                connection.commit()
                return self._result(connection, existing)
            experiment_id = str(uuid4())
            metrics = self._aggregate_metrics(connection, child_id)
            version, config = self._select_candidate(metrics)
            config["selection"]["basis"] = self._capture_selection_basis(connection, child_id, config["selection"])
            connection.execute(
                """
                INSERT INTO strategy_experiment(
                  experiment_id, child_id, strategy_key, baseline_version, candidate_version,
                  candidate_config_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    experiment_id, child_id, STRATEGY_KEY, BASELINE_VERSION,
                    version, json.dumps(config, ensure_ascii=False), now, now,
                ),
            )
            self._transition(
                connection, experiment_id, child_id, None, "draft", actor,
                "按现有证据选择受限提示节奏候选；尚未验证效果，安全、隐私和家庭规则保持冻结。",
                config["selection"], now,
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM strategy_experiment WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
            return self._result(connection, row)

    def maybe_propose(self, child_id: str) -> dict | None:
        """Create a bounded candidate only after enough real baseline feedback exists."""
        with self.database.connect() as connection:
            self._require_child(connection, child_id)
            open_experiment = connection.execute(
                """
                SELECT experiment_id FROM strategy_experiment
                WHERE child_id = ? AND strategy_key = ?
                  AND status IN ('draft', 'replay_passed', 'shadow', 'active')
                LIMIT 1
                """,
                (child_id, STRATEGY_KEY),
            ).fetchone()
            if open_experiment:
                return self.get(open_experiment["experiment_id"])
            metrics = self._aggregate_metrics(connection, child_id)
            if metrics["tutor_feedback_count"] < 3 and not any(
                    node['attempt_count'] or node['status'] in {'recheck_needed', 'supported_only'}
                    for node in metrics["reliable_learning_observations"]["nodes"]):
                return None
        return self.ensure_candidate(child_id, actor="system")

    def run_replay(self, experiment_id: str) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            experiment = self._require_experiment(connection, experiment_id)
            self._guard_selection(connection, experiment)
            if experiment["status"] != "draft":
                connection.rollback()
                raise InvalidStrategyTransitionError("只有草稿候选可以运行历史回放。")
            metrics = self._aggregate_metrics(connection, experiment["child_id"])
            allowlist_passed = self._experiment_is_low_risk(experiment)
            metrics["allowlist_passed"] = allowlist_passed
            metrics["minimum_feedback_required"] = 3
            connection.execute(
                "UPDATE strategy_experiment SET replay_metrics_json = ?, updated_at = ? WHERE experiment_id = ?",
                (json.dumps(metrics, ensure_ascii=False), now, experiment_id),
            )
            self._evaluation_event(
                connection, experiment_id, experiment["child_id"], "replay", "historical_replay",
                float(metrics["tutor_feedback_count"]), f"replay:{now}", metrics, now,
            )
            if ((metrics["tutor_feedback_count"] < 3 and not any(
                    node['attempt_count'] or node['status'] in {'recheck_needed', 'supported_only'}
                    for node in metrics["reliable_learning_observations"]["nodes"]))
                    or not allowlist_passed):
                connection.commit()
                raise StrategyEvidenceInsufficientError(metrics)
            connection.execute(
                "UPDATE strategy_experiment SET status = 'replay_passed', updated_at = ? WHERE experiment_id = ?",
                (now, experiment_id),
            )
            self._transition(
                connection, experiment_id, experiment["child_id"], "draft", "replay_passed",
                "system", "历史基线自述或合格作答达到审查门槛，候选通过固定白名单；未执行候选回放，未验证学习增益。", metrics, now,
            )
            connection.commit()
            return self.get(experiment_id)

    def start_shadow(self, experiment_id: str) -> dict:
        return self._parent_transition(
            experiment_id, "replay_passed", "shadow", "开始影子评估；记录适用回合但不改变孩子实际收到的提示。"
        )

    def activate(self, experiment_id: str) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            experiment = self._require_experiment(connection, experiment_id)
            self._guard_selection(connection, experiment)
            if experiment["status"] != "shadow":
                connection.rollback()
                raise InvalidStrategyTransitionError("只有影子期候选可以启用。")
            shadow_metrics = self._phase_metrics(connection, experiment_id, "shadow")
            shadow_metrics["allowlist_passed"] = self._experiment_is_low_risk(experiment)
            if (shadow_metrics["eligible_turns"] < 3
                    or shadow_metrics["policy_replacements"]
                    or not shadow_metrics["allowlist_passed"]):
                connection.commit()
                raise StrategyEvidenceInsufficientError({
                    **shadow_metrics, "minimum_shadow_turns_required": 3,
                })
            connection.execute(
                """
                UPDATE strategy_experiment
                SET status = 'active', shadow_metrics_json = ?, active_metrics_json = ?,
                    activated_at = ?, updated_at = ? WHERE experiment_id = ?
                """,
                (json.dumps(shadow_metrics), json.dumps(_evidence_contract("active")), now, now, experiment_id),
            )
            self._transition(
                connection, experiment_id, experiment["child_id"], "shadow", "active",
                "parent", "影子期仅通过适用性检查，家长授权受控试用；候选尚未执行，学习增益未验证。", shadow_metrics, now,
            )
            connection.commit()
            return self.get(experiment_id)

    def rollback(self, experiment_id: str, reason: str, actor: str = "parent") -> dict:
        now = utc_now()
        clean_reason = reason.strip()
        if len(clean_reason) < 2:
            raise InvalidStrategyTransitionError("请说明回滚原因。")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            experiment = self._require_experiment(connection, experiment_id)
            if experiment["status"] not in {"shadow", "active", "replay_passed"}:
                connection.rollback()
                raise InvalidStrategyTransitionError("当前策略状态不能回滚。")
            metrics = self._phase_metrics(connection, experiment_id, "active")
            connection.execute(
                """
                UPDATE strategy_experiment SET status = 'rolled_back', rollback_reason = ?,
                  active_metrics_json = ?, ended_at = ?, updated_at = ? WHERE experiment_id = ?
                """,
                (clean_reason, json.dumps(metrics), now, now, experiment_id),
            )
            self._transition(
                connection, experiment_id, experiment["child_id"], experiment["status"],
                "rolled_back", actor, clean_reason, metrics, now,
            )
            connection.commit()
            return self.get(experiment_id)

    def get(self, experiment_id: str) -> dict:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_experiment(connection, experiment_id)
            row = self._refresh_learning(connection, row)
            result = self._result(connection, row)
            connection.commit()
            return result

    def refresh_active_learning(self, child_id: str | None = None) -> list[dict]:
        """Idempotent synchronous refresh for existing maintenance, without starting a worker."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if child_id is not None:
                self._require_child(connection, child_id)
            rows = connection.execute(
                "SELECT * FROM strategy_experiment WHERE status = 'active' AND (? IS NULL OR child_id = ?) "
                "ORDER BY child_id, experiment_id", (child_id, child_id),
            ).fetchall()
            results = []
            for row in rows:
                refreshed = self._refresh_learning(connection, row)
                metrics = json.loads(refreshed["active_metrics_json"])
                results.append({
                    "experiment_id": refreshed["experiment_id"], "child_id": refreshed["child_id"],
                    "status": refreshed["status"], "learning_evidence_status": "selection_basis_invalidated"
                    if (refreshed["rollback_reason"] or "").startswith("selection_basis_invalidated:")
                    else metrics["learning_evidence"]["status"],
                })
            connection.commit()
            return results

    def tutor_context(self, child_id: str, hint_level: int) -> dict | None:
        if hint_level not in {1, 2, 3}:
            return None
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM strategy_experiment
                WHERE child_id = ? AND strategy_key = ? AND status IN ('shadow', 'active')
                ORDER BY created_at DESC LIMIT 1
                """,
                (child_id, STRATEGY_KEY),
            ).fetchone()
            if row:
                row = self._refresh_learning(connection, row)
            connection.commit()
        if not row or row["status"] not in {"shadow", "active"} or not self._experiment_is_low_risk(row):
            return None
        return {
            "experiment_id": row["experiment_id"],
            "mode": row["status"],
            "strategy_version": row["candidate_version"],
            "prompt_extension": CANDIDATES[row["candidate_version"]][1] if row["status"] == "active" else None,
        }

    def record_tutor_turn(self, turn_id: str, child_id: str, context: dict | None, safety_status: str) -> None:
        if not context:
            return
        now = utc_now()
        phase = context.get("mode")
        if phase not in {"shadow", "active"}:
            return
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            experiment = self._require_experiment(connection, context["experiment_id"])
            if (experiment["child_id"] != child_id or experiment["status"] != phase
                    or not self._experiment_is_low_risk(experiment)
                    or context.get("strategy_version") != experiment["candidate_version"]):
                connection.rollback()
                return
            experiment = self._refresh_selection(connection, experiment)
            if experiment["status"] != phase:
                connection.commit()
                return
            turn = connection.execute(
                "SELECT * FROM tutor_turn WHERE turn_id = ? AND child_id = ?",
                (turn_id, child_id),
            ).fetchone()
            if (not turn or turn["status"] != "completed" or turn["hint_level"] not in {1, 2, 3}
                    or turn["strategy_experiment_id"] not in {None, experiment["experiment_id"]}):
                connection.rollback()
                return
            # A retried receipt cannot reassign a shadow turn to the active trial.
            if connection.execute(
                "SELECT 1 FROM strategy_evaluation_event WHERE experiment_id = ? "
                "AND source_ref = ? AND event_type = 'eligible_tutor_turn'",
                (experiment["experiment_id"], turn_id),
            ).fetchone():
                connection.rollback()
                return
            if phase == "active" and context.get("prompt_extension") != CANDIDATES[experiment["candidate_version"]][1]:
                connection.rollback()
                return
            safety_status = turn["safety_status"] if turn["safety_status"] != "passed" else safety_status
            connection.execute(
                """
                UPDATE tutor_turn SET strategy_experiment_id = ?, strategy_version = ?
                WHERE turn_id = ? AND child_id = ?
                """,
                (context["experiment_id"], context["strategy_version"], turn_id, child_id),
            )
            self._evaluation_event(
                connection, context["experiment_id"], child_id, phase, "eligible_tutor_turn",
                1.0, turn_id, {"safety_status": safety_status,
                              "candidate_executed": phase == "active" and safety_status == "passed",
                              "candidate_version": experiment["candidate_version"],
                              "learning_scope": self._learning_scope(connection, turn),
                              "evidence_kind": "execution_receipt" if phase == "active" else "baseline_applicability"}, now,
            )
            if safety_status != "passed":
                self._evaluation_event(
                    connection, context["experiment_id"], child_id, phase, "policy_replacement",
                    1.0, turn_id, {"safety_status": safety_status}, now,
                )
            connection.commit()
        if phase == "active" and safety_status != "passed":
            self.rollback(context["experiment_id"], "活跃策略回合触发安全替换，已自动恢复基线。", actor="system")

    def record_tutor_feedback(self, turn_id: str, outcome: str) -> None:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            turn = connection.execute(
                """
                SELECT child_id, strategy_experiment_id FROM tutor_turn WHERE turn_id = ?
                """,
                (turn_id,),
            ).fetchone()
            if not turn or not turn["strategy_experiment_id"]:
                connection.rollback()
                return
            experiment = self._require_experiment(connection, turn["strategy_experiment_id"])
            if experiment["child_id"] != turn["child_id"]:
                connection.rollback()
                return
            feedback = self._latest_feedback(connection, turn_id, turn["child_id"])
            if not feedback or feedback["outcome"] not in FEEDBACK_OUTCOMES:
                connection.rollback()
                return
            # The callback argument may be a stale HTTP retry; use the system of record.
            outcome = feedback["outcome"]
            phase_row = connection.execute(
                """
                SELECT phase FROM strategy_evaluation_event
                WHERE experiment_id = ? AND source_ref = ? AND event_type = 'eligible_tutor_turn'
                ORDER BY occurred_at DESC LIMIT 1
                """,
                (experiment["experiment_id"], turn_id),
            ).fetchone()
            if not phase_row:
                connection.rollback()
                return
            phase = phase_row["phase"]
            self._evaluation_event(
                connection, experiment["experiment_id"], turn["child_id"], phase,
                "tutor_understood", None if outcome == "answer_attempt" else float(outcome == "understood"),
                turn_id, {"outcome": outcome, "feedback_id": feedback["feedback_id"],
                          "evidence_kind": "unscored_answer_attempt" if outcome == "answer_attempt" else "child_self_report"}, now,
            )
            metrics = self._phase_metrics(connection, experiment["experiment_id"], phase)
            field = "shadow_metrics_json" if phase == "shadow" else "active_metrics_json"
            connection.execute(
                f"UPDATE strategy_experiment SET {field} = ?, updated_at = ? WHERE experiment_id = ?",
                (json.dumps(metrics), now, experiment["experiment_id"]),
            )
            replay = json.loads(experiment["replay_metrics_json"] or "{}")
            should_rollback = (
                phase == "active" and experiment["status"] == "active"
                and metrics["self_report_count"] >= 3
                and replay.get("tutor_understood_rate") is not None
                and metrics["understood_rate"] + 0.25 < replay["tutor_understood_rate"]
            )
            connection.commit()
        refreshed = self.get(experiment["experiment_id"])
        if should_rollback and refreshed["status"] == "active":
            self.rollback(
                experiment["experiment_id"],
                "孩子自述理解比例较历史基线下降超过25个百分点，保守恢复基线；这不是学习效果因果结论。",
                actor="system",
            )

    def _parent_transition(self, experiment_id: str, expected: str, target: str, reason: str) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            experiment = self._require_experiment(connection, experiment_id)
            self._guard_selection(connection, experiment)
            if experiment["status"] != expected:
                connection.rollback()
                raise InvalidStrategyTransitionError(f"当前状态不能进入{target}。")
            if not self._experiment_is_low_risk(experiment):
                raise StrategyEvidenceInsufficientError({"allowlist_passed": False})
            connection.execute(
                "UPDATE strategy_experiment SET status = ?, updated_at = ? WHERE experiment_id = ?",
                (target, now, experiment_id),
            )
            self._transition(
                connection, experiment_id, experiment["child_id"], expected, target,
                "parent", reason, {}, now,
            )
            connection.commit()
            return self.get(experiment_id)

    @staticmethod
    def _aggregate_metrics(connection, child_id: str) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        feedback = connection.execute(
            """
            SELECT tf.*, tt.hint_level FROM tutor_turn tt
            JOIN tutor_feedback tf ON tf.turn_id = tt.turn_id AND tf.child_id = tt.child_id
              AND tf.session_id = tt.session_id
            WHERE tt.child_id = ? AND tt.created_at >= ?
              AND tt.strategy_experiment_id IS NULL AND tt.strategy_version IS NULL
              AND tt.status = 'completed' AND tt.safety_status = 'passed'
              AND tt.hint_level BETWEEN 1 AND 3
              AND tf.feedback_id = (
                SELECT latest.feedback_id FROM tutor_feedback latest
                WHERE latest.turn_id = tt.turn_id AND latest.child_id = tt.child_id
                  AND latest.session_id = tt.session_id
                ORDER BY latest.created_at DESC, latest.rowid DESC LIMIT 1
              )
            ORDER BY tf.created_at, tf.rowid
            """,
            (child_id, since),
        ).fetchall()
        tutor = StrategyEvolutionService._feedback_metrics(feedback)
        tasks = connection.execute(
            """
            SELECT AVG(CASE WHEN pi.status = 'completed' THEN 1.0 ELSE 0.0 END)
            FROM plan_item pi JOIN daily_plan dp ON dp.plan_id = pi.plan_id
            WHERE dp.child_id = ? AND pi.source_type != 'planned_break' AND dp.created_at >= ?
              AND pi.status IN ('completed', 'rescheduled')
            """,
            (child_id, since),
        ).fetchone()[0]
        learning = StrategyEvolutionService._reliable_learning(connection, child_id)
        intervention_count = connection.execute(
            "SELECT COUNT(*) FROM intervention WHERE child_id = ? AND decided_at >= ? AND status = 'active'",
            (child_id, since),
        ).fetchone()[0]
        correction_count = connection.execute(
            "SELECT COUNT(*) FROM observation_correction WHERE child_id = ? AND occurred_at >= ?",
            (child_id, since),
        ).fetchone()[0]
        return {
            **_evidence_contract("replay"),
            "window_days": 30,
            "tutor_feedback_count": tutor["self_report_count"],
            "tutor_understood_rate": tutor["understood_rate"],
            "eligible_tutor_turns": tutor["feedback_count"],
            "feedback_signals": tutor,
            "task_completion_rate": round(float(tasks), 4) if tasks is not None else None,
            "delayed_review_correctness": None,
            "reliable_learning_observations": learning,
            "background_metrics_attribution": "not_candidate_effect",
            "intervention_count": int(intervention_count),
            "misreminder_correction_count": int(correction_count),
        }

    @staticmethod
    def _phase_metrics(connection, experiment_id: str, phase: str) -> dict:
        rows = connection.execute(
            """
            SELECT e.*, tt.safety_status AS turn_safety, tt.strategy_version,
                   se.candidate_version FROM strategy_evaluation_event e
            JOIN tutor_turn tt ON tt.turn_id = e.source_ref AND tt.child_id = e.child_id
              AND tt.strategy_experiment_id = e.experiment_id
            JOIN strategy_experiment se ON se.experiment_id = e.experiment_id AND se.child_id = e.child_id
            WHERE e.experiment_id = ? AND e.phase = ?
              AND e.event_type = 'eligible_tutor_turn'
              AND tt.status = 'completed' AND tt.hint_level BETWEEN 1 AND 3
            """,
            (experiment_id, phase),
        ).fetchall()
        feedback = []
        executed = 0
        replacements = 0
        for row in rows:
            summary = json.loads(row["summary_json"])
            safe = row["turn_safety"] == "passed" and summary.get("safety_status") == "passed"
            replacements += int(not safe)
            executed += int(phase == "active" and safe and summary.get("candidate_executed") is True
                            and summary.get("candidate_version") == row["strategy_version"] == row["candidate_version"])
            latest = StrategyEvolutionService._latest_feedback(connection, row["source_ref"], row["child_id"])
            if latest:
                feedback.append(latest)
        signals = StrategyEvolutionService._feedback_metrics(feedback)
        return {
            **_evidence_contract(phase),
            **signals,
            "eligible_turns": len(rows),
            "policy_replacements": replacements,
            "candidate_executed_turns": executed,
            "candidate_execution_verified": executed > 0,
            "feedback_attribution": "baseline_only" if phase == "shadow" else "trial_observation_not_causal_gain",
        }

    @staticmethod
    def _latest_feedback(connection, turn_id: str, child_id: str):
        return connection.execute(
            """SELECT tf.*, tt.hint_level FROM tutor_feedback tf
            JOIN tutor_turn tt ON tt.turn_id = tf.turn_id AND tt.child_id = tf.child_id
              AND tt.session_id = tf.session_id
            WHERE tf.turn_id = ? AND tf.child_id = ?
            ORDER BY tf.created_at DESC, tf.rowid DESC LIMIT 1""",
            (turn_id, child_id),
        ).fetchone()

    @staticmethod
    def _feedback_metrics(rows) -> dict:
        counts = {outcome: 0 for outcome in FEEDBACK_OUTCOMES}
        dependency = 0
        refs = []
        for row in rows:
            outcome = row["outcome"]
            if outcome not in counts:
                continue
            counts[outcome] += 1
            dependency += int(outcome == "understood" and row["hint_level"] >= 2)
            refs.append({
                "source_ref": row["feedback_id"], "turn_id": row["turn_id"],
                "outcome": outcome, "hint_level": row["hint_level"],
                "evidence_kind": "unscored_answer_attempt" if outcome == "answer_attempt" else "child_self_report",
            })
        self_reports = counts["understood"] + counts["still_stuck"]
        return {
            "feedback_count": sum(counts.values()), "self_report_count": self_reports,
            "understood_rate": round(counts["understood"] / self_reports, 4) if self_reports else None,
            "understood_count": counts["understood"], "still_stuck_count": counts["still_stuck"],
            "unscored_answer_attempt_count": counts["answer_attempt"],
            "possible_hint_dependency_count": dependency,
            "signal_refs": refs,
        }

    @staticmethod
    def _select_candidate(metrics: dict) -> tuple[str, dict]:
        signals = metrics["feedback_signals"]
        stuck = signals["still_stuck_count"]
        dependent = signals["possible_hint_dependency_count"]
        nodes = metrics["reliable_learning_observations"]["nodes"]
        recheck = [node for node in nodes if node["status"] == "recheck_needed"]
        supported = [node for node in nodes if node["status"] == "supported_only"]
        if recheck:
            version, reason = STUCK_VERSION, "qualified_answer_requires_recheck"
        elif supported:
            version, reason = INDEPENDENT_VERSION, "qualified_answer_still_supported"
        elif stuck >= 2 and stuck >= dependent:
            version, reason = STUCK_VERSION, "repeated_self_report_still_stuck"
        elif dependent >= 2:
            version, reason = INDEPENDENT_VERSION, "possible_hint_dependency_not_verified"
        else:
            version, reason = CANDIDATE_VERSION, "insufficient_differentiating_evidence"
        return version, {
            **CANDIDATES[version][0],
            "selection": {
                "rule_version": "bounded_feedback_v1", "reason": reason,
                "evidence_kind": ("qualified_original_and_exact_observations"
                    if any(n.get('qualified_original_count') for n in recheck or supported)
                    else "qualified_diagnostic_answer" if recheck or supported else "child_self_report"),
                "effectiveness_status": "unverified",
                "window_days": metrics["window_days"], "signals": signals,
                "learning_window_days": metrics["reliable_learning_observations"]["window_days"],
                "learning_observations": recheck or supported,
                "limitations": ["自述不是掌握证据", "高提示后自述明白仅提示可能依赖",
                                "选择是待验证假设，次数不证明效果"],
            },
        }

    @staticmethod
    def _reliable_learning(connection, child_id: str) -> dict:
        if not connection.execute(
                "SELECT 1 FROM child_profile WHERE child_id = ? AND status = 'active'", (child_id,)).fetchone():
            return {"nodes": [], "window_days": 90, "status": "child_inactive"}
        return build_learning_feedback(connection, child_id)

    @staticmethod
    def _selection_learning_fact(receipt):
        return {key: receipt.get(key) for key in (
            "evidence_id", "knowledge_node_id", "diagnostic_item_id", "question_identity",
            "correctness", "hint_level", "independent", "confidence", "occurred_at",
            "receipt_kind", "review_task_id", "review_source_evidence_id", "mistake_id", "stage",
            "independence_reason", "tutor_refs",
        )}

    @staticmethod
    def _selection_feedback_fact(connection, child_id, reference):
        row = connection.execute("""SELECT tf.feedback_id,tf.turn_id,tf.session_id,tf.outcome,tt.hint_level
            FROM tutor_feedback tf JOIN tutor_turn tt ON tt.turn_id=tf.turn_id
              AND tt.child_id=tf.child_id AND tt.session_id=tf.session_id
            JOIN daily_session ds ON ds.session_id=tt.session_id AND ds.child_id=tt.child_id
            WHERE tf.feedback_id=? AND tf.child_id=? AND tt.status='completed'
              AND tt.safety_status='passed' AND tt.hint_level BETWEEN 1 AND 3
              AND tt.strategy_experiment_id IS NULL AND tt.strategy_version IS NULL""",
            (reference.get("source_ref"), child_id)).fetchone()
        if (not row or row["turn_id"] != reference.get("turn_id")
                or row["outcome"] != reference.get("outcome") or row["hint_level"] != reference.get("hint_level")):
            return None
        latest = StrategyEvolutionService._latest_feedback(connection, row["turn_id"], child_id)
        if not latest or latest["outcome"] != row["outcome"]:
            return None
        return {"source_ref": row["feedback_id"], "turn_id": row["turn_id"], "session_id": row["session_id"],
                "outcome": row["outcome"], "hint_level": row["hint_level"],
                "evidence_kind": "unscored_answer_attempt" if row["outcome"] == "answer_attempt" else "child_self_report"}

    def _capture_selection_basis(self, connection, child_id, selection):
        references = {ref for node in selection.get("learning_observations", []) for ref in node["evidence_refs"]}
        learning = []
        originals = []
        if references:
            projections = project_child_mastery_evidence(connection, child_id, as_of=utc_now())
            learning = [self._selection_learning_fact(receipt) for projection in projections.values()
                        for receipt in projection["effective_evidence"] if receipt["evidence_id"] in references]
            exact_ids = {fact['evidence_id'] for fact in learning}
            originals = [fact for fact in school_original_observations(connection, child_id, datetime.now(timezone.utc))
                         if fact['evidence_id'] in references - exact_ids]
            if exact_ids | {fact['evidence_id'] for fact in originals} != references:
                raise StrategyEvidenceInsufficientError({"selection_basis": "unavailable"})
        feedback = []
        if not references:
            for reference in selection.get("signals", {}).get("signal_refs", []):
                fact = self._selection_feedback_fact(connection, child_id, reference)
                if fact is None:
                    raise StrategyEvidenceInsufficientError({"selection_basis": "unavailable"})
                feedback.append(fact)
        return {"version": "selection_basis_v2" if originals else "selection_basis_v1",
                "learning": learning, "self_reports": feedback,
                **({'original_observations': originals} if originals else {})}

    def _refresh_selection(self, connection, experiment):
        if experiment["status"] not in {"draft", "replay_passed", "shadow", "active"}:
            return experiment
        # The creation audit is immutable history; mutable config cannot erase its sources.
        original = connection.execute("""SELECT metrics_json FROM strategy_transition_event
            WHERE experiment_id=? AND child_id=? AND from_status IS NULL AND to_status='draft'
            ORDER BY rowid LIMIT 1""", (experiment["experiment_id"], experiment["child_id"])).fetchone()
        selection = json.loads(original["metrics_json"]) if original else json.loads(experiment["candidate_config_json"]).get("selection", {})
        basis = selection.get("basis")
        refs = {ref for node in selection.get("learning_observations", []) for ref in node["evidence_refs"]}
        invalid = []
        original_facts = []
        if basis is None:
            # Legacy self-report references retain their original outcome/level.
            # Legacy learning aggregates cannot reconstruct original per-answer facts.
            if refs:
                invalid.append({"reason": "original_learning_fact_snapshot_missing", "evidence_refs": sorted(refs)})
            learning_facts = []
            feedback_facts = selection.get("signals", {}).get("signal_refs", []) if not refs else []
        else:
            learning_facts = basis.get("learning", [])
            original_facts = basis.get('original_observations', [])
            feedback_facts = basis.get("self_reports", [])
            if (basis.get("version") not in {"selection_basis_v1", "selection_basis_v2"}
                    or original_facts and basis.get('version') != 'selection_basis_v2'
                    or {r["evidence_id"] for r in learning_facts + original_facts} != refs):
                invalid.append({"reason": "selection_fact_snapshot_mismatch"})
        if original_facts:
            # A historical selection outlives its planning window, not its authority.
            current_originals = {r['evidence_id']: r for r in school_original_observations(
                connection, experiment['child_id'], datetime.now(timezone.utc),
                evidence_ids=[fact['evidence_id'] for fact in original_facts])}
            for fact in original_facts:
                if current_originals.get(fact['evidence_id']) != fact:
                    invalid.append({'reason': 'original_observation_invalid_or_changed',
                                    'evidence_ref': fact['evidence_id']})
        if learning_facts:
            # All-history qualification, not the 90-day planning window or latest node status.
            projections = project_child_mastery_evidence(connection, experiment["child_id"], as_of=utc_now())
            current = {r["evidence_id"]: self._selection_learning_fact(r)
                       for projection in projections.values() for r in projection["effective_evidence"]}
            for fact in learning_facts:
                if current.get(fact["evidence_id"]) != fact:
                    invalid.append({"reason": "learning_source_invalid_or_changed", "evidence_ref": fact["evidence_id"]})
        for fact in feedback_facts:
            current = self._selection_feedback_fact(connection, experiment["child_id"], fact)
            if current is None or (basis is not None and current != fact):
                invalid.append({"reason": "self_report_source_invalid_or_changed", "source_ref": fact.get("source_ref")})
        if not invalid:
            return experiment
        now = utc_now()
        reason = "selection_basis_invalidated: 原选择依据已失效或原事实不可核验；撤回候选，重新试用须重新评估与授权。"
        connection.execute("""UPDATE strategy_experiment SET status='rolled_back',rollback_reason=?,
            ended_at=?,updated_at=? WHERE experiment_id=?""", (reason, now, now, experiment["experiment_id"]))
        self._transition(connection, experiment["experiment_id"], experiment["child_id"], experiment["status"],
                         "rolled_back", "system", reason,
                         {"reason_code": "selection_basis_invalidated", "invalid_sources": invalid}, now)
        return self._require_experiment(connection, experiment["experiment_id"])

    def _guard_selection(self, connection, experiment):
        refreshed = self._refresh_selection(connection, experiment)
        if refreshed["status"] != experiment["status"]:
            connection.commit()
            raise InvalidStrategyTransitionError("selection_basis_invalidated")

    @staticmethod
    def _learning_scope(connection, turn) -> dict | None:
        """Bind an executed hint to an owned review or one exact validated item."""
        review = connection.execute(
            """SELECT rt.* FROM plan_item pi JOIN daily_plan dp ON dp.plan_id = pi.plan_id
            JOIN review_task rt ON rt.review_task_id = pi.source_ref AND rt.child_id = dp.child_id
            WHERE pi.plan_item_id = ? AND pi.source_type = 'due_review'
              AND dp.child_id = ? AND dp.session_id = ?""",
            (turn["plan_item_id"], turn["child_id"], turn["session_id"]),
        ).fetchone()
        if review:
            item_id = review["diagnostic_item_id"]
        else:
            rows = connection.execute(
                """SELECT di.diagnostic_item_id FROM diagnostic_item di
                JOIN knowledge_node kn ON kn.knowledge_node_id = di.knowledge_node_id
                WHERE TRIM(di.prompt) = ? AND kn.subject = ? AND di.status = 'active'""",
                (turn["question"].strip(), turn["subject"]),
            ).fetchall()
            if not rows:
                return None
            # Duplicate catalog IDs may describe one question, not ambiguous teaching.
            identities = set()
            for row in rows:
                try:
                    item = validate_item(connection, turn["child_id"], row["diagnostic_item_id"])
                except (LocalSupplyError, ValueError, TypeError):
                    return None
                identities.add((item["knowledge_node_id"], question_identity(item, connection=connection)))
            if len(identities) != 1:
                return None
            item_id = rows[0]["diagnostic_item_id"]
        try:
            item = validate_item(connection, turn["child_id"], item_id)
        except (LocalSupplyError, ValueError, TypeError):
            return None
        return {
            "knowledge_node_id": item["knowledge_node_id"], "diagnostic_item_id": item_id,
            "source_evidence_id": review["source_evidence_id"] if review else None,
        }

    def _learning_metrics(self, connection, experiment) -> dict:
        exposures = connection.execute(
            """SELECT tt.*, e.summary_json, e.occurred_at AS executed_at
            FROM strategy_evaluation_event e JOIN tutor_turn tt ON tt.turn_id = e.source_ref
              AND tt.child_id = e.child_id AND tt.strategy_experiment_id = e.experiment_id
            WHERE e.experiment_id = ? AND e.child_id = ? AND e.phase = 'active'
              AND e.event_type = 'eligible_tutor_turn' AND tt.status = 'completed'
              AND tt.safety_status = 'passed' AND tt.strategy_version = ?
            ORDER BY e.occurred_at DESC""",
            (experiment["experiment_id"], experiment["child_id"], experiment["candidate_version"]),
        ).fetchall()
        learning = self._reliable_learning(connection, experiment["child_id"]) if exposures else {"nodes": []}
        # Qualified receipts include failed and practice-only answers. Node status
        # limits independence/retention claims, not ownership or score validity.
        qualified_ids = {ref for node in learning["nodes"] for ref in node["evidence_refs"]}
        projections, identities = None, {}

        def identity(item_id):
            if item_id not in identities:
                item = connection.execute("SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?", (item_id,)).fetchone()
                try:
                    identities[item_id] = question_identity(dict(item), connection=connection) if item else None
                except (TypeError, ValueError):
                    identities[item_id] = None
            return identities[item_id]

        bound = []
        for exposure in exposures:
            receipt = json.loads(exposure["summary_json"])
            scope = receipt.get("learning_scope")
            if (receipt.get("candidate_executed") is not True
                    or receipt.get("candidate_version") != experiment["candidate_version"]
                    or receipt.get("safety_status") != "passed"
                    or not scope):
                continue
            current_scope = self._learning_scope(connection, exposure)
            try:
                scope_item = validate_item(connection, experiment["child_id"], scope["diagnostic_item_id"])
            except (LocalSupplyError, ValueError, TypeError, KeyError):
                continue
            scope_identity = question_identity(scope_item, connection=connection)
            if (not current_scope or current_scope["knowledge_node_id"] != scope.get("knowledge_node_id")
                    or current_scope["source_evidence_id"] != scope.get("source_evidence_id")
                    or identity(current_scope["diagnostic_item_id"]) != scope_identity):
                continue
            node_id = scope["knowledge_node_id"]
            if projections is None:
                projections = {node: {r["evidence_id"]: r for r in projection["effective_evidence"]}
                               for node, projection in project_child_mastery_evidence(
                                   connection, experiment["child_id"]).items()}
            # Exposure survives score invalidation/withdrawal and spans all aliases.
            history = connection.execute("""SELECT diagnostic_item_id FROM (
                SELECT diagnostic_item_id,child_id,created_at FROM diagnostic_attempt
                UNION ALL SELECT diagnostic_item_id,child_id,created_at FROM mistake_attempt
                ) WHERE child_id=? AND julianday(created_at)<=julianday(?)""",
                (experiment["child_id"], exposure["executed_at"])).fetchall()
            seen_identities = {identity(r["diagnostic_item_id"]) for r in history}
            prior_turns = connection.execute("""SELECT tt.* FROM tutor_turn tt JOIN daily_session ds
                ON ds.session_id=tt.session_id AND ds.child_id=tt.child_id WHERE tt.child_id=?
                AND tt.status='completed' AND tt.safety_status='passed' AND tt.answer IS NOT NULL AND trim(tt.answer)<>''
                AND julianday(tt.created_at)<=julianday(?)""",
                (experiment["child_id"], exposure["executed_at"])).fetchall()
            for prior in prior_turns:
                prior_scope = self._learning_scope(connection, prior)
                if prior_scope:
                    seen_identities.add(identity(prior_scope["diagnostic_item_id"]))
            rows = connection.execute(
                """SELECT le.*, da.diagnostic_item_id, da.created_at AS submitted_at,
                rt.source_evidence_id,
                julianday(le.occurred_at) - julianday(source.occurred_at) AS review_elapsed_days
                FROM learning_evidence le JOIN diagnostic_attempt da ON da.evidence_id = le.evidence_id
                LEFT JOIN review_task rt ON rt.review_task_id = da.review_task_id
                LEFT JOIN learning_evidence source ON source.evidence_id = rt.source_evidence_id
                WHERE le.child_id = ? AND le.knowledge_node_id = ?
                  AND julianday(le.occurred_at) >= julianday(?)
                  AND julianday(da.created_at) >= julianday(?)
                  AND julianday(le.occurred_at) >= julianday(?)
                  AND julianday(le.occurred_at) <= julianday(?)
                  AND julianday(da.created_at) <= julianday(?)
                ORDER BY le.occurred_at, da.rowid""",
                (experiment["child_id"], scope["knowledge_node_id"], exposure["executed_at"],
                 exposure["executed_at"], experiment["activated_at"], experiment["ended_at"] or utc_now(),
                 experiment["ended_at"] or utc_now()),
            ).fetchall()
            for row in rows:
                if row["evidence_id"] not in qualified_ids or any(
                        item["evidence_ref"] == row["evidence_id"] for item in bound):
                    continue
                projected = projections.get(node_id, {}).get(row["evidence_id"])
                item_identity = projected.get("question_identity") if projected else None
                if not item_identity or item_identity != identity(row["diagnostic_item_id"]):
                    continue
                new_item = (row["session_id"] == exposure["session_id"]
                            and item_identity != scope_identity
                            and item_identity not in seen_identities)
                delayed = False
                if row["review_elapsed_days"] is not None:
                    source_linked = row["source_evidence_id"] == scope["source_evidence_id"]
                    if not source_linked:
                        source = connection.execute(
                            """SELECT diagnostic_item_id FROM diagnostic_attempt WHERE evidence_id = ? AND child_id = ?
                            AND session_id = ?
                            AND julianday(created_at) >= julianday(?)""",
                            (row["source_evidence_id"], experiment["child_id"], exposure["session_id"],
                             exposure["executed_at"]),
                        ).fetchone()
                        source_linked = bool(source and identity(source["diagnostic_item_id"]) == scope_identity)
                    delayed = source_linked and row["review_elapsed_days"] >= 1
                if not new_item and not delayed:
                    continue
                bound.append({
                    "evidence_ref": row["evidence_id"], "turn_id": exposure["turn_id"],
                    "experiment_id": experiment["experiment_id"], "strategy_version": experiment["candidate_version"],
                    "check_kind": "delayed_check" if delayed else "new_item_check",
                    "question_identity": item_identity, "exposure_question_identity": scope_identity,
                    "source_evidence_ref": row["source_evidence_id"] if delayed else None,
                    "correctness": row["correctness"], "independent": None,
                    "independence_status": "not_inferred_from_raw_receipt",
                    "retention_claim_allowed": False,
                    "evidence_kind": "qualified_diagnostic_answer", "occurred_at": row["occurred_at"],
                })
        return {
            "status": "learning_check_failed" if any(item["correctness"] < 1 for item in bound)
                      else "observed_checks_not_causal_gain" if bound else "insufficient_learning_evidence",
            "checks": bound, "qualified_check_count": len(bound),
            "failed_check_count": sum(item["correctness"] < 1 for item in bound),
            "binding_rule": "executed_version_same_question_identity_or_owned_review_then_related_check",
            "learning_gain": None, "mastery_claim_allowed": False,
        }

    def _refresh_learning(self, connection, experiment):
        experiment = self._refresh_selection(connection, experiment)
        if experiment["status"] != "active":
            return experiment
        learning = self._learning_metrics(connection, experiment)
        metrics = {**self._phase_metrics(connection, experiment["experiment_id"], "active"),
                   "learning_evidence": learning}
        now = utc_now()
        if metrics != json.loads(experiment["active_metrics_json"] or "{}"):
            connection.execute(
                "UPDATE strategy_experiment SET active_metrics_json = ?, updated_at = ? WHERE experiment_id = ?",
                (json.dumps(metrics, ensure_ascii=False), now, experiment["experiment_id"]),
            )
        if learning["failed_check_count"]:
            reason = "实际执行策略后，绑定的新题或延迟检查出现合格作答失败；保守降级为基线，不宣称策略因果效果。"
            connection.execute(
                """UPDATE strategy_experiment SET status = 'rolled_back', rollback_reason = ?, ended_at = ?
                WHERE experiment_id = ? AND status = 'active'""",
                (reason, now, experiment["experiment_id"]),
            )
            self._transition(connection, experiment["experiment_id"], experiment["child_id"],
                             "active", "rolled_back", "system", reason, metrics, now)
        return self._require_experiment(connection, experiment["experiment_id"])

    def _result(self, connection, row) -> dict:
        result = dict(row)
        result["candidate_config"] = json.loads(result.pop("candidate_config_json"))
        for key in ("replay_metrics_json", "shadow_metrics_json", "active_metrics_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key) or "{}")
            # Old JSON remains readable, but cannot acquire an effectiveness claim.
            result[key.removesuffix("_json")].update(_evidence_contract(key.split("_")[0]))
        for phase in ("shadow", "active"):
            result[f"{phase}_metrics"] = self._phase_metrics(connection, result["experiment_id"], phase)
        result["active_metrics"]["learning_evidence"] = self._learning_metrics(connection, row)
        result["phase_metrics"] = result["active_metrics" if result["activated_at"] else "shadow_metrics"]
        result["effectiveness_status"] = "unverified"
        result["selection_basis_invalidated"] = (row["rollback_reason"] or "").startswith("selection_basis_invalidated:")
        result["activation_semantics"] = "parent_authorized_trial_not_effectiveness_approval"
        result["frozen_boundaries"] = ["提示等级与答案边界", "教育安全规则", "隐私上传规则", "家庭时长与家长约束"]
        transitions = connection.execute(
            """
            SELECT from_status, to_status, actor, reason, metrics_json, occurred_at
            FROM strategy_transition_event WHERE experiment_id = ? ORDER BY occurred_at
            """,
            (result["experiment_id"],),
        ).fetchall()
        result["transitions"] = [
            {
                **{key: item[key] for key in ("from_status", "to_status", "actor", "reason", "occurred_at")},
                "metrics": json.loads(item["metrics_json"] or "{}"),
            }
            for item in transitions
        ]
        try:
            from .bounded_experiment_protocol import protocol_state
            result["bounded_protocol"] = protocol_state(
                connection, source_kind="strategy_experiment",
                source_id=result["experiment_id"],
            )
        except KeyError:
            result["bounded_protocol"] = None
        return result

    @staticmethod
    def _config_is_low_risk(config: dict) -> bool:
        if not isinstance(config, dict):
            return False
        bounded = {key: value for key, value in config.items() if key != "selection"}
        return any(
            json.dumps(bounded, sort_keys=True) == json.dumps(allowed, sort_keys=True)
            for allowed, _ in CANDIDATES.values()
        )

    @staticmethod
    def _experiment_is_low_risk(experiment) -> bool:
        try:
            config = json.loads(experiment["candidate_config_json"])
            allowed = CANDIDATES.get(experiment["candidate_version"])
            return bool(allowed and StrategyEvolutionService._config_is_low_risk(config)
                        and {key: value for key, value in config.items() if key != "selection"} == allowed[0])
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _require_child(connection, child_id: str) -> None:
        if not connection.execute("SELECT 1 FROM child_profile WHERE child_id = ?", (child_id,)).fetchone():
            raise UnknownChildError(child_id)

    @staticmethod
    def _require_experiment(connection, experiment_id: str):
        row = connection.execute(
            "SELECT * FROM strategy_experiment WHERE experiment_id = ?", (experiment_id,)
        ).fetchone()
        if not row:
            raise UnknownStrategyExperimentError(experiment_id)
        return row

    @staticmethod
    def _evaluation_event(connection, experiment_id, child_id, phase, event_type, value, source_ref, summary, now):
        connection.execute(
            """
            INSERT INTO strategy_evaluation_event(
              event_id, experiment_id, child_id, phase, event_type, value,
              source_ref, summary_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(experiment_id, phase, event_type, source_ref) DO UPDATE SET
              value = excluded.value, summary_json = excluded.summary_json,
              occurred_at = excluded.occurred_at
            WHERE excluded.event_type = 'tutor_understood'
            """,
            (
                str(uuid4()), experiment_id, child_id, phase, event_type, value,
                source_ref, json.dumps(summary, ensure_ascii=False), now,
            ),
        )

    @staticmethod
    def _transition(connection, experiment_id, child_id, previous, target, actor, reason, metrics, now):
        connection.execute(
            """
            INSERT INTO strategy_transition_event(
              transition_id, experiment_id, child_id, from_status, to_status,
              actor, reason, metrics_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()), experiment_id, child_id, previous, target, actor,
                reason, json.dumps(metrics, ensure_ascii=False), now,
            ),
        )
        from .bounded_experiment_protocol import ensure_protocol, transition_protocol
        experiment = connection.execute(
            "SELECT * FROM strategy_experiment WHERE experiment_id=? AND child_id=?",
            (experiment_id, child_id),
        ).fetchone()
        replay = json.loads(experiment["replay_metrics_json"] or "{}")
        learning = metrics.get("learning_evidence") if isinstance(metrics, dict) else None
        sample_size = max(
            int((metrics or {}).get("self_report_count") or 0),
            int((metrics or {}).get("eligible_turns") or 0),
            int((metrics or {}).get("tutor_feedback_count") or 0),
            int((learning or {}).get("qualified_check_count") or 0),
        )
        protocol_metrics = {
            "sample_size": sample_size,
            "prompt_count": int((metrics or {}).get("eligible_turns") or 0),
            "refusal_count": int((metrics or {}).get("policy_replacements") or 0),
            "burden_events": int((metrics or {}).get("policy_replacements") or 0),
            "source_metrics": {
                **(metrics or {}),
                "baseline_score": replay.get("tutor_understood_rate"),
                "current_score": (metrics or {}).get("understood_rate"),
            },
        }
        mapped_target = "stopped" if target == "rejected" else target
        if previous is None:
            ensure_protocol(
                connection, child_id=child_id, domain="teaching_strategy",
                source_kind="strategy_experiment", source_id=experiment_id,
                primary_variable=experiment["strategy_key"],
                baseline_version=experiment["baseline_version"],
                candidate_version=experiment["candidate_version"], status=mapped_target,
                metrics=protocol_metrics, now=now, reason=reason, actor=actor,
            )
        else:
            transition_protocol(
                connection, source_kind="strategy_experiment", source_id=experiment_id,
                target=mapped_target, metrics=protocol_metrics, now=now,
                reason=reason, actor=actor,
            )


class UnknownStrategyExperimentError(Exception):
    pass


class InvalidStrategyTransitionError(Exception):
    pass


class StrategyEvidenceInsufficientError(Exception):
    def __init__(self, metrics: dict):
        super().__init__("现有证据不足，策略保持原状态。")
        self.metrics = metrics
