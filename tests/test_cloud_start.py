import base64
from pathlib import Path
import pytest
from deploy import start


def test_cloud_provider_key_is_private_and_cannot_be_silently_replaced(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(start,'__file__',str(tmp_path/'deploy/start.py'))
    monkeypatch.setenv('PROVIDER_CREDENTIAL_KEY_BASE64',base64.b64encode(b'c'*32).decode())
    monkeypatch.delenv('DATABASE_KEY_FILE',raising=False)
    start.prepare()
    key=tmp_path/'secrets/provider-credential-keys/1.key'
    assert key.read_bytes()==b'c'*32 and key.stat().st_mode&0o777==0o600
    monkeypatch.setenv('PROVIDER_CREDENTIAL_KEY_BASE64',base64.b64encode(b'd'*32).decode())
    with pytest.raises(RuntimeError):start.prepare()
    assert key.read_bytes()==b'c'*32


def test_cloud_refuses_to_replace_a_missing_existing_database_key(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(start,'__file__',str(tmp_path/'deploy/start.py'))
    (tmp_path/'data').mkdir()
    (tmp_path/'data/morning_mail_pa.db').write_bytes(b'not-a-real-database')
    with pytest.raises(RuntimeError):start.prepare()
