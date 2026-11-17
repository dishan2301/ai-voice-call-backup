"""Explicit optional local voice capability without model downloads."""
LOCAL_MODE = "android_system"

def capabilities(mode: str) -> dict:
    if mode == LOCAL_MODE:
        return {"id": LOCAL_MODE, "available": True,
                "input": "android_platform_speech", "output": "android_system_tts",
                "languages": ["en-IN", "hi-IN"], "code_switching": "best_effort",
                "server_models": False, "authorization_path": "same_phone_agent_final_transcript",
                "limitations": ["quality and latency depend on the Android device and installed speech service",
                                 "Hinglish accuracy is best_effort and not benchmarked in this environment",
                                 "browser WebRTC audio remains Gemini Live"]}
    return {"id": "gemini_live", "available": True, "input": "gemini_live_webrtc",
            "output": "gemini_live_audio", "languages": ["en-IN", "hi-IN"],
            "code_switching": "provider_supported", "server_models": False,
            "authorization_path": "final_transcript_only", "limitations": []}

def mode_catalog(selected: str) -> dict:
    return {"selected": selected, "default": "gemini_live",
            "modes": [capabilities("gemini_live"), capabilities(LOCAL_MODE)],
            "model_downloads_required": False}
