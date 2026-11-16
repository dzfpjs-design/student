import argparse,json
from pathlib import Path
from home_study_agent.database import Database
from home_study_agent.knowledge_graph.audit_pack import MappingAuditPackService
p=argparse.ArgumentParser();p.add_argument('--app-db',type=Path,required=True);p.add_argument('--per-stratum',type=int,default=5);p.add_argument('--output',type=Path);a=p.parse_args();r=MappingAuditPackService(Database(a.app_db)).create(per_stratum=a.per_stratum)
if a.output:a.output.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(r,ensure_ascii=False))
