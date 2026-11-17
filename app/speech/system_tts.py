from pathlib import Path
import shutil
import subprocess
from threading import Lock
from typing import Optional

from app.speech.base import TTSError, TTSProvider


MAX_SPOKEN_CHARS = 20_000


class SystemTTSProvider(TTSProvider):
    """Local TTS using a fixed executable and argv-only subprocess calls."""

    def __init__(self, rate: int = 165, volume: float = 1.0, executable: Optional[str] = None):
        self.rate = rate
        self.volume = volume
        self.executable = executable or self._find_executable()
        self.process: Optional[subprocess.Popen] = None
        self.last_text = ""
        self.lock = Lock()

    @staticmethod
    def _find_executable() -> str:
        for name in ("spd-say", "espeak-ng", "espeak"):
            path = shutil.which(name)
            if path:
                return path
        raise TTSError("No supported local TTS executable was found")

    def _command(self, text: str):
        name = Path(self.executable).name
        if name == "spd-say":
            rate = max(-100, min(100, round((self.rate - 165) * 100 / 115)))
            volume = max(-100, min(100, round(self.volume * 200 - 100)))
            return [self.executable, "--wait", "--rate", str(rate), "--volume", str(volume), text]
        amplitude = max(0, min(200, round(self.volume * 200)))
        return [self.executable, "-s", str(self.rate), "-a", str(amplitude), text]

    def play(self, text: str) -> None:
        spoken = text.strip()[:MAX_SPOKEN_CHARS]
        if not spoken:
            raise TTSError("The spoken briefing is empty")
        with self.lock:
            self._stop_locked()
            try:
                self.process = subprocess.Popen(
                    self._command(spoken),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                )
            except OSError as exc:
                raise TTSError("Local TTS could not start") from exc
            self.last_text = spoken

    def _stop_locked(self) -> None:
        had_process = self.process is not None
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        if had_process and Path(self.executable).name == "spd-say":
            subprocess.run(
                [self.executable, "--cancel"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )

    def stop(self) -> None:
        with self.lock:
            self._stop_locked()

    def replay(self) -> None:
        if not self.last_text:
            raise TTSError("There is no briefing to replay")
        self.play(self.last_text)
