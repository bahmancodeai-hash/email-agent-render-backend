import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.services.dedupe_service import run_message_dedupe  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove duplicate synced email rows.")
    parser.add_argument("--apply", action="store_true", help="Delete duplicate rows. Without this flag only reports counts.")
    parser.add_argument("--max-delete", type=int, default=None, help="Maximum rows to delete in this run.")
    args = parser.parse_args()

    url = settings.database_url_sync or settings.database_url.replace("+asyncpg", "")
    engine = create_engine(url, pool_pre_ping=True)
    with engine.begin() as conn:
        db = Session(bind=conn)
        print(run_message_dedupe(db, apply=args.apply, max_delete=args.max_delete))


if __name__ == "__main__":
    main()
