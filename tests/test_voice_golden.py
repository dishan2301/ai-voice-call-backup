import pytest

from app.voice.normalizer import normalize_voice
from app.agents.slots import search_query
from app.core.config import Settings


@pytest.mark.parametrize('raw, expected', [
    ('how many male from side us', 'mail from side us'),
    ('any mail afro zydus', 'mail from zydus'),
    ('do I have any mating mail', 'meeting mail'),
])
def test_speech_repairs_are_reversible_and_never_authorize_send(raw, expected):
    value = normalize_voice(raw, input_mode='voice')
    assert expected in value.text
    assert value.tier != 'HIGH'


def test_typed_text_is_never_rewritten():
    assert normalize_voice('sand it', input_mode='text').text == 'sand it'


def test_known_hinglish_count_and_uncertain_commands():
    count = normalize_voice('kitne mail aaye aaj', input_mode='voice')
    assert count.tier == 'UNKNOWN' and 'today' in count.text
    for raw in ('reply karo us customer ko', 'sand it', 'argent mail padho'):
        assert normalize_voice(raw, input_mode='voice').tier == 'LOW'
    assert normalize_voice('sand it', input_mode='voice').text != 'send it'


def test_model_cannot_invent_a_search_date():
    with pytest.raises(ValueError):
        search_query('from:zydus after:2026/01/01', 'Find Zydus mail', Settings().timezone)


def test_today_filter_uses_local_midnight():
    from datetime import datetime
    zone = Settings().timezone
    now = datetime(2026, 9, 16, 12, tzinfo=zone)
    query = search_query('meeting after:2020/01/01', 'Meeting mail today', zone, now)
    assert str(int(now.replace(hour=0).timestamp())) in query
    assert '2020' not in query


def test_today_mail_count_adds_received_window_when_model_omits_dates():
    from datetime import datetime, timedelta
    zone = Settings().timezone
    now = datetime(2026, 9, 16, 12, tzinfo=zone)
    query = search_query('mail', 'How many mail did I receive today?', zone, now)
    assert str(int(now.replace(hour=0).timestamp())) in query
    assert str(int((now.replace(hour=0) + timedelta(days=1)).timestamp())) in query
