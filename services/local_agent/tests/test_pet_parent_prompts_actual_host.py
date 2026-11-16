"""Real create_app routes from a fresh temporary household, without media/models."""

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database


CHILD = "child-primary-4"
LIST = f"/v1/parent/children/{CHILD}/pet-prompts"


def dump(database):
    with database.connect() as connection:
        return list(connection.iterdump())


def setup(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "fresh.db",
                        maintenance_enabled=False, session_token="isolated-parent-pet-host")
    return settings, {"X-HSC-Session": settings.session_token}


def prepare(client, local):
    response = client.post("/v1/sessions", headers=local,
        json={"child_id": CHILD, "client_action_id": "parent-pet-session"})
    assert response.status_code == 201, response.text
    sid = response.json()["session_id"]
    response = client.post(f"/v1/sessions/{sid}/diagnostic-prompt/prepare", headers=local,
        json={"track": "pet", "preferred_item_id": "diag-pet-listening-1",
              "client_action_id": "parent-pet-original-prompt"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "waiting_answer"
    return sid, response.json()["prompt"]


def test_fresh_actual_host_parent_list_matches_panel_and_recovers_original_after_restart(tmp_path):
    settings, local = setup(tmp_path)
    database = Database(settings.database_path)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.post("/v1/parent/setup", headers=local, json={"pin": "2468"})
        assert response.status_code == 200, response.text
        parent = {**local, "X-HSC-Parent": response.json()["parent_token"]}
        assert client.get(LIST).status_code == 401
        assert client.get(LIST, headers=local).status_code == 401
        assert client.get(LIST, headers={"X-HSC-Parent": parent["X-HSC-Parent"]}).status_code == 401
        before = dump(database)
        assert client.get(LIST, headers=parent).json() == {
            "child_id": CHILD, "prompts": [], "local_only": True, "cloud_transmission_allowed": False}
        assert dump(database) == before
        sid, original = prepare(client, local)
        before = dump(database)
        response = client.get(LIST, headers=parent)
        assert response.status_code == 200, response.text
        data = response.json()
        row, = data["prompts"]
        assert data["child_id"] == row["child_id"] == CHILD
        assert data["local_only"] is True and data["cloud_transmission_allowed"] is False
        assert row["prompt_id"] == original["prompt_id"]
        assert row["item_version"] == original["item_version"]
        assert row["origin_session_id"] == sid and row["status"] == "waiting_answer"
        assert set(row) == {"child_id", "origin_session_id", "prompt_id", "item_version",
                            "diagnostic_item_id", "created_at", "status", "item"}
        assert row["item"] == {"track": 'pet', "prompt": original["item"]["prompt"],
            "stimulus_text": original["item"]["stimulus_text"], "pet_audio_modality": "listening"}
        assert response.headers["cache-control"] == "no-store"
        # This is the exact follow-up GET performed when the panel selects a row.
        collection = client.get(f"/v1/sessions/{sid}/pet-media/prompt/{row['prompt_id']}",
            headers=parent, params={"item_version": row["item_version"]})
        assert collection.status_code == 200, collection.text
        assert collection.json()["prompt_id"] == row["prompt_id"]
        assert collection.json()["materials"] == collection.json()["listening"] == []
        assert dump(database) == before
        assert client.get("/v1/parent/children/child-junior-3/pet-prompts", headers=parent).json()["prompts"] == []
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        assert client.get(LIST, headers=parent).status_code == 401
        response = client.post("/v1/parent/unlock", headers=local, json={"pin": "2468"})
        assert response.status_code == 200, response.text
        parent = {**local, "X-HSC-Parent": response.json()["parent_token"]}
        before = dump(database)
        assert client.get(LIST, headers=parent).json() == data
        assert dump(database) == before


def test_actual_host_source_revision_is_visible_without_rewriting_snapshot(tmp_path):
    settings, local = setup(tmp_path)
    database = Database(settings.database_path)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.post("/v1/parent/setup", headers=local, json={"pin": "2468"})
        assert response.status_code == 200, response.text
        parent = {**local, "X-HSC-Parent": response.json()["parent_token"]}
        sid, original = prepare(client, local)
        with database.connect() as connection:
            connection.execute("UPDATE diagnostic_item SET prompt=prompt||' revised' WHERE diagnostic_item_id=?",
                               (original["diagnostic_item_id"],))
            connection.commit()
        before = dump(database)
        response = client.get(LIST, headers=parent)
        assert response.status_code == 200, response.text
        row, = response.json()["prompts"]
        assert row["status"] == "invalidated" and row["item_version"] == original["item_version"]
        assert row["item"]["prompt"] == original["item"]["prompt"]
        assert dump(database) == before
        with database.connect() as connection:
            assert connection.execute("SELECT status FROM diagnostic_prompt WHERE prompt_id=?",
                (row["prompt_id"],)).fetchone()[0] == "waiting_answer"
            assert connection.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 0
