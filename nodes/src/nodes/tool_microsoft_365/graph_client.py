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

"""
Shared Microsoft Graph credential and request machinery — the ONE copy.

Every Microsoft 365 tool service (excel, word, onedrive, outlook mail,
outlook calendar) declares a :class:`GraphService` profile and calls through
these functions. This file owns everything that guards credentials and
requests:

- credential construction (app-only client-credentials and broker-issued
  user OAuth) with a validated ``token_uri`` (login.microsoftonline.com
  only) and a validated broker refresh URL (https + trusted broker hosts,
  ``RR_OAUTH_BROKER_URL`` for self-hosted);
- the broker-backed refresh that fails loud on HTTP rejection, connection
  errors, malformed bodies, and 200s without an access token, and clears a
  stale expiry so a fresh token is never re-refreshed per call;
- scope diagnostics (:func:`token_scope_report`) shared by ``validateConfig``,
  ``check_connection``, and ``build_auth`` so the three checks cannot drift;
- request execution with exponential backoff on 429/5xx, while permission
  401/403s and other errors fail fast with an agent-readable message.

Per-service subpackages keep only their tool functions and response cleaners,
and bind these functions via ``functools.partial`` (see e.g. ``excel/client.py``).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from nodes.core.microsoft_access import missing_scopes

GRAPH_BASE = 'https://graph.microsoft.com/v1.0'

# Hosts the token-refresh POST may target. The token payload is untrusted (it
# rides in saved pipes and broker responses); a token-supplied refresh URL is
# honored only if it is https and its host is one of these, so a tampered token
# can never redirect the refresh_token to an attacker host. RR_OAUTH_BROKER_URL
# lets self-hosted deployments add their own broker host.
_BROKER_HOSTS = frozenset({'oauth2.rocketride.ai', 'oauth.rocketride.ai'})

# Microsoft's Entra ID (Azure AD) OAuth2 token endpoints. token_uri rides in
# the saved token (untrusted); the broker's refresh POSTs the refresh_token
# there, so only Microsoft's own host is accepted — a tampered token can't
# redirect credentials elsewhere.
_MS_TOKEN_HOSTS = frozenset({'login.microsoftonline.com'})

# HTTP status codes worth retrying (rate-limit + transient server errors).
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _urlopen(req, timeout=30):
    """Seam for tests; all HTTP goes through here."""
    return urllib.request.urlopen(req, timeout=timeout)


@dataclass(frozen=True)
class GraphService:
    """Profile of one Microsoft 365 service consuming the shared machinery.

    ``superset_scopes``: granted scopes that implicitly satisfy any request for
    this service (e.g. a Files.ReadWrite grant covers a readonly node); a
    literal membership check would otherwise false-error when a broader grant
    exists.
    """

    product: str  # human label used in error messages, e.g. 'Excel'
    superset_scopes: frozenset[str] = field(default_factory=frozenset)


class GraphError(RuntimeError):
    """Agent-readable Graph request failure."""


def resolve_refresh_url(svc: GraphService, token_url: object) -> str | None:
    """Validate a token-supplied refresh URL against the trusted broker hosts.

    Returns the URL when it is https and its host is a known broker host
    (built-in or the RR_OAUTH_BROKER_URL host). Returns None when the token
    carries no URL. Raises ValueError for any other value so a tampered token
    fails loud instead of silently posting credentials elsewhere.
    """
    if not token_url:
        return None
    if not isinstance(token_url, str):
        raise ValueError(f'{svc.product} token oauth_server_url must be a string')

    allowed = set(_BROKER_HOSTS)
    env_broker = os.environ.get('RR_OAUTH_BROKER_URL', '')
    if env_broker:
        env_host = urllib.parse.urlparse(env_broker).hostname
        if not env_host:
            # A schemeless value ("broker.example.com") parses with no hostname;
            # treat it as a bare host instead of silently dropping the allowlist entry.
            env_host = urllib.parse.urlparse('//' + env_broker).hostname
        if env_host:
            allowed.add(env_host.lower())

    parsed = urllib.parse.urlparse(token_url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.hostname.lower() not in allowed:
        raise ValueError(
            f'{svc.product} token refresh URL {token_url!r} is not a trusted OAuth broker '
            '(expected https and one of: ' + ', '.join(sorted(allowed)) + '). '
            'Reconnect your Microsoft account, or set RR_OAUTH_BROKER_URL for a self-hosted broker.'
        )
    return token_url


def resolve_token_uri(svc: GraphService, token_uri: object) -> str:
    """Return a trusted Microsoft token endpoint, or raise for a tampered value.

    Defaults to Microsoft's standard common-tenant endpoint when absent. Any
    present value must be https with a login.microsoftonline.com host, else
    ValueError (fail loud, never silently POST the refresh_token to an
    attacker host).
    """
    default = 'https://login.microsoftonline.com/common/oauth2/v2.0/token'
    if not token_uri:
        return default
    if not isinstance(token_uri, str):
        raise ValueError(f'{svc.product} token_uri must be a string')
    parsed = urllib.parse.urlparse(token_uri)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.hostname.lower() not in _MS_TOKEN_HOSTS:
        raise ValueError(
            f'{svc.product} token_uri {token_uri!r} is not a trusted Microsoft OAuth endpoint '
            '(expected https and one of: ' + ', '.join(sorted(_MS_TOKEN_HOSTS)) + '). '
            'Reconnect your Microsoft account.'
        )
    return token_uri


def _decode_blob(value: str) -> str:
    """Return text from a raw string or a base64 ``data:`` URL (serviceKey/userToken fields)."""
    if not value:
        raise ValueError('missing credential value')
    if value.startswith('data:'):
        _, _, payload = value.partition(',')
        try:
            return base64.b64decode(payload).decode('utf-8')
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f'could not decode data-url credential: {exc}') from exc
    return value


class GraphAuth:
    def token(self) -> str:
        raise NotImplementedError


class AppOnlyAuth(GraphAuth):
    """Client-credentials token, cached in-process until 60s before expiry."""

    def __init__(self, svc, tenant_id, client_id, client_secret):
        self._svc, self._tenant, self._id, self._secret = svc, tenant_id, client_id, client_secret
        self._token, self._exp = None, 0.0

    def token(self) -> str:
        if self._token and _time.time() < self._exp - 60:
            return self._token
        body = urllib.parse.urlencode(
            {
                'grant_type': 'client_credentials',
                'client_id': self._id,
                'client_secret': self._secret,
                'scope': 'https://graph.microsoft.com/.default',
            }
        ).encode()
        req = urllib.request.Request(
            f'https://login.microsoftonline.com/{self._tenant}/oauth2/v2.0/token',
            data=body,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            method='POST',
        )
        try:
            with _urlopen(req, timeout=10) as resp:
                raw = resp.read().decode()
            data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            # HTTPError subclasses URLError: catch it first so a broker
            # rejection (401/500/...) is not misreported as unreachable.
            raise ValueError(
                f'{self._svc.product} token acquisition was rejected by Microsoft (HTTP {exc.code}). '
                'Check the Entra app credentials.'
            ) from exc
        except urllib.error.URLError as exc:
            raise ValueError(
                f'{self._svc.product} token acquisition failed (Microsoft token endpoint unreachable: {exc}). '
                'Check the Entra app credentials.'
            ) from exc
        except OSError as exc:
            # e.g. a socket timeout raised from resp.read(); URLError is
            # a subclass of OSError so this branch must follow it.
            raise ValueError(
                f'{self._svc.product} token acquisition failed (connection error: {exc}). '
                'Check the Entra app credentials.'
            ) from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f'{self._svc.product} token acquisition returned a malformed response. Check the Entra app credentials.'
            ) from exc
        token = data.get('access_token') if isinstance(data, dict) else None
        if not token:
            raise ValueError(
                f'{self._svc.product}: Microsoft token endpoint returned no access token. '
                'Check the Entra app credentials.'
            )
        self._token = token
        self._exp = _time.time() + float(data.get('expires_in') or 0)
        return token


class BrokerUserAuth(GraphAuth):
    """Broker-issued user token; refresh POSTs the refresh_token to the broker.

    Refresh contract (design.md): POST {oauth_server_url} {'refresh_token': ...}
    -> 200 {'access_token': ..., 'expiry_date': <ms>}. Non-200 = rejection;
    200 without access_token = contract violation. Validate expiry_date BEFORE
    committing the new token; a missing expiry clears the old one (None = not expiring).
    """

    def __init__(self, svc, access_token, refresh_token, broker_url, expiry_ms):
        self._svc = svc
        self._token = access_token
        self._refresh_token = refresh_token
        self._broker_url = broker_url
        self._expiry_ms = expiry_ms

    def _is_expired(self) -> bool:
        if self._expiry_ms is None:
            return False
        return (self._expiry_ms / 1000.0) < _time.time()

    def token(self) -> str:
        if self._token and not self._is_expired():
            return self._token
        if not self._broker_url or not self._refresh_token:
            raise ValueError(
                f'{self._svc.product} access token has expired. '
                'Please reconnect your Microsoft account in the node settings.'
            )
        body = json.dumps({'refresh_token': self._refresh_token}).encode()
        req = urllib.request.Request(
            self._broker_url,
            data=body,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with _urlopen(req, timeout=10) as resp:
                raw = resp.read().decode()
            data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            # HTTPError subclasses URLError: catch it first so a broker
            # rejection (401/500/...) is not misreported as unreachable.
            raise ValueError(
                f'{self._svc.product} token refresh was rejected by the broker (HTTP {exc.code}). '
                'Please reconnect your Microsoft account.'
            ) from exc
        except urllib.error.URLError as exc:
            raise ValueError(
                f'{self._svc.product} token refresh failed (broker unreachable: {exc}). '
                'Please reconnect your Microsoft account.'
            ) from exc
        except OSError as exc:
            # e.g. a socket timeout raised from resp.read(); URLError is
            # a subclass of OSError so this branch must follow it.
            raise ValueError(
                f'{self._svc.product} token refresh failed (connection error during broker request: {exc}). '
                'Please reconnect your Microsoft account.'
            ) from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f'{self._svc.product} token refresh returned a malformed response. Please reconnect your Microsoft account.'
            ) from exc
        token = data.get('access_token') if isinstance(data, dict) else None
        if not token:
            # A 200 without an access token is a broker contract violation;
            # keeping the stale token would just fail again downstream.
            raise ValueError(
                f'{self._svc.product} token refresh returned no access token. Please reconnect your Microsoft account.'
            )
        # Validate the expiry BEFORE committing the token: a garbage
        # expiry_date must surface as ValueError, not leave the auth
        # half-updated (new token, stale expiry).
        ms = data.get('expiry_date')
        expiry_ms = None
        if ms:
            try:
                expiry_ms = float(ms)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f'{self._svc.product} token refresh returned an invalid expiry_date ({ms!r}). '
                    'Please reconnect your Microsoft account.'
                ) from exc
        self._token = token
        # A missing expiry must clear the old (past) value, or the fresh token
        # would look expired and re-POST to the broker on every call; None
        # means "not expiring".
        self._expiry_ms = expiry_ms
        return token


def token_scope_report(svc: GraphService, cfg: dict, required: list[str]) -> tuple[set[str], bool, list[str]]:
    """Decode the config's userToken and report (granted, covered, missing) for ``required``.

    The single source of truth for scope diagnostics: ``IGlobal.validateConfig``,
    ``IInstance.check_connection``, and ``build_auth`` all judge scopes through
    this function so the three checks cannot drift. App-only auth carries no
    ``userToken`` (its permissions are app roles, verified by a probe call in
    check_connection), so it always reports (set(), True, []). An absent/empty
    granted set is fail-open: older tokens may lack a scope field, and the
    absence of evidence must not block validation.
    """
    token_str = str(cfg.get('userToken') or '').strip()
    if not token_str:
        return set(), True, []
    info = json.loads(_decode_blob(token_str))
    granted = {s for s in (info.get('scope') or '').split() if s}
    if not granted:
        return granted, True, []
    if granted & svc.superset_scopes:
        return granted, True, []
    missing = missing_scopes(granted, required)
    return granted, not missing, missing


def build_auth(svc: GraphService, auth_type: str, cfg: dict, scopes: list[str]) -> GraphAuth:
    """Build a :class:`GraphAuth` for ``svc`` from node config, scoped to ``scopes``.

    ``auth_type`` selects app-only (tenantId/clientId/clientSecret, client
    credentials) or delegated user OAuth (userToken JSON, broker-refreshed).
    """
    if auth_type == 'user':
        info = json.loads(_decode_blob(str(cfg.get('userToken') or '')))
        access_token = info.get('access_token') or info.get('token') or ''
        resolve_token_uri(svc, info.get('token_uri') or 'https://login.microsoftonline.com/common/oauth2/v2.0/token')
        # Untrusted input: reject any refresh URL not pointing at a known broker.
        broker_url = resolve_refresh_url(svc, info.get('oauth_server_url'))
        refresh_token = info.get('refresh_token')
        expiry_ms = info.get('expiry_date')

        _granted, scopes_covered, missing = token_scope_report(svc, cfg, scopes)
        if not scopes_covered:
            raise ValueError(
                f'{svc.product}: your Microsoft account authorization is missing required scopes '
                'for the selected access tier. Please disconnect and reconnect your '
                f'Microsoft account. Missing: {", ".join(missing)}'
            )

        # Fail fast: if the token is already expired and there is no refresh path
        # (no broker URL and no refresh_token), the first Graph call would fail
        # with a cryptic error. Surface a clear message now.
        try:
            is_expired = expiry_ms is not None and (float(expiry_ms) / 1000.0) < _time.time()
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f'{svc.product}: your Microsoft account authorization has an invalid expiry_date '
                f'({expiry_ms!r}). Please disconnect and reconnect your Microsoft account.'
            ) from exc
        if is_expired and not (broker_url and refresh_token):
            raise ValueError(
                f'{svc.product} access token has expired. Please reconnect your Microsoft account in the node settings.'
            )

        return BrokerUserAuth(svc, access_token, refresh_token, broker_url, expiry_ms)

    for key, title in (('tenantId', 'Tenant ID'), ('clientId', 'Client ID'), ('clientSecret', 'Client Secret')):
        if not str(cfg.get(key) or '').strip():
            raise ValueError(f'{svc.product}: {title} ({key}) is required for App authentication.')
    return AppOnlyAuth(svc, cfg['tenantId'].strip(), cfg['clientId'].strip(), cfg['clientSecret'].strip())


def user_base(cfg: dict) -> str:
    """Return the Graph base path for the acting identity: '/me' (user) or '/users/{upn}' (app-only)."""
    if (str(cfg.get('authType') or 'service')).strip() == 'user':
        return '/me'
    upn = str(cfg.get('userPrincipalName') or '').strip()
    if not upn:
        raise ValueError(
            'Acting User (userPrincipalName) is required for App authentication: app-only Graph calls target /users/{upn}.'
        )
    return f'/users/{upn}'


def request(
    svc: GraphService,
    auth: GraphAuth,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    data: bytes | None = None,
    content_type: str | None = None,
    binary: bool = False,
    extra_headers: dict | None = None,
) -> Any:
    """Run a Graph API request with exponential backoff on 429/5xx.

    ``binary=True`` returns the raw response bytes (download content);
    otherwise the JSON dict (or {} when the body is empty/whitespace). With 4
    attempts the sleeps are 1s, 2s, 4s (or a numeric Retry-After header when
    present) — worst case ~7s before the final raise. ``extra_headers`` are
    merged in after the defaults (e.g. an ``If-Match`` etag on a docx
    round-trip write), so a caller-supplied value can override
    Authorization/Content-Type when it needs to. 401/403 fail fast with an
    agent-readable message naming the consent fix; 409/412 (a stale
    If-Match) fail fast with a conflict message telling the caller to
    re-read and retry; other HTTP errors fail fast with the status and
    Graph error detail.
    """
    url = path if path.startswith('http') else GRAPH_BASE + path
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    base_delay = 1.0
    for attempt in range(4):
        body = json.dumps(json_body).encode() if json_body is not None else data
        headers = {'Authorization': f'Bearer {auth.token()}'}
        headers['Content-Type'] = content_type or (
            'application/json' if json_body is not None else 'application/octet-stream'
        )
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with _urlopen(req, timeout=60) as resp:
                raw = resp.read()
            if binary:
                return raw
            return json.loads(raw.decode()) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status in _RETRY_STATUSES and attempt < 3:
                retry_after = exc.headers.get('Retry-After') if exc.headers else None
                delay = base_delay * (2**attempt)
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        # Graph may send an HTTP-date instead of a delta-seconds
                        # value; fall back to the exponential backoff delay
                        # rather than raising out of the retry loop.
                        pass
                _time.sleep(delay)
                continue
            detail = ''
            try:
                err = (json.loads(exc.read().decode()).get('error')) or {}
                detail = f'{err.get("code", "")}: {err.get("message", "")}'
            except Exception:
                detail = str(exc)
            if status in (401, 403):
                raise GraphError(
                    f'{svc.product}: access denied ({detail}). Check that the granted scopes/'
                    'application permissions cover this operation and that admin consent was granted '
                    '(AADSTS65001 means consent is required). Reconnect your Microsoft account or fix the Entra app.'
                ) from exc
            if status in (409, 412):
                raise GraphError(
                    f'{svc.product}: conflict (HTTP {status}; {detail}) — the file changed while editing; '
                    're-read and retry.'
                ) from exc
            raise GraphError(f'{svc.product}: Graph request failed (HTTP {status}; {detail}).') from exc
    raise RuntimeError('request: retry loop exhausted unexpectedly')  # unreachable
