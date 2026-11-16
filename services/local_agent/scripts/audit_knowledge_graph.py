from __future__ import annotations
import argparse, json
from pathlib import Path
from home_study_agent.database import Database
from home_study_agent.knowledge_graph.quality import GraphQualityService
parser=argparse.ArgumentParser();parser.add_argument('--app-db',type=Path,required=True);parser.add_argument('--queue-size',type=int,default=8)
args=parser.parse_args();service=GraphQualityService(Database(args.app_db));print(json.dumps({'report':service.report(),'review_queue':service.review_queue(args.queue_size)},ensure_ascii=False))
