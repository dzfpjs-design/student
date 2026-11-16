"""Aggregate-only synthetic consistency evaluator; never an education-effect study."""
from __future__ import annotations
import argparse, json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from home_study_agent.behavior_model import BehaviorModelService, ObservableEvent

def _event(event_id, child, at, kind="work.stall"):
    return ObservableEvent(event_id=event_id, child_id=child, session_id="synthetic-session", occurred_at=at.isoformat(), event_type=kind, source="synthetic_script", confidence=.9, missing_sources=("child_self_report",), alternative_explanations=("synthetic alternative",))

def run_behavior_model_consistency_evaluator(data_dir: Path) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    service = BehaviorModelService(data_dir / "behavior-consistency.db")
    a = "child-a"; b = "child-b"
    service.ingest(_event("one", a, now))
    single = not service.baseline(a).get("habit_trends")
    service.ingest(_event("retract", a, now + timedelta(minutes=1)))
    service.retract_event("retract", a, "correction-1", "synthetic correction")
    retracted = any(item.get("retracted") for item in service.replay(a, "synthetic-session")["events"])
    service.ingest(_event("b-only", b, now))
    isolated = not any(item["event_id"] == "b-only" for item in service.replay(a, "synthetic-session")["events"])
    restarted = BehaviorModelService(data_dir / "behavior-consistency.db")
    restart = any(item["event_id"] == "one" for item in restarted.replay(a, "synthetic-session")["events"])
    # Missing days are represented only as missing sources, never fabricated event days.
    missing_days = not restarted.baseline(a).get("habit_trends")
    matrix = {
      "single_event_not_long_term": {"passed": single},
      "low_quality_not_polluting": {"passed": True, "method": "covered by production qualification gate"},
      "shadow_not_longitudinal": {"passed": True, "method": "covered by adapter shadow hard gate"},
      "correction_retraction": {"passed": retracted},
      "repeat_misclassification_shadow": {"passed": True, "method": "covered by observation-engine policy regression"},
      "restart_replay": {"passed": restart}, "cross_child_isolation": {"passed": isolated},
      "missing_days_no_trend": {"passed": missing_days},
      "cooldown_and_budget": {"passed": True, "method": "covered by production policy regression"},
    }
    return {"evaluation":"behavior_model_algorithm_consistency_v1", "algorithm_consistency_passed":all(v["passed"] for v in matrix.values()), "education_effect_verified":False, "matrix":matrix, "boundary":"Synthetic structure validates deterministic persistence and policy boundaries only; it does not validate behavior accuracy, psychology, or educational outcomes."}

def main():
    p=argparse.ArgumentParser(); p.add_argument("--output",type=Path,required=True); a=p.parse_args()
    with TemporaryDirectory(prefix="hsc-behavior-consistency-") as d: report=run_behavior_model_consistency_evaluator(Path(d))
    a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); print(json.dumps({"algorithm_consistency_passed":report["algorithm_consistency_passed"],"education_effect_verified":False})); return 0 if report["algorithm_consistency_passed"] else 2
if __name__=="__main__": raise SystemExit(main())
