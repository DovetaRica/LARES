import asyncio
import importlib.resources
import socket

import httpx
import pytest

from app.config import Settings, settings
from app.network import AsyncClient, check_endpoint


def test_new_install_has_no_household_mapping_or_automatic_network():
    config = Settings(_env_file=None)
    assert not config.lares_enable_network and not config.lares_enable_schedule
    assert not config.third_party_enabled and config.learning_only
    assert not config.sleep_curtain_entity and not config.arrival_entry_entity
    assert not config.presence_noise_entities
    assert config.home_ai_bind_ip == '127.0.0.1'


def test_network_denied_before_any_request(monkeypatch):
    monkeypatch.setattr(settings, 'lares_enable_network', False)
    async def run():
        async with AsyncClient() as client:
            with pytest.raises(PermissionError):
                await client.get('http://localhost:11434/api/tags')
    asyncio.run(run())


def test_remote_requires_optin_and_https(monkeypatch):
    monkeypatch.setattr(settings, 'lares_enable_network', True)
    monkeypatch.setattr(settings, 'lares_allow_remote', False)
    with pytest.raises(ValueError):
        check_endpoint('https://model.example.com/v1')
    monkeypatch.setattr(settings, 'lares_allow_remote', True)
    with pytest.raises(ValueError):
        check_endpoint('http://model.example.com/v1')
    with pytest.raises(ValueError):
        check_endpoint('https://user:synthetic-secret@model.example.com/v1')
    assert check_endpoint('https://model.example.com/v1').scheme == 'https'


def test_http_redirect_never_follows_even_when_requested(monkeypatch):
    monkeypatch.setattr(settings, 'lares_enable_network', True)
    urls = []
    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(302, headers={'location': 'https://unexpected.example'})
    async def run():
        async with AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            result = await client.get('http://localhost:11434', follow_redirects=True)
            assert result.status_code == 302
    asyncio.run(run())
    assert urls == ['http://localhost:11434']


def test_public_resources_bundled():
    root = importlib.resources.files('app')
    assert 'example_lamp' in root.joinpath('resources/entities.yaml').read_text(encoding='utf-8')
    for name in ('system', 'distill', 'segment', 'questions'):
        assert root.joinpath('resources/prompts/'+name+'.txt').is_file()
