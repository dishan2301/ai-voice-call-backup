#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.dependencies import create_generation_service
from app.database.db import create_tables


if __name__ == "__main__":
    create_tables()
    briefing = create_generation_service().generate()
    print(briefing.detailed_brief)
    print("\n--- Spoken brief ---\n")
    print(briefing.spoken_brief)
