"""Synthetic component projections from the pinned empty historical v84 schema.

No current database or future authority objects are removed. Callers explicitly
project their own old component; this is not whole-version migration evidence.
"""
from historical_database_fixture import seed_empty_historical_v84
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.learning_state import LearningStateService


def family(path, child_id, action):
    database = Database(path)
    seed_empty_historical_v84(database.path)
    repository = StudyRepository(database)
    repository.seed_family()
    LearningStateService(database).seed_catalog()
    session = repository.start_session(child_id, action)['session_id']
    return database, repository, session
