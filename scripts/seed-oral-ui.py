"""Seed only synthetic standards and catalog, never the authored oral source."""
import json
import sys
from pathlib import Path
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.learning_state import LearningStateService
from test_pet_typed_source_import import add_standard

path = Path(sys.argv[1]).resolve()
assert str(path).startswith('/private/tmp/hsc-c03-dev-20260909/oral/') and not path.exists()
db = Database(path)
db.initialize()
StudyRepository(db).seed_family()
LearningStateService(db).seed_catalog()
add_standard(db)
with db.connect() as c:
    assert c.execute('SELECT COUNT(*) FROM knowledge_source').fetchone()[0] == 0
print(json.dumps(dict(synthetic_only=True, authored_source_count=0, database=str(path))))
