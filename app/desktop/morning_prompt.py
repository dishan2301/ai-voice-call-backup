from enum import Enum
import shutil
import subprocess
from typing import Optional


class PromptDecision(str, Enum):
    YES = "YES"
    NO = "NO"
    REMIND = "REMIND"


class DesktopPromptError(RuntimeError):
    pass


class DesktopPrompt:
    def __init__(self, executable: Optional[str] = None):
        self.executable = executable or shutil.which("zenity") or ""
        if not self.executable:
            raise DesktopPromptError("zenity is required for the desktop permission prompt")

    def ask(self) -> PromptDecision:
        result = subprocess.run(
            [
                self.executable,
                "--question",
                "--title=Morning Mail PA",
                "--text=Good morning.\nI reviewed your emails.\nWould you like today's email briefing?",
                "--ok-label=Yes, read it",
                "--cancel-label=No",
                "--extra-button=Remind me in 15 minutes",
                "--width=430",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=300,
            check=False,
        )
        if result.stdout.strip() == "Remind me in 15 minutes":
            return PromptDecision.REMIND
        return PromptDecision.YES if result.returncode == 0 else PromptDecision.NO
