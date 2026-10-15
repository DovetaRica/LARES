"""Runtime regressions use temporary stores and explicit stub transports only."""
import socket

import pytest


@pytest.fixture(autouse=True)
def forbid_live_connections(monkeypatch, request):
    def denied(*args, **kwargs):
        raise AssertionError('Live connections are forbidden in runtime tests')

    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    # These wire regressions install MockTransport and need the normal loopback gate open.
    if request.module.__name__ in {'test_model_request_plan', 'test_report_budget_wire',
                                   'test_maintenance_safety'}:
        from app.config import settings
        monkeypatch.setattr(settings, 'lares_enable_network', True)
