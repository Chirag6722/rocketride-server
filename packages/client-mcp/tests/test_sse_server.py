# MIT License
# Copyright (c) 2026 Aparavi Software AG
# Tests for rocketride_mcp.sse_server — the HTTP/SSE transport shipped as
# docker/Dockerfile.mcp and the `rocketride-mcp-sse` console script.
#
# This module previously had no tests at all, which is how `settings.auth` (a
# field that has never existed on the Settings dataclass) shipped and stayed
# broken: every tool call and every health probe died with AttributeError
# before reaching the engine, and /health answered 200 regardless, so the
# container's HEALTHCHECK reported it healthy.

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip('starlette', reason='SSE transport is an optional extra: pip install rocketride-mcp[sse]')

from starlette.testclient import TestClient  # noqa: E402

import rocketride_mcp.sse_server as sse  # noqa: E402


# -----------------------------------------------------------------------------
# Client construction
# -----------------------------------------------------------------------------


def test_get_client_uses_the_apikey_field(env_rocketride: None) -> None:
    """The credential is read from Settings.apikey, the field that exists.

    Regression test for the `settings.auth` AttributeError. Asserting on the
    kwargs passed to RocketRideClient — rather than just "it did not raise" —
    is what keeps the credential from silently going missing again.
    """
    with patch.object(sse, 'RocketRideClient') as ctor:
        sse._get_client()

    ctor.assert_called_once_with(uri='wss://test.example.com', auth='test-api-key')


def test_get_client_propagates_missing_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing ROCKETRIDE_URI / auth surfaces as ValueError, not AttributeError."""
    monkeypatch.delenv('ROCKETRIDE_AUTH', raising=False)
    monkeypatch.delenv('ROCKETRIDE_APIKEY', raising=False)
    monkeypatch.delenv('ROCKETRIDE_URI', raising=False)

    with pytest.raises(ValueError):
        sse._get_client()


# -----------------------------------------------------------------------------
# /health
# -----------------------------------------------------------------------------


def _connectable_client() -> MagicMock:
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    return client


def test_health_returns_200_when_the_engine_round_trips(env_rocketride: None) -> None:
    """A healthy server answers 200 with status ok."""
    with patch.object(sse, '_get_client', return_value=_connectable_client()):
        with TestClient(sse.create_app()) as http:
            response = http.get('/health')

    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'ok'
    assert body['server'] == 'rocketride-mcp'


def test_health_returns_503_when_the_engine_is_unreachable(env_rocketride: None) -> None:
    """An unreachable engine fails the probe instead of passing it.

    The Docker HEALTHCHECK is a bare urlopen, so a 200 carrying
    ``status: degraded`` marked the container healthy while nothing worked.
    """
    client = MagicMock()
    client.connect = AsyncMock(side_effect=OSError('connection refused'))
    client.disconnect = AsyncMock()

    with patch.object(sse, '_get_client', return_value=client):
        with TestClient(sse.create_app()) as http:
            response = http.get('/health')

    assert response.status_code == 503
    body = response.json()
    assert body['status'] == 'degraded'
    assert body['engine'] == 'unreachable'


def test_health_reports_a_configuration_fault_distinctly(env_rocketride: None) -> None:
    """A wiring/config error is not reported as an unreachable engine.

    Collapsing both into ``engine: unreachable`` is what sent operators
    looking at the engine when the fault was in this process.
    """
    with patch.object(sse, '_get_client', side_effect=ValueError('Missing required environment variable')):
        with TestClient(sse.create_app()) as http:
            response = http.get('/health')

    assert response.status_code == 503
    body = response.json()
    assert body['status'] == 'error'
    assert body['error'] == 'configuration'
    assert 'engine' not in body


# -----------------------------------------------------------------------------
# Bearer auth middleware
# -----------------------------------------------------------------------------


def test_health_is_reachable_without_a_token(monkeypatch: pytest.MonkeyPatch, env_rocketride: None) -> None:
    """/health bypasses auth so monitoring does not need the API key."""
    monkeypatch.setattr(sse, '_API_KEY', 's3cret')

    with patch.object(sse, '_get_client', return_value=_connectable_client()):
        with TestClient(sse.create_app()) as http:
            response = http.get('/health')

    assert response.status_code == 200


@pytest.mark.parametrize(
    'header',
    [
        None,
        'Bearer wrong',
        's3cret',  # right token, missing the scheme
        'bearer s3cret',  # scheme is case-sensitive here
    ],
)
def test_non_health_routes_reject_bad_credentials(
    header: str | None,
    monkeypatch: pytest.MonkeyPatch,
    env_rocketride: None,
) -> None:
    """Anything but the exact ``Bearer <MCP_API_KEY>`` value is a 401."""
    monkeypatch.setattr(sse, '_API_KEY', 's3cret')

    headers = {'authorization': header} if header is not None else {}
    with TestClient(sse.create_app()) as http:
        response = http.get('/sse', headers=headers)

    assert response.status_code == 401
    assert response.json() == {'error': 'unauthorized'}


def test_auth_middleware_is_absent_when_no_api_key_is_set(
    monkeypatch: pytest.MonkeyPatch,
    env_rocketride: None,
) -> None:
    """With MCP_API_KEY unset the server is open — documented, and asserted.

    Recorded as a test so the "unauthenticated by default" posture is a
    deliberate, visible choice rather than an accident of configuration.
    """
    monkeypatch.setattr(sse, '_API_KEY', '')

    app = sse.create_app()

    assert not [m for m in app.user_middleware if m.cls is sse.AuthMiddleware]
