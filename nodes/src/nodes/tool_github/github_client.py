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
GitHub REST API v3 client.

Thin wrapper around requests — handles auth, headers, path validation, rate-limit
retries, error parsing and response normalisation. All tool methods in IInstance
call through here.

Why this hand-rolls its retry policy instead of using ``ai.common.utils.http_retry``:
that helper retries every 5xx on every method, which on a POST is a duplicate-write
hazard; it cannot see response headers, so ``retry-after`` and ``x-ratelimit-reset``
are unreachable and the wait is a fixed exponential schedule; it never retries 403,
which is the status GitHub actually uses for a rate limit; and it is GET/POST-only
while this client issues PUT, PATCH and DELETE.

Retry policy, evaluated top-down. The ordering is load-bearing:

    429, any method                                  -> retry
    403 + retry-after                                -> retry (secondary limit)
    403 + body message names a rate limit            -> retry
    403 + x-accepted-github-permissions / x-github-sso -> NEVER (permission, SSO)
    403 + x-ratelimit-remaining == 0                 -> retry (primary limit)
    403 otherwise                                    -> NEVER (scope, repo disabled)
    408 / 5xx / transport, GET and HEAD only         -> retry
    408 / 5xx / transport, POST PUT PATCH DELETE     -> NEVER (may have applied)
    any of the above with a wait above the cap       -> NEVER, fail fast

``retry-after`` is read before the permission headers because on a 403 it is only ever
a secondary limit. The permission headers are read before ``x-ratelimit-remaining``
because the ``x-ratelimit-*`` family rides on *every* response, including a plain "you
lack contents:read" 403 — checking ``remaining == 0`` first would sleep and retry a
permanent failure three times.

Retrying a rate-limited POST is safe: a rate limit is enforced at the edge, before the
request is processed, so there is nothing to duplicate. A 502 may already have applied,
which is why 5xx is the one rule that splits on method.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

import requests
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

BASE_URL = 'https://api.github.com'
DEFAULT_TIMEOUT = 30

#: Attempts, not retries: 3 attempts is at most 2 sleeps. A window-based limit clears on
#: the first retry, so a third mostly adds latency.
MAX_ATTEMPTS = 3

#: Hard ceiling on any single sleep. Not arbitrary: 60s is simultaneously the widest window
#: GitHub promises to clear (search is 30/min and code search 10/min, so their
#: x-ratelimit-reset is never more than 60s out) and the minimum GitHub documents for a
#: secondary limit. The core resource is 5,000/hour, so its reset can be ~3,600s out; that
#: is above this cap by design and fails fast naming the reset, because a pipeline thread
#: must not block for an hour.
MAX_RETRY_SLEEP = 60.0

#: Ceiling on the sum of all sleeps within one call(), so a 5xx backoff and a rate-limit
#: wait cannot compound. Worst-case wall clock for a call is therefore
#: MAX_ATTEMPTS * DEFAULT_TIMEOUT (socket) + MAX_TOTAL_RETRY_SLEEP = 180s.
MAX_TOTAL_RETRY_SLEEP = 90.0

#: GitHub's documented floor for a secondary limit that carries no timing header at all.
SECONDARY_LIMIT_FLOOR = 60.0

#: x-ratelimit-reset is an absolute UTC epoch, so a local clock ahead of GitHub's makes the
#: computed wait negative. Sleep this rather than 0: a zero sleep against a window that is
#: still live spends an attempt and learns nothing.
CLOCK_SKEW_FLOOR = 1.0

BACKOFF_MULTIPLIER = 2.0
BACKOFF_MIN = 2.0

#: A 5xx from GitHub's edge is an HTML error page, not JSON. Without a cap the whole page
#: becomes the message an agent reads.
MAX_ERROR_CHARS = 500

MAX_PATH_CHARS = 2048

_REDACTED = '[redacted]'

_SAFE_METHODS = frozenset({'GET', 'HEAD'})
_ALLOWED_METHODS = frozenset({'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'})

#: Matched against an error body's ``message`` field only, never against ``resp.text``. A
#: GitHub error body also carries ``documentation_url``, and on some permission 403s that URL
#: contains the words "rate-limiting" — matching raw text would classify a permanent
#: authorisation failure as a rate limit and retry it, which is the exact bug this guards.
_RATE_LIMIT_MESSAGE_RE = re.compile(r'secondary rate limit|abuse detection|rate limit exceeded', re.IGNORECASE)

#: Headers GitHub attaches to a 403 it is refusing on authorisation grounds.
#: ``x-accepted-oauth-scopes`` is deliberately absent: GitHub sends it on successful responses
#: too, so treating it as a veto would suppress real rate limits.
_PERMISSION_HEADERS = ('x-accepted-github-permissions', 'x-github-sso')

#: Control characters only. A space is NOT rejected: GitHub file paths legitimately contain
#: spaces, urllib3 percent-encodes them, and a space cannot retarget the host. Rejecting it
#: would break file_get on every repository holding a "My Notes.md".
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x1f\x7f]')

_TRAVERSAL_SEGMENTS = frozenset({'..', '%2e%2e', '.%2e', '%2e.'})


class GitHubAPIError(ValueError):
    """Raised when the GitHub API returns an error response.

    A ``ValueError`` subclass on purpose: every one of the curated tools calls :func:`call`
    bare, and any existing ``except ValueError`` upstream must keep catching this. ``str()``
    keeps the exact shape the pre-hardening client raised.

    ``status_code`` is the structured not-found signal. Nothing anywhere may branch on the
    message text to detect a 404.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        errors: list | None = None,
        documentation_url: str = '',
        rate_limited: bool = False,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(f'GitHub API {status_code}: {message}')
        self.status_code = status_code
        self.message = message
        self.errors = list(errors or [])
        self.documentation_url = documentation_url
        self.rate_limited = rate_limited
        self.retry_after_seconds = retry_after_seconds


class _Retry(Exception):
    """Internal control-flow signal. Never escapes :func:`call`.

    Deliberately not a ``ValueError``: a caller's ``except ValueError`` must not be able to
    swallow it, and it must never be mistaken for a :class:`GitHubAPIError`. ``wait_seconds``
    is None when the schedule should fall back to exponential backoff.
    """

    def __init__(self, response: requests.Response | None, wait_seconds: float | None):
        self.response = response
        self.wait_seconds = wait_seconds


# ---------------------------------------------------------------------------
# Credential hygiene
# ---------------------------------------------------------------------------


def _redact(text: Any, token: str) -> str:
    """Replace the credential with a placeholder anywhere it appears in a message.

    The 8-character floor is not cosmetic: without it an empty or one-character token would
    turn every message into ``[redacted]`` between every character.
    """
    out = '' if text is None else str(text)
    secret = (token or '').strip()
    if secret and len(secret) >= 8:
        out = out.replace(secret, _REDACTED)
    return out


#: Public alias, for the one tool outside this module that returns raw file bytes.
redact_text = _redact


def redact_payload(payload: Any, token: str) -> Any:
    """Strip the credential out of a decoded payload, structure preserved.

    For the two tools that return a response this node has not projected through a key
    allowlist: the raw ``request`` escape hatch and ``workflow_get_usage``. Everything else
    projects through a hardcoded ``clean_*`` allowlist, so nothing unexpected can reach the
    agent and a full traversal per call would be pure cost.
    """
    secret = (token or '').strip()
    if not secret or len(secret) < 8:
        return payload
    if isinstance(payload, str):
        return payload.replace(secret, _REDACTED)
    if isinstance(payload, dict):
        return {redact_payload(key, secret): redact_payload(value, secret) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [redact_payload(item, secret) for item in payload]
    return payload


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def _validate_path(path: str) -> str:
    """Reject a path that could retarget the request at another host, then return it.

    :func:`call` builds its URL as ``BASE_URL + path``, so ``path`` controls everything after
    ``https://api.github.com``. That is fine for a path and not fine for a string starting
    with anything else::

        >>> urlsplit('https://api.github.com' + '@evil.com/x').hostname
        'evil.com'

    ``api.github.com`` becomes the *userinfo* of an authority whose host is ``evil.com``, and
    :func:`call` has already attached ``Authorization: Bearer <token>``. ``requests`` strips
    that header across a cross-host redirect but not from the first request, so this leaks the
    credential rather than merely misrouting. Requiring a single leading ``/`` closes it, and
    closes ``.evil.com/x`` (host ``api.github.com.evil.com``) and ``:8080/x`` with it.

    This lives here rather than in the ``request`` tool because :func:`call` is the only choke
    point, and because the curated tools interpolate an agent-supplied ``repo`` and ``path``
    into their own paths too.
    """
    if not isinstance(path, str) or not path:
        raise ValueError('GitHub request: "path" must be a non-empty string, such as "/repos/owner/repo/issues"')
    if len(path) > MAX_PATH_CHARS:
        raise ValueError(f'GitHub request: "path" must be at most {MAX_PATH_CHARS} characters')

    shown = path[:120] + ('...' if len(path) > 120 else '')

    if _CONTROL_CHARS_RE.search(path):
        raise ValueError(f'GitHub request: "path" must not contain control characters: {shown!r}')
    if '\\' in path:
        # WHATWG URL parsers normalise a backslash to a slash, and nothing in the GitHub API
        # needs one, so the only reason to send one is to confuse a parser.
        raise ValueError(f'GitHub request: "path" must not contain a backslash: {shown!r}')
    if '://' in path:
        raise ValueError(
            f'GitHub request: "path" must be an API path such as "/repos/owner/repo/issues", not a full URL: {shown!r}'
        )
    if not path.startswith('/') or path.startswith('//'):
        raise ValueError(
            f'GitHub request: "path" must begin with a single "/", such as "/repos/owner/repo/issues"; '
            f'got {shown!r}. A path beginning with "@", "." or ":" would send this request, and the '
            f'Authorization header with it, to another host.'
        )
    if '?' in path or '#' in path:
        # Not a security hole once the leading "/" is enforced, but a silent correctness one:
        # "/repos/o/r/contents/notes#1.md" reads notes, not notes#1.md, with no error at all.
        raise ValueError(
            f'GitHub request: "path" must not contain "?" or "#"; pass query parameters in "params" instead: {shown!r}'
        )
    if any(segment.lower() in _TRAVERSAL_SEGMENTS for segment in path.split('/')):
        raise ValueError(f'GitHub request: "path" must not contain a ".." segment: {shown!r}')
    return path


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def _hdr(headers: Any, name: str) -> str | None:
    """Case-insensitive header lookup.

    ``requests`` returns a CaseInsensitiveDict, but tests (and the odd caller) pass a plain
    dict, so try the common casings before giving up.
    """
    if not headers:
        return None
    for candidate in (name, name.lower(), name.upper(), name.title()):
        value = headers.get(candidate)
        if value is not None:
            return value
    return None


def _float_hdr(headers: Any, name: str) -> float | None:
    """Read a header as a finite float, or None when absent or unusable.

    ``nan`` and ``inf`` parse fine through ``float()`` and would poison every arithmetic
    comparison downstream, so they are treated as an absent header.
    """
    raw = _hdr(headers, name)
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


def _is_permission_denial(resp: requests.Response) -> bool:
    """Whether a 403 names an authorisation problem rather than a budget one."""
    return any(_hdr(resp.headers, name) for name in _PERMISSION_HEADERS)


def _mentions_rate_limit(payload: Any) -> bool:
    """Whether the parsed error body says this is a rate limit. See _RATE_LIMIT_MESSAGE_RE."""
    if not isinstance(payload, dict):
        return False
    message = payload.get('message')
    return isinstance(message, str) and bool(_RATE_LIMIT_MESSAGE_RE.search(message))


def _reset_wait(headers: Any, now: float) -> float | None:
    """Seconds until ``x-ratelimit-reset``, or None when the header is absent or unusable."""
    reset = _float_hdr(headers, 'x-ratelimit-reset')
    if reset is None:
        return None
    return max(CLOCK_SKEW_FLOOR, reset - now)


def _rate_limit_wait(resp: requests.Response, payload: Any, *, now: float | None = None) -> float | None:
    """Seconds GitHub asked us to wait, or None when this response is not a rate limit.

    Returns the TRUE wait, uncapped. Capping here would silently retry a 45-minute primary
    limit after 60s and spend every attempt on a request that cannot succeed; :func:`call`
    owns the cap and fails fast above it.
    """
    if resp.status_code not in (403, 429):
        return None
    now = time.time() if now is None else now
    headers = resp.headers

    retry_after = _float_hdr(headers, 'retry-after')
    if retry_after is not None:
        return max(0.0, retry_after)

    if resp.status_code == 403:
        if _mentions_rate_limit(payload):
            wait = _reset_wait(headers, now)
            return wait if wait is not None else SECONDARY_LIMIT_FLOOR
        if _is_permission_denial(resp):
            return None
        if _float_hdr(headers, 'x-ratelimit-remaining') == 0:
            wait = _reset_wait(headers, now)
            return wait if wait is not None else SECONDARY_LIMIT_FLOOR
        return None

    # 429 is a rate limit by status alone, whatever else it does or does not carry.
    if _float_hdr(headers, 'x-ratelimit-remaining') == 0:
        wait = _reset_wait(headers, now)
        if wait is not None:
            return wait
    return SECONDARY_LIMIT_FLOOR


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _error_payload(resp: requests.Response) -> Any:
    """Parsed error body, or None when it is not JSON."""
    try:
        return resp.json()
    except Exception:
        return None


def _error_message(payload: Any, resp: requests.Response) -> str:
    """Pull a message out of an error response, preserving the original wording."""
    if isinstance(payload, dict):
        msg = payload.get('message', resp.text)
        errors = payload.get('errors')
        if errors:
            msg = f'{msg} — ' + '; '.join(e.get('message', str(e)) if isinstance(e, dict) else str(e) for e in errors)
        return str(msg)[:MAX_ERROR_CHARS]
    return (resp.text or resp.reason or 'unknown error')[:MAX_ERROR_CHARS]


def _raise_github_error(
    resp: requests.Response,
    payload: Any,
    token: str,
    *,
    over_cap_wait: float | None = None,
) -> None:
    """Raise :class:`GitHubAPIError` for a failed response."""
    message = _error_message(payload, resp)

    # The rate-limit clause belongs on a rate-limit error and nowhere else. The
    # x-ratelimit-* family is on every response, so appending it unconditionally would put
    # "resets in ~2612s" on every 404 and invite the agent to sleep and retry something that
    # will never succeed.
    if over_cap_wait is not None:
        resource = _hdr(resp.headers, 'x-ratelimit-resource') or 'this'
        message = (
            f'{message} (the {resource} rate limit is exhausted and resets in ~{over_cap_wait:.0f}s, '
            f'longer than the {MAX_RETRY_SLEEP:.0f}s this client is willing to wait, so it did not retry)'
        )

    documentation_url = ''
    errors: list = []
    if isinstance(payload, dict):
        documentation_url = str(payload.get('documentation_url') or '')
        raw_errors = payload.get('errors')
        if isinstance(raw_errors, list):
            errors = raw_errors

    raise GitHubAPIError(
        resp.status_code,
        _redact(message, token),
        errors=errors,
        documentation_url=documentation_url,
        rate_limited=over_cap_wait is not None,
        retry_after_seconds=over_cap_wait,
    )


def _decode(resp: requests.Response, token: str) -> Any:
    """Parse a successful response. Empty bodies become {}, and a JSON null becomes {}.

    The empty-body branch also fixes a latent bug: ``resp.json()`` on an empty 2xx raised a
    ``requests.exceptions.JSONDecodeError`` whose message an agent could do nothing with.
    """
    if resp.status_code in (204, 205) or not (resp.content or b'').strip():
        return {}
    try:
        payload = resp.json()
    except ValueError as exc:
        text = _redact(resp.text, token)[:MAX_ERROR_CHARS]
        raise GitHubAPIError(resp.status_code, f'response was not JSON: {text}') from exc
    return {} if payload is None else payload


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def call(
    token: str,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    base_url: str = BASE_URL,
    not_found_ok: bool = False,
) -> Any:
    """Make an authenticated GitHub API call and return parsed JSON.

    Raises :class:`GitHubAPIError` (a ``ValueError`` subclass, so existing ``except
    ValueError`` handlers still catch it) on HTTP errors. Returns an empty dict for 204 and
    for any 2xx with an empty body. With ``not_found_ok=True`` a 404 returns ``None`` instead
    of raising; every other status behaves identically.

    Retries per the table in the module docstring. At most :data:`MAX_ATTEMPTS` attempts, no
    single sleep above :data:`MAX_RETRY_SLEEP` and no more than :data:`MAX_TOTAL_RETRY_SLEEP`
    of sleep in total, so this cannot hold a pipeline thread for a rate-limit window an hour
    out.
    """
    verb = (method or '').strip().upper()
    if verb not in _ALLOWED_METHODS:
        raise ValueError(
            f'GitHub request: unsupported HTTP method {method!r}; expected one of {", ".join(sorted(_ALLOWED_METHODS))}'
        )
    safe = verb in _SAFE_METHODS
    url = base_url + _validate_path(path)
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    query = {k: v for k, v in (params or {}).items() if v is not None}
    slept = 0.0

    def _attempt() -> Any:
        try:
            resp = requests.request(verb, url, headers=headers, params=query, json=body, timeout=timeout)
        except requests.RequestException as exc:
            if safe and isinstance(exc, (requests.Timeout, requests.ConnectionError)):
                raise _Retry(None, None) from exc
            raise ValueError(_redact(f'GitHub request failed: {exc}', token)) from exc

        if resp.ok:
            return _decode(resp, token)

        payload = _error_payload(resp)

        wait = _rate_limit_wait(resp, payload)
        if wait is not None:
            if wait <= MAX_RETRY_SLEEP and wait <= MAX_TOTAL_RETRY_SLEEP - slept:
                raise _Retry(resp, wait)
            # Over the cap. Retrying would block the pipeline thread longer than any pipeline
            # should wait, so surface it with the reset in the message and let the agent decide.
            _raise_github_error(resp, payload, token, over_cap_wait=wait)

        if safe and (resp.status_code >= 500 or resp.status_code == 408):
            raise _Retry(resp, None)

        if not_found_ok and resp.status_code == 404:
            return None

        _raise_github_error(resp, payload, token)

    def _wait(retry_state: RetryCallState) -> float:
        nonlocal slept
        exc = retry_state.outcome.exception()
        seconds = getattr(exc, 'wait_seconds', None)
        if seconds is None:
            seconds = float(
                wait_exponential(multiplier=BACKOFF_MULTIPLIER, min=BACKOFF_MIN, max=MAX_RETRY_SLEEP)(retry_state)
            )
        seconds = max(0.0, min(seconds, MAX_RETRY_SLEEP, MAX_TOTAL_RETRY_SLEEP - slept))
        slept += seconds
        return seconds

    try:
        return Retrying(
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=_wait,
            retry=retry_if_exception_type(_Retry),
            reraise=True,
        )(_attempt)
    except _Retry as exc:
        # Attempts exhausted. _Retry must never reach a caller, so re-raise the real failure.
        if exc.response is not None:
            _raise_github_error(exc.response, _error_payload(exc.response), token)
        cause = exc.__cause__
        raise ValueError(_redact(f'GitHub request failed: {cause}', token)) from cause


# ---------------------------------------------------------------------------
# Response cleaners — strip noisy fields (node_id, _links, gravatar, etc.)
# so agents get compact, useful output.
# ---------------------------------------------------------------------------


def clean_user(u: dict | None) -> dict:
    if not isinstance(u, dict):
        return {}
    return {k: u[k] for k in ('login', 'id', 'avatar_url', 'html_url') if k in u}


def clean_label(lbl: dict | None) -> dict:
    if not isinstance(lbl, dict):
        return {}
    return {k: lbl[k] for k in ('id', 'name', 'color', 'description') if k in lbl}


def clean_issue(issue: dict) -> dict:
    return {
        'number': issue.get('number'),
        'title': issue.get('title'),
        'body': issue.get('body'),
        'state': issue.get('state'),
        'labels': [clean_label(lbl) for lbl in (issue.get('labels') or [])],
        'user': clean_user(issue.get('user')),
        'assignees': [clean_user(a) for a in (issue.get('assignees') or [])],
        'created_at': issue.get('created_at'),
        'updated_at': issue.get('updated_at'),
        'closed_at': issue.get('closed_at'),
        'html_url': issue.get('html_url'),
        'comments': issue.get('comments'),
    }


def clean_pr(pr: dict) -> dict:
    head = pr.get('head') or {}
    base = pr.get('base') or {}
    return {
        'number': pr.get('number'),
        'title': pr.get('title'),
        'body': pr.get('body'),
        'state': pr.get('state'),
        'merged': pr.get('merged', False),
        'draft': pr.get('draft', False),
        'head': {'ref': head.get('ref'), 'sha': head.get('sha')},
        'base': {'ref': base.get('ref'), 'sha': base.get('sha')},
        'user': clean_user(pr.get('user')),
        'created_at': pr.get('created_at'),
        'updated_at': pr.get('updated_at'),
        'merged_at': pr.get('merged_at'),
        'html_url': pr.get('html_url'),
        'commits': pr.get('commits'),
        'additions': pr.get('additions'),
        'deletions': pr.get('deletions'),
        'changed_files': pr.get('changed_files'),
    }


def clean_file_entry(f: dict) -> dict:
    return {k: f[k] for k in ('name', 'path', 'type', 'sha', 'size', 'download_url') if k in f}


def clean_commit(c: dict) -> dict:
    commit = c.get('commit') or {}
    author = commit.get('author') or {}
    return {
        'sha': c.get('sha'),
        'message': commit.get('message'),
        'author': author.get('name'),
        'date': author.get('date'),
        'html_url': c.get('html_url'),
    }


def clean_release(r: dict) -> dict:
    return {
        'id': r.get('id'),
        'tag_name': r.get('tag_name'),
        'name': r.get('name'),
        'body': r.get('body'),
        'draft': r.get('draft'),
        'prerelease': r.get('prerelease'),
        'created_at': r.get('created_at'),
        'published_at': r.get('published_at'),
        'html_url': r.get('html_url'),
        'author': clean_user(r.get('author')),
    }


def clean_workflow(w: dict) -> dict:
    return {k: w[k] for k in ('id', 'name', 'path', 'state', 'created_at', 'updated_at', 'html_url') if k in w}


def clean_repo(r: dict) -> dict:
    return {
        'id': r.get('id'),
        'full_name': r.get('full_name'),
        'description': r.get('description'),
        'private': r.get('private'),
        'fork': r.get('fork'),
        'default_branch': r.get('default_branch'),
        'language': r.get('language'),
        'stargazers_count': r.get('stargazers_count'),
        'forks_count': r.get('forks_count'),
        'open_issues_count': r.get('open_issues_count'),
        'created_at': r.get('created_at'),
        'updated_at': r.get('updated_at'),
        'pushed_at': r.get('pushed_at'),
        'html_url': r.get('html_url'),
        'clone_url': r.get('clone_url'),
    }
