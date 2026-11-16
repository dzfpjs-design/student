"""Bounded application-owned delivery of verified local exercise templates."""
from __future__ import annotations

from .local_supply import LocalSupplyError


def refresh_local_question_supply(database, specialized, variants, *, should_stop=None) -> dict:
    """Never approves LLM content, never creates attempts or mastery evidence."""
    report = {"local_only": True, "prepared": [], "qualified": 0, "blocked": [], "review_bindings": 0}

    def stopped():
        if should_stop is not None and should_stop():
            report["stopped"] = True
            return True
        return False

    stop_options = {} if should_stop is None else {"should_stop": should_stop}
    if stopped():
        return report
    with database.connect() as connection:
        children = [dict(row) for row in connection.execute("SELECT child_id,grade FROM child_profile WHERE status='active'")]
    for child in children:
        tracks = ("pet", "competition_math") if child["grade"].startswith("四年级") else ("junior_exam",)
        for track in tracks:
            if stopped():
                return report
            try:
                result = specialized.enqueue_local_gaps(child["child_id"], track, f"local-content-delivery-v1:{child['child_id']}:{track}", limit=50, **stop_options)
                report["prepared"].append({"child_id": child["child_id"], "track": track, **result})
            except LocalSupplyError as error:
                report["blocked"].append({"track": track, "reason": str(error)})
    if stopped():
        return report
    specialized.process_local_pending(50, **stop_options)
    if stopped():
        return report
    variants.process_local_pending(20, **stop_options)
    if stopped():
        return report
    for child in children:
        for service, table in ((specialized, "specialized_supply_candidate"), (variants, "variant_candidate")):
            if stopped():
                return report
            with database.connect() as connection:
                candidates = connection.execute(f"SELECT candidate_id,validation_json FROM {table} WHERE child_id=? AND status='needs_review'", (child["child_id"],)).fetchall()
            import json
            for row in candidates:
                if stopped():
                    return report
                if json.loads(row["validation_json"]).get("ruleset") != "local_deterministic_v1": continue
                try:
                    service.qualify_local(row["candidate_id"], child["child_id"], f"verified-local-policy-v1:{row['candidate_id']}")
                    report["qualified"] += 1
                except LocalSupplyError as error:
                    report["blocked"].append({"candidate_id": row["candidate_id"], "reason": str(error)})
            if stopped():
                return report
            report["review_bindings"] += len(service.bind_due_reviews(child["child_id"])["bindings"])
    return report
