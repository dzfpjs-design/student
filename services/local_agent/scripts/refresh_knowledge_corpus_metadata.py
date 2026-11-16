"""Repair local corpus grade/subject/edition/unit metadata in place."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_ingestion import CorpusSpec,LocalCorpusIngestor


def main()->None:
    parser=argparse.ArgumentParser()
    parser.add_argument('--app-db',type=Path,required=True)
    parser.add_argument('--corpus-id',required=True)
    parser.add_argument('--title',required=True)
    parser.add_argument('--source-kind',choices=('seyk','bilibili'),required=True)
    parser.add_argument('--transcripts',type=Path,required=True)
    parser.add_argument('--videos',type=Path)
    parser.add_argument('--metadata-db',type=Path,required=True)
    args=parser.parse_args()
    database=Database(args.app_db);database.initialize()
    spec=CorpusSpec(args.corpus_id,args.title,args.source_kind,args.transcripts,args.videos,
                    f'local:{args.source_kind}','local_only_rights_not_verified',args.metadata_db)
    print(json.dumps(LocalCorpusIngestor(database).refresh_metadata(spec),ensure_ascii=False))


if __name__=='__main__':main()
