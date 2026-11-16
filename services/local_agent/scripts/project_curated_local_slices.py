from __future__ import annotations
import argparse
from pathlib import Path
from home_study_agent.database import Database
from home_study_agent.knowledge_graph.curated_slices import CuratedLocalSlices
parser=argparse.ArgumentParser();parser.add_argument('--app-db',type=Path,required=True)
args=parser.parse_args(); database=Database(args.app_db);database.initialize(); projector=CuratedLocalSlices(database);print({'p4':projector.p4_large_number_rewrite(),'j3':projector.j3_quadratic_formula_retest()})
