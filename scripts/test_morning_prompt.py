#!/usr/bin/env python3
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.dependencies import create_generation_service
from app.database.db import create_tables
from app.desktop.morning_prompt import DesktopPrompt, PromptDecision
from app.services.morning_job import MorningJob
from app.services.speech_service import SpeechService, get_tts_provider


class ScriptedPrompt:
    def __init__(self, decision: PromptDecision):
        self.decision = decision

    def ask(self) -> PromptDecision:
        return self.decision


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the morning workflow without waiting for 09:30")
    parser.add_argument("--decision", choices=["yes", "no", "remind"], help="simulate a click instead of opening zenity")
    args = parser.parse_args()
    prompt = ScriptedPrompt(PromptDecision(args.decision.upper())) if args.decision else DesktopPrompt()
    create_tables()
    job = MorningJob(
        create_generation_service(),
        prompt,
        SpeechService(get_tts_provider()),
    )
    decision = job.run(datetime.now(timezone.utc))
    print(f"Morning workflow completed with decision: {decision.value}")


if __name__ == "__main__":
    main()
