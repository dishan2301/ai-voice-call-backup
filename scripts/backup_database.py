#!/usr/bin/env python3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.database_backup import backup_database


if __name__ == "__main__":
    backup = backup_database()
    print(f"Verified database backup: {backup.name}")
