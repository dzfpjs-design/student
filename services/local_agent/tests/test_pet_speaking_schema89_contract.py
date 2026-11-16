"""Actual source validator/Database initialization; no SQL capability imitation."""
from pathlib import Path
import json,pytest
from home_study_agent.database import Database,LATEST_SCHEMA_VERSION
from home_study_agent import parent_restore_assets as assets
from home_study_agent.pet_speaking_candidate_store import TABLES,ensure_schema,SCHEMA_OBJECTS

def drop_empty_schema92_receipts(c):
 from home_study_agent.pet_multipa_execution_receipt import OBJECTS,TABLE,EVENT
 for table in(TABLE,EVENT):assert c.execute('SELECT COUNT(*)FROM '+table).fetchone()[0]==0
 for kind,name,_ in reversed(OBJECTS):c.execute('DROP '+kind.upper()+' '+name)

def current_schema(c):return [tuple(r)for r in c.execute("SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'pet_speaking_%' ORDER BY type,name")]

@pytest.mark.parametrize('fault',['missing_table','renamed_column','missing_trigger','changed_trigger','missing_index','foreign_key'])
def test_declared89_missing_or_changed_contract_never_repaired(tmp_path,fault):
 db=Database(tmp_path/'schema89.db');db.initialize()
 with db.connect()as c:
  assert LATEST_SCHEMA_VERSION==95
  # Reconstruct the actual schema89 predecessor: its two original immutable
  # candidate tables exist, while the later empty durable-job table does not.
  drop_empty_schema92_receipts(c)
  assert c.execute('SELECT count(*)FROM pet_speaking_candidate_job').fetchone()[0]==0
  c.execute('DROP TABLE pet_speaking_candidate_job')
  c.execute('DELETE FROM schema_migration WHERE version>=90');c.commit()
  assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==89
  assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='pet_speaking_candidate_job'").fetchone()
  assert not c.execute('PRAGMA foreign_key_check').fetchall()
  assets.validate_schema(c);ensure_schema(c)
  if fault=='missing_table':c.execute('DROP TABLE '+TABLES[1])
  elif fault=='renamed_column':c.execute('ALTER TABLE '+TABLES[1]+' RENAME COLUMN request_hash TO request_digest')
  elif fault=='missing_trigger':c.execute('DROP TRIGGER pet_speaking_model_candidate_no_update')
  elif fault=='changed_trigger':c.execute('DROP TRIGGER pet_speaking_model_candidate_no_update');c.execute('CREATE TRIGGER pet_speaking_model_candidate_no_update BEFORE UPDATE ON pet_speaking_model_candidate BEGIN SELECT 1;END')
  elif fault=='missing_index':c.execute('DROP INDEX pet_speaking_candidate_child')
  else:
   c.execute('DROP TABLE '+TABLES[1]);sql=next(sql for kind,name,sql in SCHEMA_OBJECTS if name==TABLES[1]);c.execute(sql.replace('ON DELETE CASCADE','ON DELETE RESTRICT',1))
  c.commit();before=current_schema(c);assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==89
  with pytest.raises(Exception,match='RESTORE_ASSET_OWNERSHIP_UNCONFIRMED'):assets.validate_schema(c)
  with pytest.raises(Exception,match='RESTORE_ASSET_OWNERSHIP_UNCONFIRMED'):ensure_schema(c)
 with pytest.raises(Exception,match='RESTORE_ASSET_OWNERSHIP_UNCONFIRMED'):db.initialize()
 with db.connect()as c:assert current_schema(c)==before

def test_original88_missing_new_tables_keeps_normal_upgrade(tmp_path):
 db=Database(tmp_path/'schema88.db');db.initialize()
 with db.connect()as c:
  assert LATEST_SCHEMA_VERSION==95
  drop_empty_schema92_receipts(c)
  assert c.execute('SELECT count(*)FROM pet_speaking_candidate_job').fetchone()[0]==0
  c.execute('DROP TABLE pet_speaking_candidate_job')
  for table in reversed(TABLES):
   assert c.execute('SELECT count(*)FROM '+table).fetchone()[0]==0
   c.execute('DROP TABLE '+table)
  c.execute('DELETE FROM schema_migration WHERE version>=89');c.commit()
  assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==88
  assert not current_schema(c)
  assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1
  assert not c.execute('PRAGMA foreign_key_check').fetchall()
  assets.validate_schema(c)
 db.initialize()
 with db.connect()as c:
  assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==LATEST_SCHEMA_VERSION
  assets.validate_schema(c)
  assert {r[1]for r in current_schema(c)if r[0]=='table'}==set(TABLES)|{'pet_speaking_candidate_job'}
  assert not c.execute('PRAGMA foreign_key_check').fetchall()
 upgrade=db.latest_upgrade_status()
 assert upgrade['status']=='completed'and upgrade['snapshot_available']
 assert(upgrade['source_schema_version'],upgrade['target_schema_version'])==(88,LATEST_SCHEMA_VERSION)
