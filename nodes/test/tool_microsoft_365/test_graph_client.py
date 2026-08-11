# =============================================================================
# RocketRide Engine
# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# =============================================================================

"""Unit tests for the shared Microsoft Graph credential/request machinery."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

_NODES_SRC = Path(__file__).resolve().parents[2] / 'src'
if str(_NODES_SRC) not in sys.path:
    sys.path.insert(0, str(_NODES_SRC))

# Self-sufficient bootstrap: importing the nodes package pulls engine runtime
# modules (depends/rocketlib); stub them if absent so this file never depends
# on a sibling test having run first, then drop what we added.
from unittest.mock import MagicMock

_added = []
for _name in ('depends', 'rocketlib', 'ai', 'ai.common', 'ai.common.utils', 'ai.common.config'):
    if _name not in sys.modules:
        _stub = MagicMock()
        if _name == 'depends':
            _stub.depends = lambda *a, **k: None
        if _name == 'rocketlib':
            _stub.IInstanceBase = object
            _stub.IGlobalBase = object
            _stub.tool_function = lambda **kw: lambda f: f
        sys.modules[_name] = _stub
        _added.append(_name)

_fresh_nodes = 'nodes' not in sys.modules
from nodes.tool_microsoft_365 import graph_client as gc

for _name in _added:
    sys.modules.pop(_name, None)
if _fresh_nodes:
    for _name in [k for k in list(sys.modules) if k == 'nodes' or k.startswith('nodes.')]:
        sys.modules.pop(_name, None)
# Keep a direct reference; tests only touch graph_client's pure functions.

SVC = gc.GraphService(product='Excel')


def _resp(body: dict, status: int = 200, headers: dict | None = None):
    m = mock.MagicMock()
    m.read.return_value = json.dumps(body).encode()
    m.status = status
    m.headers = headers or {}
    m.__enter__ = lambda s: s
    m.__exit__ = lambda s, *a: False
    return m


class TestHostValidation:
    def test_refresh_url_rejects_untrusted_host(self):
        with pytest.raises(ValueError, match='not a trusted OAuth broker'):
            gc.resolve_refresh_url(SVC, 'https://evil.example.com/refresh')

    def test_refresh_url_accepts_builtin_and_env(self, monkeypatch):
        assert gc.resolve_refresh_url(SVC, 'https://oauth2.rocketride.ai/microsoft/refresh')
        monkeypatch.setenv('RR_OAUTH_BROKER_URL', 'https://broker.corp.local')
        assert gc.resolve_refresh_url(SVC, 'https://broker.corp.local/refresh')

    def test_refresh_url_rejects_http_scheme(self):
        with pytest.raises(ValueError):
            gc.resolve_refresh_url(SVC, 'http://oauth2.rocketride.ai/refresh')

    def test_token_uri_must_be_microsoftonline(self):
        with pytest.raises(ValueError, match='login.microsoftonline.com'):
            gc.resolve_token_uri(SVC, 'https://evil.example.com/token')
        ok = gc.resolve_token_uri(SVC, 'https://login.microsoftonline.com/common/oauth2/v2.0/token')
        assert ok.startswith('https://login.microsoftonline.com')


class TestAppOnlyAuth:
    CFG = {'tenantId': 't1', 'clientId': 'c1', 'clientSecret': 's1', 'userPrincipalName': 'a@b.com'}

    def test_acquires_and_caches_token(self):
        auth = gc.build_auth(SVC, 'service', self.CFG, ['Files.ReadWrite'])
        with mock.patch.object(gc, '_urlopen', return_value=_resp({'access_token': 'T', 'expires_in': 3600})) as u:
            assert auth.token() == 'T'
            assert auth.token() == 'T'  # cached, no second POST
            assert u.call_count == 1
            req = u.call_args[0][0]
            assert req.full_url == 'https://login.microsoftonline.com/t1/oauth2/v2.0/token'
            assert b'client_credentials' in req.data and b'.default' in req.data

    def test_expired_token_reacquired(self):
        auth = gc.build_auth(SVC, 'service', self.CFG, [])
        with mock.patch.object(gc, '_urlopen', return_value=_resp({'access_token': 'T1', 'expires_in': 1})):
            auth.token()
        with mock.patch.object(gc, '_urlopen', return_value=_resp({'access_token': 'T2', 'expires_in': 3600})):
            time.sleep(1.1)
            assert auth.token() == 'T2'

    def test_missing_config_fails_loud(self):
        with pytest.raises(ValueError, match='tenantId'):
            gc.build_auth(SVC, 'service', {'clientId': 'c', 'clientSecret': 's'}, [])


class TestBrokerUserAuth:
    def _payload(self, **over):
        p = {
            'access_token': 'A',
            'refresh_token': 'R',
            'token_uri': 'https://login.microsoftonline.com/common/oauth2/v2.0/token',
            'oauth_server_url': 'https://oauth2.rocketride.ai/microsoft/refresh',
            'expiry_date': int((time.time() - 10) * 1000),  # already expired
            'scope': 'Files.ReadWrite Mail.Read',
        }
        p.update(over)
        return {'userToken': json.dumps(p)}

    def test_refresh_posts_refresh_token_to_broker(self):
        auth = gc.build_auth(SVC, 'user', self._payload(), ['Files.ReadWrite'])
        with mock.patch.object(
            gc, '_urlopen', return_value=_resp({'access_token': 'B', 'expiry_date': int((time.time() + 3600) * 1000)})
        ) as u:
            assert auth.token() == 'B'
            req = u.call_args[0][0]
            assert req.full_url == 'https://oauth2.rocketride.ai/microsoft/refresh'
            assert json.loads(req.data) == {'refresh_token': 'R'}

    def test_refresh_200_without_token_is_contract_violation(self):
        auth = gc.build_auth(SVC, 'user', self._payload(), [])
        with mock.patch.object(gc, '_urlopen', return_value=_resp({'ok': True})):
            with pytest.raises(ValueError, match='no access token'):
                auth.token()

    def test_refresh_http_error_is_rejection(self):
        auth = gc.build_auth(SVC, 'user', self._payload(), [])
        err = urllib.error.HTTPError('u', 401, 'nope', {}, None)
        with mock.patch.object(gc, '_urlopen', side_effect=err):
            with pytest.raises(ValueError, match='rejected by the broker'):
                auth.token()

    def test_expired_without_refresh_path_fails_now(self):
        cfg = self._payload(oauth_server_url=None, refresh_token=None)
        with pytest.raises(ValueError, match='expired'):
            gc.build_auth(SVC, 'user', cfg, [])

    def test_missing_required_scope_fails_at_build(self):
        with pytest.raises(ValueError, match='Mail.Send'):
            gc.build_auth(SVC, 'user', self._payload(), ['Mail.Send'])

    def test_scope_report_reads_payload(self):
        granted, ok, missing = gc.token_scope_report(SVC, self._payload(), ['Files.Read'])
        assert 'Files.ReadWrite' in granted and ok and missing == []


class TestUserBase:
    def test_user_auth_is_me(self):
        assert gc.user_base({'authType': 'user'}) == '/me'

    def test_app_auth_targets_upn(self):
        assert gc.user_base({'authType': 'service', 'userPrincipalName': 'a@b.com'}) == '/users/a@b.com'

    def test_app_auth_without_upn_fails(self):
        with pytest.raises(ValueError, match='Acting User'):
            gc.user_base({'authType': 'service'})


class TestRequest:
    def _auth(self):
        a = mock.MagicMock()
        a.token.return_value = 'TOK'
        return a

    def test_json_request_and_auth_header(self):
        with mock.patch.object(gc, '_urlopen', return_value=_resp({'value': [1]})) as u:
            out = gc.request(SVC, self._auth(), 'GET', '/me/drive/root/children')
            assert out == {'value': [1]}
            req = u.call_args[0][0]
            assert req.full_url.startswith('https://graph.microsoft.com/v1.0/me/drive')
            assert req.get_header('Authorization') == 'Bearer TOK'

    def test_retries_429_with_retry_after(self):
        err = urllib.error.HTTPError('u', 429, 'throttle', {'Retry-After': '0'}, None)
        with mock.patch.object(gc, '_urlopen', side_effect=[err, _resp({'ok': 1})]) as u:
            assert gc.request(SVC, self._auth(), 'GET', '/me') == {'ok': 1}
            assert u.call_count == 2

    def test_403_names_scope_fix(self):
        import io

        body = io.BytesIO(json.dumps({'error': {'code': 'ErrorAccessDenied', 'message': 'Access is denied.'}}).encode())
        err = urllib.error.HTTPError('u', 403, 'forbidden', {}, body)
        with mock.patch.object(gc, '_urlopen', side_effect=err):
            with pytest.raises(gc.GraphError, match='Excel.*denied'):
                gc.request(SVC, self._auth(), 'GET', '/me')

    def test_extra_headers_merge_into_request(self):
        # extra_headers (e.g. If-Match for docx round-trip) ride on the
        # outgoing request alongside the default headers. urllib normalizes
        # header names to Title-Case, so 'If-Match' is read back as 'If-match'.
        with mock.patch.object(gc, '_urlopen', return_value=_resp({'ok': 1})) as u:
            out = gc.request(
                SVC, self._auth(), 'PUT', '/me/drive/items/1/content', data=b'x', extra_headers={'If-Match': 'abc123'}
            )
            assert out == {'ok': 1}
            req = u.call_args[0][0]
            assert req.get_header('If-match') == 'abc123'
            assert req.get_header('Authorization') == 'Bearer TOK'

    def test_412_precondition_failed_raises_conflict_graph_error(self):
        import io

        body = io.BytesIO(json.dumps({'error': {'code': 'resourceModified', 'message': 'stale etag'}}).encode())
        err = urllib.error.HTTPError('u', 412, 'precondition failed', {}, body)
        with mock.patch.object(gc, '_urlopen', side_effect=err):
            with pytest.raises(gc.GraphError, match='conflict'):
                gc.request(SVC, self._auth(), 'PUT', '/me/drive/items/1/content', data=b'x')

    def test_409_conflict_raises_conflict_graph_error(self):
        import io

        body = io.BytesIO(json.dumps({'error': {'code': 'nameAlreadyExists', 'message': 'conflict'}}).encode())
        err = urllib.error.HTTPError('u', 409, 'conflict', {}, body)
        with mock.patch.object(gc, '_urlopen', side_effect=err):
            with pytest.raises(gc.GraphError, match='conflict'):
                gc.request(SVC, self._auth(), 'PUT', '/me/drive/items/1/content', data=b'x')
