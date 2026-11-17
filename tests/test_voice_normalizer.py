from app.voice.normalizer import normalize_voice


def test_voice_normalizer_repairs_common_indian_english_mishears():
    result = normalize_voice("how many male from side us", input_mode="voice")
    assert result.text == "how many mail from side us"
    assert result.tier == "UNKNOWN"
    assert result.corrections


def test_voice_alternative_with_real_sender_wins_when_confidence_is_higher():
    result = normalize_voice(
        "how many male from side us",
        [{"text": "how many male from side us", "confidence": 0.55},
         {"text": "how many mail from Zydus", "confidence": 0.70}],
        input_mode="voice",
    )
    assert result.text == "how many mail from Zydus"


def test_typed_input_is_high_confidence_and_unchanged_except_safe_terms():
    result = normalize_voice("Show my Zydus mail", input_mode="text")
    assert result.text == "Show my Zydus mail"
    assert result.tier == "HIGH"


def test_repairs_never_increase_asr_confidence_or_hide_zero():
    for score, tier in [(0.0, "UNUSABLE"), (0.5, "LOW"), (0.7, "MEDIUM")]:
        result = normalize_voice("male from Acme", [{"text": "male from Acme", "confidence": score}], "voice")
        assert result.text == "mail from Acme"
        assert result.confidence == score and result.tier == tier


def test_missing_and_invalid_asr_scores_are_unknown():
    for score in (None, "invalid", -1, float("nan"), float("inf")):
        result = normalize_voice("Read the message", [{"text": "Read the message", "confidence": score}], "voice")
        assert result.tier == "UNKNOWN" and result.confidence is None
