from __future__ import annotations

import argparse
from pathlib import Path
from home_study_agent.database import Database
from home_study_agent.knowledge_graph.standard_projection import StandardEvidenceProjector

parser=argparse.ArgumentParser(); parser.add_argument("--app-db",type=Path,required=True)
args=parser.parse_args(); database=Database(args.app_db); database.initialize(); print(StandardEvidenceProjector(database).run())
