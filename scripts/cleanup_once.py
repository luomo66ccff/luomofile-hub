import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import ensure_dirs
from app.db import init_db
from app.cleanup import cleanup_once


if __name__ == "__main__":
    ensure_dirs()
    init_db()
    print(cleanup_once())
