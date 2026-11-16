from __future__ import annotations

import argparse
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.catalog_bootstrap import CatalogGraphBootstrapper


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-db", type=Path, required=True)
    arguments = parser.parse_args()
    database = Database(arguments.app_db); database.initialize()
    print(CatalogGraphBootstrapper(database).run())


if __name__ == "__main__":
    main()
