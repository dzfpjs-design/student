from home_study_agent.install_setup import installation_setup_status


class Auth:
    def __init__(self, configured=True): self.configured = configured
    def status(self): return {"configured": self.configured, "locked_seconds": 0}


class Repository:
    def __init__(self, children): self.children = children
    def list_children(self): return self.children


class Delivery:
    def __init__(self, configured=True, recipients=None, tests=None):
        self.configured = configured; self.recipients = recipients or {}; self.tests = tests or {}
    def channel_status(self): return [{"channel": "email", "configured": self.configured}]
    def list_recipients(self, child_id): return self.recipients.get(child_id, [])
    def channel_test_status(self, child_id): return self.tests.get(child_id, [])


def test_install_setup_requires_pin_smtp_recipient_and_real_test_for_existing_children():
    repository = Repository([{"child_id": "child-1", "status": "active"}])
    incomplete = installation_setup_status(Auth(False), repository, Delivery(False))
    assert incomplete["complete"] is False
    assert incomplete["next_step"] == "parent_pin"

    recipients = {"child-1": [{
        "channel": "email", "consent_status": "consented", "revoked_at": None,
    }]}
    tests = {"child-1": [{"channel": "email", "status": "passed"}]}
    complete = installation_setup_status(Auth(), repository, Delivery(True, recipients, tests))
    assert complete["complete"] is True
    assert complete["next_step"] == "complete"


def test_install_setup_exposes_counts_not_addresses_or_credentials():
    status = installation_setup_status(
        Auth(), Repository([{"child_id": "child-1", "status": "active"}]), Delivery(True),
    )
    assert status["children_missing_email_recipient"] == 1
    assert status["children_missing_email_test"] == 1
    assert "address" not in str(status).lower()
    assert "password" not in str(status).lower()


def test_real_existing_family_evidence_requires_a_saved_session(tmp_path):
    from home_study_agent.database import Database
    from home_study_agent.repository import StudyRepository
    db = Database(tmp_path / 'family.db'); db.initialize()
    repo = StudyRepository(db); repo.seed_family()
    assert installation_setup_status(Auth(), repo, Delivery(False))['existing_family'] is False
    child = repo.list_children()[0]['child_id']
    repo.start_session(child, 'synthetic-setup-history')
    assert installation_setup_status(Auth(), StudyRepository(Database(db.path)), Delivery(False))['existing_family'] is True
    assert installation_setup_status(Auth(False), repo, Delivery(False))['complete'] is False
