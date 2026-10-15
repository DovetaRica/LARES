import os
import stat
import pytest
from app.database import Database
from app.provider_settings import ProviderSettings

def build(tmp_path):
    return ProviderSettings(Database(tmp_path / 'home_ai.db'), tmp_path / 'data')

def test_save_and_read_round_trip(tmp_path):
    provider = build(tmp_path)
    assert provider.read() == {'enabled': False, 'base_url': '', 'models': [], 'default_model': '', 'key_present': False}
    assert provider.client() is None
    provider.save(True, 'https://api.example.com/v1', ['a', ' b ', ''], 'b', 'sk-1')
    assert provider.read() == {'enabled': True, 'base_url': 'https://api.example.com/v1', 'models': ['a', 'b'], 'default_model': 'b', 'key_present': True}
    client = provider.client()
    assert client is not None and client.installed == ['a', 'b'] and (client.model == 'b')

def test_client_stays_none_until_fully_configured(tmp_path):
    provider = build(tmp_path)
    for enabled, base_url, models, key in ((False, 'https://x/v1', ['m'], 'k'), (True, '', ['m'], 'k'), (True, 'https://x/v1', [], 'k'), (True, 'https://x/v1', ['m'], '')):
        provider.save(enabled, base_url, models, '', key)
        assert provider.client() is None, (enabled, base_url, models, key)

def test_saving_without_a_key_keeps_the_existing_one(tmp_path):
    provider = build(tmp_path)
    provider.save(True, 'https://x/v1', ['m'], '', 'sk-keep')
    provider.save(True, 'https://x/v1/', ['n'], '')
    assert provider.load_key() == 'sk-keep'
    with pytest.raises(ValueError):
        provider.save(True, 'https://x/v2', ['n'], '')
    assert provider.load_key() == 'sk-keep'
    provider.save(True, 'https://x/v2', ['n'], '', '')
    assert provider.load_key() == ''
    assert provider.key_path.exists() is False

@pytest.mark.skipif(os.name != 'posix', reason='file modes are a POSIX concept')
def test_key_file_is_readable_only_by_its_owner(tmp_path):
    provider = build(tmp_path)
    provider.save(True, 'https://x/v1', ['m'], '', 'sk-secret')
    assert stat.S_IMODE(provider.key_path.stat().st_mode) == 384

def test_the_database_never_holds_the_key(tmp_path):
    database = Database(tmp_path / 'home_ai.db')
    provider = ProviderSettings(database, tmp_path / 'data')
    provider.save(True, 'https://x/v1', ['m'], '', 'sk-secret')
    dump = ' '.join((str(value) for value in database.rows('SELECT key,value FROM system_state')))
    assert 'sk-secret' not in dump
    assert database.scalar("SELECT value FROM system_state WHERE key='third_party_config'")
