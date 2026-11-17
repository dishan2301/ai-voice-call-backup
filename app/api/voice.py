"""Authenticated WebRTC signaling and a bounded server-to-server audio bridge."""
import asyncio
import json
import secrets
import logging
from fractions import Fraction

from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from av import AudioFrame, AudioResampler
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agents.mail_agent import MailAgent
from app.api.dependencies import create_mail_agent, create_assistant
from app.core.auth import MailboxContext, require_mailbox_context
from app.core.config import get_settings
from app.voice.gemini_live import GeminiLive
from app.voice.session import VoiceSession
from app.voice.local_mode import mode_catalog


router = APIRouter(prefix="/phone/voice", tags=["voice"])
logger = logging.getLogger(__name__)
peers = {}


def ice_servers(settings):
    if not settings.voice_ice_urls:
        return []
    return [RTCIceServer(urls=settings.voice_ice_urls,
                         username=settings.voice_ice_username or None,
                         credential=settings.voice_ice_credential or None)]


@router.get("/config")
async def voice_config(context: MailboxContext = Depends(require_mailbox_context)):
    settings = get_settings()
    servers = []
    if settings.voice_ice_urls:
        server = {"urls": settings.voice_ice_urls}
        if settings.voice_ice_username:
            server["username"] = settings.voice_ice_username
        if settings.voice_ice_credential:
            server["credential"] = settings.voice_ice_credential
        servers.append(server)
    return {"iceServers": servers, "maxReconnectAttempts": 2}


@router.get("/modes")
async def voice_modes(context: MailboxContext = Depends(require_mailbox_context)):
    return mode_catalog(get_settings().voice_mode)


class Speaker(AudioStreamTrack):
    """Paced 24 kHz mono PCM; interruption discards queued samples immediately."""
    def __init__(self):
        super().__init__()
        self.buffer = bytearray()
        self.pts = 0
        self.started = None

    def push(self, pcm):
        # ponytail: 10-second buffer ceiling; streaming backpressure if longer replies are needed.
        if len(self.buffer) + len(pcm) > 480000:
            raise ValueError("Voice playback fell behind")
        self.buffer.extend(pcm)

    def clear(self):
        self.buffer.clear()

    async def recv(self):
        loop = asyncio.get_running_loop()
        if self.started is None:
            self.started = loop.time()
        await asyncio.sleep(max(0, self.started + self.pts / 24000 - loop.time()))
        data = bytes(self.buffer[:960])
        del self.buffer[:960]
        frame = AudioFrame(format="s16", layout="mono", samples=480)
        frame.planes[0].update(data.ljust(960, b"\x00"))
        frame.sample_rate, frame.pts, frame.time_base = 24000, self.pts, Fraction(1, 24000)
        self.pts += 480
        return frame


class Offer(BaseModel):
    mail_first: bool = False
    sdp: str = Field(min_length=1, max_length=200000)
    type: str = Field(pattern="^offer$")
    session_key: str = Field(min_length=16, max_length=255)


async def close_peer(peer_id):
    state = peers.pop(peer_id, None)
    if not state:
        return
    state["voice"].close()
    disconnect_task = state.pop("disconnect_task", None)
    tasks = [task for task in [*state["tasks"], disconnect_task]
             if task is not None and task is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    try:
        await asyncio.wait_for(state["live"].close(), timeout=5)
    except Exception as exc:
        logger.warning("voice_provider_close_failed", extra={"error_type": type(exc).__name__})
    finally:
        await state["pc"].close()


async def close_all():
    await asyncio.gather(*(close_peer(key) for key in list(peers)))


@router.post("/offer")
async def offer(request: Offer, context: MailboxContext = Depends(require_mailbox_context)):
    if isinstance(context, MailboxContext) and context.provider == "microsoft":
        request = request.model_copy(update={"mail_first": True})
    if request.mail_first and not isinstance(context, MailboxContext):
        raise HTTPException(401, "An authenticated mailbox owner is required")
    mailbox_id = context.mailbox_id if isinstance(context, MailboxContext) else "default"
    owner = (mailbox_id, request.session_key)
    for existing_id, state in list(peers.items()):
        if state.get("owner") == owner:
            await close_peer(existing_id)
    if len(peers) >= 2:
        raise HTTPException(429, "Two voice sessions are already open. Stop one first.")
    settings = get_settings()
    pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers(settings)))
    speaker = Speaker()
    channel = None
    peer_id = secrets.token_urlsafe(24)

    def send(event):
        if channel and channel.readyState == "open" and channel.bufferedAmount < 65536:
            encoded = json.dumps(event, default=str)
            if len(encoded.encode()) <= 60000:
                channel.send(encoded)
            else:
                channel.send(json.dumps({"type": "session.error", "message": "This response is too long for voice. Please narrow the request."}))

    agent_factory = (lambda: create_assistant(context)) if isinstance(context, MailboxContext) else MailAgent
    if request.mail_first:
        from app.voice.mail_first import mail_agent_factory
        agent_factory = mail_agent_factory(context)
    voice = VoiceSession(send, agent_factory, request.session_key)
    voice.preserve_literals = request.mail_first
    if request.mail_first:
        from app.voice.mail_first import MailFirstLive
        live = MailFirstLive(settings, voice, speaker.push, speaker.clear)
    else:
        live = GeminiLive(settings, voice, speaker.push, speaker.clear)
    state = {"pc": pc, "voice": voice, "live": live, "tasks": [], "owner": owner}
    peers[peer_id] = state

    async def watch(coro):
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("voice_session_task_failed", extra={"error_type": type(exc).__name__})
            voice.event("session.error", message="Voice service stopped. Your saved brief and typed chat remain available.")
        finally:
            await close_peer(peer_id)

    @pc.on("datachannel")
    def on_channel(value):
        nonlocal channel
        if channel is not None or value.label != "mail-events":
            value.close()
            return
        channel = value
        voice.start()

        @channel.on("message")
        def on_message(raw):
            if not isinstance(raw, str) or len(raw) > 4096:
                return
            try:
                event = json.loads(raw)
            except ValueError:
                return
            if isinstance(event, dict) and event.get("type") == "interrupt":
                voice.interrupt(notify=False)
                live.allow_audio = False
                speaker.clear()
            elif isinstance(event, dict) and event.get("type") == "contact.mode" and not request.mail_first:
                voice.contact_mode = bool(event.get("active"))
            elif isinstance(event, dict) and event.get('type') == 'briefing.play' and isinstance(context, MailboxContext) and not request.mail_first:
                from app.voice.daily_briefing import play
                job = asyncio.create_task(play(live, voice, context, str(event.get('run_id', ''))))
                state['tasks'].append(job)

    @pc.on("track")
    def on_track(track):
        if track.kind != "audio":
            track.stop()
            return

        async def capture():
            resampler = AudioResampler(format="s16", layout="mono", rate=16000)
            while True:
                frame = await track.recv()
                for mono in resampler.resample(frame):
                    await live.audio(bytes(mono.planes[0])[:mono.samples * 2])
        state["tasks"].append(asyncio.create_task(watch(capture())))

    @pc.on("connectionstatechange")
    async def connection_changed():
        if pc.connectionState in {"failed", "closed"}:
            await close_peer(peer_id)
        elif pc.connectionState == "disconnected":
            # Mobile Wi-Fi and WebView ICE can briefly report disconnected
            # while a candidate pair changes. Do not destroy a valid session
            # until the grace period has elapsed.
            async def delayed_close():
                await asyncio.sleep(8)
                if pc.connectionState == "disconnected":
                    await close_peer(peer_id)
            previous = state.pop("disconnect_task", None)
            if previous:
                previous.cancel()
            state["disconnect_task"] = asyncio.create_task(delayed_close())
        elif pc.connectionState == "connected":
            task = state.pop("disconnect_task", None)
            if task:
                task.cancel()

    async def expiry():
        await asyncio.sleep(600)
        voice.event("session.error", message="This voice session ended after ten minutes. Start another to continue.")

    try:
        await live.start()
        pc.addTrack(speaker)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=request.sdp, type=request.type))
        await pc.setLocalDescription(await pc.createAnswer())
        state["tasks"].extend([asyncio.create_task(watch(live.receive())), asyncio.create_task(watch(expiry()))])
        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type, "session_id": peer_id}
    except Exception as exc:
        await close_peer(peer_id)
        raise HTTPException(503, "Live voice could not connect. Check the backend key, model access and network.") from exc


@router.delete("/{peer_id}")
async def stop(peer_id: str, context: MailboxContext = Depends(require_mailbox_context)):
    state = peers.get(peer_id)
    if (state and isinstance(context, MailboxContext)
            and state.get("owner", (None,))[0] != context.mailbox_id):
        raise HTTPException(404, "Voice session was not found")
    await close_peer(peer_id)
    return {"state": "IDLE"}
