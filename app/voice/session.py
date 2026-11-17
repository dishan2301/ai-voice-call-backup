"""One continuous conversation. Transport events are separate from mail authority."""
import asyncio
import secrets
from typing import Callable

from app.agents.requests import handle_mail_request


class VoiceSession:
    def __init__(self, send: Callable, agent_factory: Callable, session_key=None):
        self.send = send
        self.agent_factory = agent_factory
        self.session_key = session_key or secrets.token_urlsafe(24)
        self.state = "CONNECTING"
        self.epoch = 0
        self.transcript = ""
        self.closed = False
        self.contact_mode = False
        self.preserve_literals = False
        self.lock = asyncio.Lock()

    def event(self, kind, **fields):
        if not self.closed:
            self.send({"type": kind, "epoch": self.epoch, **fields})

    def transition(self, state):
        if self.closed:
            return
        self.state = state
        self.event("session.state", state=state)

    def start(self):
        self.transition("LISTENING")

    def interrupt(self, notify=True):
        self.epoch += 1
        if notify:
            self.event("response.cancelled")
        self.transition("INTERRUPTED")
        self.transition("LISTENING")

    def partial(self, text):
        self.transcript += text
        self.event("transcript.partial", text=self.transcript)

    def replace_transcript(self, text):
        self.transcript = text[:2000]
        self.event("transcript.partial", text=self.transcript)

    def final(self, text):
        self.transcript = text[:2000]
        self.event("transcript.final", text=self.transcript)

    async def respond(self, text, input_mode="voice", review_ticket=""):
        if self.closed:
            return {"cancelled": True}
        epoch = self.epoch
        self.transcript = ""
        self.transition("THINKING")
        loop = asyncio.get_running_loop()

        def emit(event):
            def publish():
                if epoch == self.epoch:
                    self.event(event.pop("type"), **event)
            loop.call_soon_threadsafe(publish)

        async with self.lock:
            if epoch != self.epoch or self.closed:
                return {"cancelled": True}
            try:
                result = await asyncio.to_thread(
                    handle_mail_request, self.agent_factory, text, self.session_key,
                    input_mode=input_mode, review_ticket=review_ticket,
                    preserve_literals=self.preserve_literals,
                    emit=emit, cancelled=lambda: self.closed or epoch != self.epoch)
            except Exception:
                result = {"answer": "The mail assistant could not complete that request. Please try again.",
                          "spoken_answer": "I couldn't complete that request. Your saved briefing is still available.",
                          "state": "ERROR", "evidence": []}
        if epoch != self.epoch or self.closed:
            return {"cancelled": True}
        self.event("mail.result", result=result)
        self.transition("LISTENING")
        return result

    def audio(self, encoded):
        self.transition("SPEAKING")
        self.event("response.audio.delta", audio=encoded)

    def close(self):
        if self.closed:
            return
        self.epoch += 1
        self.transition("IDLE")
        self.closed = True
