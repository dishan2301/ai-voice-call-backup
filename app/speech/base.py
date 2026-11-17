from abc import ABC, abstractmethod


class TTSError(RuntimeError):
    pass


class TTSProvider(ABC):
    @abstractmethod
    def play(self, text: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def replay(self) -> None:
        raise NotImplementedError
