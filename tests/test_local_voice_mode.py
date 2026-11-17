from app.voice.local_mode import LOCAL_MODE, capabilities, mode_catalog

def test_gemini_live_is_default_and_local_mode_is_explicit():
    catalog = mode_catalog("gemini_live")
    assert catalog["default"] == "gemini_live"
    assert catalog["selected"] == "gemini_live"
    assert {item["id"] for item in catalog["modes"]} == {"gemini_live", LOCAL_MODE}
    assert catalog["model_downloads_required"] is False

def test_android_system_mode_reports_truthful_limits():
    result = capabilities(LOCAL_MODE)
    assert result["available"] is True
    assert result["input"] == "android_platform_speech"
    assert result["output"] == "android_system_tts"
    assert result["code_switching"] == "best_effort"
    assert any("not benchmarked" in item for item in result["limitations"])
