import json

from test_curriculum_supply_http import BASE, flow, post


def test_options_are_real_owned_sources_and_drive_whole_http_workflow(flow):
    db, _, client, _ = flow
    before = None
    with db.connect() as c:
        before = c.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0]
    options = client.get(BASE+"/options")
    assert options.status_code == 200
    values = options.json()
    assert values["cloud_transmission_allowed"] is False
    assert len(values["sources"]) == 1
    source = values["sources"][0]
    assert source["available"] is True
    details = client.get(BASE+f"/sources/{source['source_id']}/options").json()
    excerpt, standard, seed = details["excerpts"][0], details["standards"][0], details["seeds"][0]
    assert standard["available"] is True and standard["quote"]
    assert seed["stages"] == ["parallel"]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0] == before
    selection = post(client, "/select", {"action_id": "ui-scope", "expected_version": 1, "scope": {
        "grade": values["grade"], "subject": source["subject"], "edition": source["edition"],
        "semester": values["semesters"][0], "standard_scope": standard["stage_scope"],
        "textbook_source_id": source["source_id"]}})
    generated = post(client, "/generate", {"action_id": "ui-draft", "expected_version": 0,
        "expected_scope_version": selection["version"], "source": {
            "excerpt_id": excerpt["excerpt_id"], "seed_item_id": seed["seed_item_id"], "stage": "parallel",
            "requirement_id": standard["requirement_id"], "standard_page": standard["page_number"],
            "standard_quote": standard["quote"], "validator": seed["validator"],
            "difficulty": 1, "explanation": "核对原题后计算括号，再乘125。"}})
    cid = generated["candidate_id"]
    assert generated["payload"]["prompt"] == excerpt["text"]
    post(client, f"/{cid}/validate", {"action_id": "ui-validate", "expected_version": 1})
    post(client, f"/{cid}/review", {"action_id": "ui-review", "expected_version": 2, "decision": "approve", "note": "已核对"})
    assert post(client, f"/{cid}/publish", {"action_id": "ui-publish", "expected_version": 3})["eligible"] is True
    assert post(client, f"/{cid}/withdraw", {"action_id": "ui-withdraw", "expected_version": 4, "note": "撤回"})["eligible"] is False


def test_options_keep_insufficient_material_visible_without_certifying(flow):
    db, _, client, _ = flow
    with db.connect() as c:
        c.execute("UPDATE knowledge_source SET review_status='quarantined'")
        c.execute("UPDATE curriculum_standard_requirement SET maturity='model_drafted'")
        c.commit()
    source = client.get(BASE+"/options").json()["sources"][0]
    assert source["available"] is False and source["blockers"]
    response = client.get(BASE+f"/sources/{source['source_id']}/options")
    assert response.status_code == 200
    assert response.json()["standards"][0]["available"] is False
    assert response.json()["local_only"] is True
    assert "local_root_path" not in response.text


def test_options_never_expose_other_child_or_bypass_parent_auth(flow):
    _, _, client, _ = flow
    source = client.get(BASE+"/options").json()["sources"][0]
    other = BASE.replace("child-primary-4", "child-junior-3")
    assert client.get(other+"/options").json()["sources"] == []
    assert client.get(other+f"/sources/{source['source_id']}/options").status_code == 404
    assert client.get(BASE+"/options", headers={"X-HSC-Parent": "invalid"}).status_code == 401


def test_options_do_not_offer_grade_four_seeds_for_grade_nine_material(flow):
    db, _, client, _ = flow
    with db.connect() as c:
        c.execute("UPDATE child_profile SET grade='初三' WHERE child_id='child-primary-4'")
        c.execute("UPDATE knowledge_source SET grade_band='初三'")
        c.commit()
    source = client.get(BASE+"/options").json()["sources"][0]
    response = client.get(BASE+f"/sources/{source['source_id']}/options")
    assert response.status_code == 200
    assert response.json()["seeds"] == []
