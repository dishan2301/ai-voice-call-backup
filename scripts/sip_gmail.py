"""Run the private Gmail voice bridge for the isolated, authenticated SIP account."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import wave
from uuid import UUID

from google.oauth2.credentials import Credentials
from websockets.exceptions import WebSocketException

from app.agents.mail_agent import MailAgent
from app.core.config import get_settings
from app.email.gmail import GMAIL_SCOPES, GmailProvider, create_gmail_service
from app.voice.gemini_live import GeminiLive
from app.voice.gmail_conversation import GmailConversationLive, waiting_audio
from app.voice.mail_first import LIVE_POLICY, MailFirstLive
from app.voice.session import VoiceSession
from scripts.m8_receptionist_server import CallServer

ROOT = Path(__file__).resolve().parents[1]
PBX = ROOT / ".local-asterisk/etc"


def read_settings():
    settings = get_settings()
    if settings.email_provider != "gmail" or not settings.google_token_file:
        raise ValueError("The laptop's Gmail read connection is required")
    token = ROOT / settings.google_token_file
    credentials = Credentials.from_authorized_user_file(str(token))
    if set(credentials.scopes or ()) != set(GMAIL_SCOPES):
        raise ValueError("A Gmail read-only grant is required")
    return settings.model_copy(update={
        "deployment_mode": "personal",
        "google_send_token_file": "",
        "mailbox_id": "sip-personal-gmail",
        "retrieval_backend": "existing",
        "retrieval_adaptive_enabled": False,
    })


def isolated_route():
    # Check the private config used by the existing trusted Wi-Fi manager.
    dialplan = (PBX / "extensions.conf").read_text()
    route = re.search(r"^\[android-test\]\s+exten => 700,1,Answer\(\)\s+"
                      r"same => n,AudioSocket\(([0-9a-f-]{36}),127\.0\.0\.1:19000\)",
                      dialplan, re.M)
    endpoint = (PBX / "pjsip.conf").read_text()
    if (not route or dialplan.count("AudioSocket(") != 1
            or not re.search(r"^auth=softphone-auth\s*$", endpoint, re.M)
            or not re.search(r"^context=android-test\s*$", endpoint, re.M)):
        raise ValueError("The authenticated isolated SIP route is missing or ambiguous")
    return str(UUID(route[1]))


class SipMailLive(MailFirstLive):
    async def send(self, payload):
        if "setup" in payload:
            setup = dict(payload["setup"])
            setup["systemInstruction"] = {"parts": [{"text": LIVE_POLICY + (
                "\nAt call opening, speak the server's welcome once without calling a tool. "
                "During the call, briefly acknowledge a mail question if retrieval takes time. "
                "Speak naturally in short phrases. Direct sync, drafts and sending to the website."
            )}]}
            setup["inputAudioTranscription"] = {}
            setup["outputAudioTranscription"] = {}
            return await GeminiLive.send(self, {"setup": setup})
        if "toolResponse" in payload:
            payload = {"toolResponse": {"functionResponses": [
                {**item, "response": {"spoken_answer": item["response"].get("spoken_answer", "")}}
                for item in payload["toolResponse"]["functionResponses"]
            ]}}
        await super().send(payload)


class GmailCall:
    def __init__(self, adapter, settings, continuous=False, *, ist_counts=False):
        self.adapter = adapter
        self.frames = 0
        self.continuous = continuous

        def read_provider():
            # A fresh provider query supplies evidence without bootstrapping a
            # hosted briefing or loading the separate Gmail send authorization.
            return GmailProvider(create_gmail_service(settings), settings=settings)

        def create_agent():
            agent = MailAgent(settings=settings, gmail=read_provider())
            agent.mail_first = True
            agent.sip_read_only = True
            return agent

        self.voice = VoiceSession(self.event, create_agent)
        self.voice.preserve_literals = True
        if continuous:
            self.session = GmailConversationLive(
                settings, self.voice, adapter.output, adapter.interrupt, read_provider,
                ist_counts=ist_counts)
        else:
            self.session = SipMailLive(settings, self.voice, adapter.output, adapter.interrupt)

    def event(self, event):
        kind = event["type"]
        if kind == "mail.result":
            result = event["result"]
            print(json.dumps({"event": "sip_mail_result", "state": result.get("state"),
                              "evidence_count": len(result.get("evidence", []))}), flush=True)
        elif kind == "sip.lookup.waiting":
            self.adapter.output(waiting_audio())
        elif kind in {"sip.tool", "sip.first_audio", "sip.interrupted", "sip.cache.unavailable"}:
            print(json.dumps({"event": kind, **{key: event[key] for key in
                  ("seconds", "cached", "failed", "after_provider_activity_end", "scope",
                   "complete", "tool") if key in event}}), flush=True)
        elif kind in {"speech.fallback", "speech.error", "session.error"}:
            self.adapter.interrupt()
            with wave.open(str(ROOT / "app/voice/service-unavailable.wav"), "rb") as audio:
                self.adapter.output(audio.readframes(audio.getnframes()))
                return audio.getnframes() / audio.getframerate()

    async def start(self):
        try:
            await asyncio.wait_for(self.session.start(), 8)
        except (TimeoutError, OSError, RuntimeError, ValueError, WebSocketException):
            await asyncio.sleep(self.event({"type": "session.error"}) + .1)
            raise RuntimeError("Gemini voice setup failed") from None
        self.voice.start()
        await self.session.send({"clientContent": {"turns": [{"role": "user", "parts": [{
            "text": "Welcome the caller now. Say: Morning Mail here. What would you like me to check?"
        }]}], "turnComplete": True}})

    async def audio(self, pcm):
        self.frames += 1
        await self.session.audio(pcm)

    async def receive(self):
        try:
            await self.session.receive()
        except RuntimeError:
            if self.continuous:
                await asyncio.sleep(self.event({"type": "session.error"}) + .1)
            raise

    async def close(self):
        self.voice.close()
        await self.session.close()


async def serve(settings):
    continuous = os.environ.get("SIP_GMAIL_CONTINUOUS") == "1"
    ist_counts = continuous and os.environ.get("SIP_GMAIL_IST_COUNTS") == "1"
    calls = CallServer(True, isolated_route(), grant_seconds=float("inf"),
                       call_seconds=None if continuous else 300,
                       live_factory=lambda adapter: GmailCall(adapter, settings, continuous,
                                                              ist_counts=ist_counts), reusable=True)
    async with await asyncio.start_server(calls.handle, "127.0.0.1", 19000,
                                         limit=8192) as server:
        print("Ready for Linphone calls to extension 700 (" +
              ("continuous pilot, IST counts" if ist_counts else
               "continuous pilot" if continuous else "current") + ")", flush=True)
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "check-gmail", "serve"])
    args = parser.parse_args()
    try:
        settings = read_settings()
        isolated_route()
        if args.command == "check-gmail":
            response = GmailProvider.from_settings(settings).service.users().getProfile(
                userId="me").execute()
            if not response.get("emailAddress"):
                raise ValueError("Gmail did not verify the read connection")
            print("Gmail read connection verified. No message contents requested.")
        elif args.command == "check":
            print("Private SIP route and existing Gmail read-only grant configured.")
        else:
            asyncio.run(serve(settings))
    except Exception as exc:
        print("SIP Gmail stopped:", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
