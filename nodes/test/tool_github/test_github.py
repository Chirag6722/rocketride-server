"""
Unit tests for tool_github.

Covers the hardened client (path validation, rate-limit classification, the retry
driver and its caps, graceful 404, credential redaction, backward compatibility) and
the publication filter (tool groups, read-only hiding, the raw request tool). No
credentials and no network access are needed.

The framework is stubbed rather than imported, so the node loads without the engine.
``rocketlib`` and ``ai.common.config`` are hand-written stand-ins because the real ones
need the native engine, but the argument validators are not:
``packages/ai/src/ai/common/utils/tool_args.py`` is loaded from its source file, so the
normalisation tests below exercise the shipped helpers rather than a copy that stops
tracking them the moment the real module changes.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import time
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src' / 'nodes'))

_STUB_MODULE_NAMES = ('rocketlib', 'ai', 'ai.common', 'ai.common.config', 'ai.common.utils')

#: The real argument validators, loaded from source in _install_stubs.
_TOOL_ARGS = (
    Path(__file__).resolve().parents[3] / 'packages' / 'ai' / 'src' / 'ai' / 'common' / 'utils' / 'tool_args.py'
)

_NODE_DIR = Path(__file__).resolve().parents[2] / 'src' / 'nodes' / 'tool_github'
_IINSTANCE_PY = _NODE_DIR / 'IInstance.py'
_SERVICES_JSON = _NODE_DIR / 'services.json'

# Obviously fake. A real GitHub PAT is "ghp_" plus 36 characters, but a literal one here
# trips secret scanners, so use a placeholder long enough that _redact fires (it ignores
# anything under 8 characters).
TEST_TOKEN = 'github-test-not-a-real-token'

REPO = 'acme/app'

#: Written out literally rather than imported from the node. A test that derives its
#: expectation from the thing it is checking passes vacuously.
WRITE_TOOLS = frozenset(
    {
        'file_create',
        'file_delete',
        'file_edit',
        'issue_comment',
        'issue_create',
        'issue_edit',
        'issue_lock',
        'pr_create',
        'release_create',
        'release_delete',
        'release_update',
        'review_create',
        'review_update',
        'user_invite',
        'workflow_disable',
        'workflow_dispatch',
        'workflow_enable',
    }
)

READ_TOOLS = frozenset(
    {
        'commit_get',
        'commit_list',
        'file_get',
        'file_list',
        'issue_get',
        'issue_list',
        'org_list_repos',
        'pr_get',
        'pr_list',
        'release_get',
        'release_list',
        'repo_get',
        'review_get',
        'review_list',
        'search_code',
        'search_issues',
        'user_get_repos',
        'workflow_get',
        'workflow_get_usage',
        'workflow_list',
    }
)

#: Pins the README tables and the services.json description prose.
EXPECTED_GROUP_COUNTS = {
    'files': 5,
    'issues': 6,
    'org_members': 1,
    'pull_requests': 7,
    'releases': 5,
    'repos': 5,
    'search': 2,
    'workflows': 6,
}


# ---------------------------------------------------------------------------
# Framework stubs
# ---------------------------------------------------------------------------


def _install_stubs() -> None:
    mod_rl = types.ModuleType('rocketlib')

    def mock_tool_function(*args, **kwargs):
        def decorator(fn):
            fn.__tool_meta__ = kwargs
            return fn

        return decorator

    mod_rl.tool_function = mock_tool_function

    class IInstanceBase:
        """Mirrors rocketlib.filters.IInstanceBase._collect_tool_methods.

        Reproduced rather than stubbed away because the node overrides this method and
        calls ``super()``, so the publication tests below need a real one to build on.
        """

        def _collect_tool_methods(self):
            methods = {}
            for name in dir(type(self)):
                attr = getattr(type(self), name, None)
                if attr is not None and hasattr(attr, '__tool_meta__'):
                    methods[name] = getattr(self, name)
            return methods

    class IGlobalBase:
        pass

    mod_rl.IInstanceBase = IInstanceBase
    mod_rl.IGlobalBase = IGlobalBase
    mod_rl.OPEN_MODE = Mock()
    mod_rl.warning = Mock()
    sys.modules['rocketlib'] = mod_rl

    sys.modules['ai'] = types.ModuleType('ai')
    sys.modules['ai.common'] = types.ModuleType('ai.common')

    mod_config = types.ModuleType('ai.common.config')

    class Config:
        pass

    mod_config.Config = Config
    sys.modules['ai.common.config'] = mod_config

    # The real validators, loaded from their source file rather than re-implemented: a
    # hand-written copy stops tracking the helper the moment it changes. rocketlib must
    # already be in sys.modules here, because tool_args imports warning from it.
    spec = importlib.util.spec_from_file_location('ai.common.utils', _TOOL_ARGS)
    mod_utils = importlib.util.module_from_spec(spec)
    sys.modules['ai.common.utils'] = mod_utils
    spec.loader.exec_module(mod_utils)


@contextmanager
def _scoped_stubs() -> Iterator[None]:
    original = {name: sys.modules.get(name) for name in _STUB_MODULE_NAMES}
    _install_stubs()
    try:
        yield
    finally:
        for name, module in original.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


with _scoped_stubs():
    from tool_github.github_client import (
        CLOCK_SKEW_FLOOR,
        MAX_ATTEMPTS,
        MAX_RETRY_SLEEP,
        MAX_TOTAL_RETRY_SLEEP,
        SECONDARY_LIMIT_FLOOR,
        GitHubAPIError,
        _rate_limit_wait,
        _validate_path,
        call,
        redact_payload,
        redact_text,
    )
    from tool_github.IInstance import IInstance
    from tool_github.tool_groups import (
        ALL_GROUPS,
        DEFAULT_GROUPS,
        RAW_REQUEST_TOOL,
        github_tool,
        normalize_groups,
        tool_counts_by_group,
        unknown_groups,
    )

_CLIENT = 'tool_github.github_client.requests.request'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Sentinel: ``json_data`` left unset means the body does not parse as JSON at all, which
#: is distinct from a body that parses to ``null``. Both are real GitHub responses.
_NO_JSON = object()


def _resp(status=200, *, json_data=_NO_JSON, headers=None, text='', content=b'{}', reason='reason'):
    resp = Mock(spec=requests.Response)
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.headers = headers or {}
    resp.text = text
    resp.content = content
    resp.reason = reason
    if json_data is _NO_JSON:
        resp.json.side_effect = ValueError('no json')
    else:
        resp.json.return_value = json_data
    return resp


def _instance(**overrides):
    """Build an IInstance without running the engine lifecycle."""
    inst = IInstance.__new__(IInstance)
    glob = Mock()
    glob.token = TEST_TOKEN
    glob.default_repo = REPO
    glob.read_only = False
    glob.tool_groups = DEFAULT_GROUPS
    glob.allow_raw_request = True
    for key, value in overrides.items():
        setattr(glob, key, value)
    inst.IGlobal = glob
    return inst


def _tool_attrs():
    """Every published-capable tool on the class, as (name, class attribute)."""
    out = []
    for name in dir(IInstance):
        attr = getattr(IInstance, name, None)
        if attr is not None and hasattr(attr, '__tool_meta__'):
            out.append((name, attr))
    return out


def _require_write_callers() -> set[str]:
    """Method names whose body calls ``self._require_write()``, read from the source.

    The flat single-module layout is what makes this one ``ast.parse`` rather than a
    directory walk.
    """
    tree = ast.parse(_IINSTANCE_PY.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'IInstance')
    found: set[str] = set()
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == '_require_write'
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'self'
            ):
                found.add(fn.name)
    return found


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


class TestPathValidation:
    """``call`` builds its URL as BASE_URL + path, so path controls the authority."""

    @patch(_CLIENT)
    def test_userinfo_injection_is_rejected_before_any_request(self, mock_request):
        """The credential-leak regression.

        ``'https://api.github.com' + '@evil.com/x'`` parses with host ``evil.com``, and
        the Authorization header is attached before the URL is built, so this would hand
        the token to an attacker-chosen host. It must fail before the socket opens.
        """
        with pytest.raises(ValueError, match='must begin with a single'):
            call(TEST_TOKEN, 'GET', '@evil.com/x')
        mock_request.assert_not_called()

    @pytest.mark.parametrize(
        'path',
        [
            '@evil.com/x',
            '.evil.com/x',
            ':8080/x',
            '//evil.com/x',
            'https://evil.com/x',
            'evil',
            '',
            '\\evil.com',
            '/x\r\nHost: evil.com',
            '/x\x00',
            '/repos/../../x',
            '/repos/%2E%2E/x',
            '/repos/o/r/issues?state=open',
            '/repos/o/r/contents/notes#1.md',
            '/' + 'a' * 3000,
        ],
    )
    @patch(_CLIENT)
    def test_dangerous_paths_are_rejected(self, mock_request, path):
        with pytest.raises(ValueError):
            call(TEST_TOKEN, 'GET', path)
        mock_request.assert_not_called()

    @pytest.mark.parametrize(
        'path',
        [
            '/repos/o/r',
            '/search/code',
            '/user/repos',
            '/repos/o/r/contents/My Notes.md',
            '/repos/o/r/contents/a.b.c',
            '/repos/o/r/contents/v1.2@3',
            '/repos/o/r/./x',
        ],
    )
    def test_legitimate_paths_are_accepted(self, path):
        """A space in particular must stay legal, or file_get breaks on "My Notes.md"."""
        assert _validate_path(path) == path

    def test_query_string_error_names_the_params_argument(self):
        """So an agent that writes the query into the path self-corrects on the first try."""
        with pytest.raises(ValueError, match='"params"'):
            _validate_path('/repos/o/r/issues?state=open')

    @patch(_CLIENT)
    def test_an_unsupported_method_is_rejected(self, mock_request):
        with pytest.raises(ValueError, match='unsupported HTTP method'):
            call(TEST_TOKEN, 'TRACE', '/repos/o/r')
        mock_request.assert_not_called()


# ---------------------------------------------------------------------------
# Rate-limit classification
# ---------------------------------------------------------------------------


class TestRateLimitClassification:
    NOW = 1000.0

    def _wait(self, status, headers=None, payload=None):
        return _rate_limit_wait(_resp(status, headers=headers), payload, now=self.NOW)

    def test_bare_429_uses_the_documented_floor(self):
        assert self._wait(429) == SECONDARY_LIMIT_FLOOR

    @pytest.mark.parametrize('status', [403, 429])
    def test_retry_after_wins_outright(self, status):
        assert self._wait(status, {'retry-after': '12'}) == 12.0

    def test_primary_limit_waits_until_reset(self):
        assert self._wait(403, {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1045'}) == 45.0

    def test_the_true_wait_is_returned_uncapped(self):
        """call() owns the cap. Capping here would retry a 45-minute limit after 60s."""
        assert self._wait(403, {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '4600'}) == 3600.0

    @pytest.mark.parametrize('header', ['x-accepted-github-permissions', 'x-github-sso'])
    def test_a_permission_header_vetoes_an_exhausted_budget(self, header):
        """The regression test for "it retried every 403".

        x-ratelimit-* rides on every response including a permission denial, so reading
        remaining == 0 first would sleep and retry a permanent failure three times.
        """
        headers = {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1045', header: 'contents=read'}
        assert self._wait(403, headers) is None

    def test_retry_after_is_not_vetoed_by_a_permission_header(self):
        """Ordering: retry-after on a 403 is only ever a secondary limit."""
        assert self._wait(403, {'retry-after': '8', 'x-accepted-github-permissions': 'contents=read'}) == 8.0

    def test_a_secondary_limit_message_is_honoured(self):
        assert self._wait(403, {}, {'message': 'You have exceeded a secondary rate limit'}) == SECONDARY_LIMIT_FLOOR

    def test_a_documentation_url_mentioning_rate_limiting_does_not_count(self):
        """Match the body's message field, never resp.text.

        A permission 403 carries documentation_url ending "#rate-limiting"; matching raw
        text would reclassify a permanent authorisation failure as retryable.
        """
        payload = {
            'message': 'Resource not accessible by integration',
            'documentation_url': 'https://docs.github.com/rest/overview/#rate-limiting',
        }
        assert self._wait(403, {'x-ratelimit-remaining': '4999'}, payload) is None

    def test_a_plain_permission_403_is_not_a_rate_limit(self):
        assert self._wait(403, {'x-ratelimit-remaining': '4999'}) is None

    @pytest.mark.parametrize('status', [200, 404, 422, 500])
    def test_other_statuses_are_never_rate_limits(self, status):
        assert self._wait(status, {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '4600'}) is None

    def test_a_clock_ahead_of_github_floors_rather_than_going_negative(self):
        """A zero sleep against a still-live window spends an attempt and learns nothing."""
        assert self._wait(429, {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '900'}) == CLOCK_SKEW_FLOOR

    @pytest.mark.parametrize('value', ['nan', 'inf', '-inf', 'not-a-number', ''])
    def test_an_unusable_reset_header_is_treated_as_absent(self, value):
        headers = {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': value}
        assert self._wait(429, headers) == SECONDARY_LIMIT_FLOOR

    def test_an_http_date_retry_after_falls_through_rather_than_becoming_zero(self):
        headers = {'retry-after': 'Wed, 21 Oct 2026 07:28:00 GMT', 'x-ratelimit-remaining': '0'}
        assert self._wait(429, headers) == SECONDARY_LIMIT_FLOOR


# ---------------------------------------------------------------------------
# Retry driver
# ---------------------------------------------------------------------------


class TestRetryDriver:
    @patch('time.sleep')
    @patch(_CLIENT)
    def test_a_rate_limit_is_retried_then_succeeds(self, mock_request, mock_sleep):
        mock_request.side_effect = [
            _resp(429, headers={'retry-after': '1'}, json_data={'message': 'Too Many Requests'}),
            _resp(200, json_data={'ok': True}),
        ]
        assert call(TEST_TOKEN, 'GET', '/repos/o/r') == {'ok': True}
        assert mock_request.call_count == 2
        mock_sleep.assert_called_once_with(1.0)

    @patch('time.sleep')
    @patch(_CLIENT)
    def test_attempts_are_capped_and_the_real_error_surfaces(self, mock_request, mock_sleep):
        """_Retry must never reach the caller."""
        mock_request.side_effect = [
            _resp(429, headers={'retry-after': '1'}, json_data={'message': 'Too Many Requests'})
        ] * MAX_ATTEMPTS
        with pytest.raises(GitHubAPIError) as exc:
            call(TEST_TOKEN, 'GET', '/repos/o/r')
        assert exc.value.status_code == 429
        assert mock_request.call_count == MAX_ATTEMPTS

    @patch('time.sleep')
    @patch(_CLIENT)
    def test_a_wait_above_the_cap_fails_fast_without_sleeping(self, mock_request, mock_sleep):
        """The unbounded-sleep regression.

        A core limit resets up to an hour out. Sleeping on it would hold a pipeline
        thread for that hour, so the call fails immediately and names the reset instead.
        """
        headers = {
            'x-ratelimit-remaining': '0',
            'x-ratelimit-reset': str(int(time.time()) + 3600),
            'x-ratelimit-resource': 'core',
        }
        mock_request.return_value = _resp(403, headers=headers, json_data={'message': 'API rate limit exceeded'})
        with pytest.raises(GitHubAPIError) as exc:
            call(TEST_TOKEN, 'GET', '/repos/o/r')
        assert mock_request.call_count == 1
        mock_sleep.assert_not_called()
        assert exc.value.rate_limited is True
        assert 'core' in str(exc.value)
        assert 'did not retry' in str(exc.value)

    @patch('time.sleep')
    @patch(_CLIENT)
    def test_total_sleep_is_bounded(self, mock_request, mock_sleep):
        mock_request.side_effect = [
            _resp(429, headers={'retry-after': '60'}, json_data={'message': 'Too Many Requests'})
        ] * MAX_ATTEMPTS
        with pytest.raises(GitHubAPIError):
            call(TEST_TOKEN, 'GET', '/repos/o/r')
        total = sum(c.args[0] for c in mock_sleep.call_args_list)
        assert total <= MAX_TOTAL_RETRY_SLEEP
        assert all(c.args[0] <= MAX_RETRY_SLEEP for c in mock_sleep.call_args_list)

    @patch('time.sleep')
    @patch(_CLIENT)
    def test_a_permission_403_is_never_retried(self, mock_request, mock_sleep):
        mock_request.return_value = _resp(
            403,
            headers={'x-accepted-github-permissions': 'contents=read'},
            json_data={'message': 'Resource not accessible by integration'},
        )
        with pytest.raises(GitHubAPIError):
            call(TEST_TOKEN, 'GET', '/repos/o/r')
        assert mock_request.call_count == 1
        mock_sleep.assert_not_called()

    @patch('time.sleep')
    @patch(_CLIENT)
    def test_5xx_is_retried_on_a_read(self, mock_request, mock_sleep):
        mock_request.side_effect = [_resp(502), _resp(200, json_data={'ok': True})]
        assert call(TEST_TOKEN, 'GET', '/repos/o/r') == {'ok': True}
        assert mock_request.call_count == 2

    @pytest.mark.parametrize(
        'method,status',
        [('POST', 500), ('PATCH', 502), ('DELETE', 503), ('PUT', 504)],
    )
    @patch('time.sleep')
    @patch(_CLIENT)
    def test_5xx_is_never_retried_on_a_write(self, mock_request, mock_sleep, method, status):
        """A 502 may already have applied; retrying a POST could create the record twice."""
        mock_request.return_value = _resp(status)
        with pytest.raises(GitHubAPIError):
            call(TEST_TOKEN, method, '/repos/o/r/issues', body={})
        assert mock_request.call_count == 1
        mock_sleep.assert_not_called()

    @patch('time.sleep')
    @patch(_CLIENT)
    def test_a_transport_failure_is_retried_on_a_read_only(self, mock_request, mock_sleep):
        mock_request.side_effect = [requests.ConnectionError('boom'), _resp(200, json_data={'ok': True})]
        assert call(TEST_TOKEN, 'GET', '/repos/o/r') == {'ok': True}

        mock_request.reset_mock()
        mock_request.side_effect = requests.ConnectionError('boom')
        with pytest.raises(ValueError):
            call(TEST_TOKEN, 'POST', '/repos/o/r/issues', body={})
        assert mock_request.call_count == 1

    @pytest.mark.parametrize('status', [401, 404, 409, 422])
    @patch('time.sleep')
    @patch(_CLIENT)
    def test_client_errors_are_never_retried(self, mock_request, mock_sleep, status):
        mock_request.return_value = _resp(status, json_data={'message': 'nope'})
        with pytest.raises(GitHubAPIError):
            call(TEST_TOKEN, 'GET', '/repos/o/r')
        assert mock_request.call_count == 1
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Graceful 404
# ---------------------------------------------------------------------------


class TestNotFound:
    @patch(_CLIENT)
    def test_not_found_ok_returns_none(self, mock_request):
        mock_request.return_value = _resp(404, json_data={'message': 'Not Found'})
        assert call(TEST_TOKEN, 'GET', '/repos/o/r', not_found_ok=True) is None

    @patch(_CLIENT)
    def test_the_default_still_raises_with_a_structured_status(self, mock_request):
        """The signal is an int, never a substring match on the message."""
        mock_request.return_value = _resp(404, json_data={'message': 'Not Found'})
        with pytest.raises(GitHubAPIError) as exc:
            call(TEST_TOKEN, 'GET', '/repos/o/r')
        assert exc.value.status_code == 404

    @patch(_CLIENT)
    def test_not_found_ok_does_not_soften_any_other_status(self, mock_request):
        mock_request.return_value = _resp(403, json_data={'message': 'Forbidden'})
        with pytest.raises(GitHubAPIError):
            call(TEST_TOKEN, 'GET', '/repos/o/r', not_found_ok=True)

    @patch('tool_github.IInstance.call')
    def test_file_get_reports_a_miss_without_over_claiming(self, mock_call):
        """GitHub answers 404 the same way for four different causes.

        Saying "the file does not exist" would assert one of them.
        """
        mock_call.return_value = None
        out = _instance().file_get({'path': 'docs/missing.md', 'ref': 'main'})
        assert out['found'] is False
        assert out['repo'] == REPO
        assert out['path'] == 'docs/missing.md'
        assert out['ref'] == 'main'
        assert 'does not exist or is not visible' in out['message']
        assert 'file_list' in out['message'] and 'repo_get' in out['message']

    @patch('tool_github.IInstance.call')
    def test_file_get_marks_a_hit(self, mock_call):
        mock_call.return_value = {'path': 'a.md', 'name': 'a.md', 'sha': 's', 'size': 1, 'content': 'aGk='}
        out = _instance().file_get({'path': 'a.md'})
        assert out['found'] is True
        assert out['content'] == 'hi'


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_the_token_is_replaced_in_a_message(self):
        assert TEST_TOKEN not in redact_text(f'failed for {TEST_TOKEN}', TEST_TOKEN)

    @pytest.mark.parametrize('token', ['', 'short'])
    def test_a_short_token_never_blanks_the_text(self, token):
        """Without the floor, a one-character token blanks between every character."""
        assert redact_text('hello world', token) == 'hello world'
        assert redact_payload({'a': 'hello'}, token) == {'a': 'hello'}

    def test_payload_redaction_preserves_structure(self):
        payload = {TEST_TOKEN: [{'v': TEST_TOKEN}, (TEST_TOKEN, 1)], 'n': None, 'i': 3, 'b': True}
        out = redact_payload(payload, TEST_TOKEN)
        assert TEST_TOKEN not in json.dumps(out, default=str)
        assert out['n'] is None and out['i'] == 3 and out['b'] is True

    @patch(_CLIENT)
    def test_an_echoed_token_is_redacted_out_of_an_error(self, mock_request):
        mock_request.return_value = _resp(422, json_data={'message': f'bad token {TEST_TOKEN}'})
        with pytest.raises(GitHubAPIError) as exc:
            call(TEST_TOKEN, 'GET', '/repos/o/r')
        assert TEST_TOKEN not in str(exc.value)

    @patch('tool_github.IInstance.call')
    def test_file_content_carrying_the_token_is_redacted(self, mock_call):
        """A token committed to a repo is exactly what an agent gets pointed at."""
        import base64

        blob = base64.b64encode(f'SECRET={TEST_TOKEN}'.encode()).decode()
        mock_call.return_value = {'path': 'a.env', 'content': blob}
        assert TEST_TOKEN not in _instance().file_get({'path': 'a.env'})['content']


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_the_error_is_a_value_error(self):
        """All 37 curated tools call `call` bare; nothing may start escaping them."""
        assert isinstance(GitHubAPIError(404, 'Not Found'), ValueError)

    def test_the_message_shape_is_unchanged(self):
        assert str(GitHubAPIError(422, 'Validation Failed')) == 'GitHub API 422: Validation Failed'

    @patch(_CLIENT)
    def test_an_existing_except_value_error_still_catches(self, mock_request):
        mock_request.return_value = _resp(404, json_data={'message': 'Not Found'})
        with pytest.raises(ValueError):
            call(TEST_TOKEN, 'GET', '/repos/o/r')

    @patch(_CLIENT)
    def test_the_errors_list_is_still_joined_into_the_message(self, mock_request):
        payload = {'message': 'Validation Failed', 'errors': [{'message': 'x'}, 'y']}
        mock_request.return_value = _resp(422, json_data=payload)
        with pytest.raises(GitHubAPIError) as exc:
            call(TEST_TOKEN, 'GET', '/repos/o/r')
        assert 'x' in str(exc.value) and 'y' in str(exc.value)

    @pytest.mark.parametrize('status,content', [(204, b''), (200, b''), (200, b'   ')])
    @patch(_CLIENT)
    def test_an_empty_body_becomes_an_empty_dict(self, mock_request, status, content):
        """A 2xx with an empty body used to raise an unreadable JSONDecodeError."""
        mock_request.return_value = _resp(status, content=content)
        assert call(TEST_TOKEN, 'GET', '/repos/o/r') == {}

    @patch(_CLIENT)
    def test_a_json_null_body_becomes_an_empty_dict(self, mock_request):
        """Distinct from an unparseable body: this one parses, to None."""
        mock_request.return_value = _resp(200, json_data=None, content=b'null')
        assert call(TEST_TOKEN, 'GET', '/repos/o/r') == {}

    @patch(_CLIENT)
    def test_a_rate_limit_clause_stays_off_an_unrelated_error(self, mock_request):
        """x-ratelimit-* is on every response; a 404 must not invite a sleep-and-retry."""
        headers = {'x-ratelimit-remaining': '4999', 'x-ratelimit-reset': str(int(time.time()) + 3600)}
        mock_request.return_value = _resp(404, headers=headers, json_data={'message': 'Not Found'})
        with pytest.raises(GitHubAPIError) as exc:
            call(TEST_TOKEN, 'GET', '/repos/o/r')
        assert 'resets in' not in str(exc.value)


# ---------------------------------------------------------------------------
# Group gating
# ---------------------------------------------------------------------------


class TestGroupGating:
    def test_the_default_publishes_the_whole_surface(self):
        """Narrowing the default would silently break pipelines already running."""
        published = _instance()._collect_tool_methods()
        assert WRITE_TOOLS | READ_TOOLS | {RAW_REQUEST_TOOL} == set(published)
        assert len(published) == 38

    def test_default_groups_is_all_groups(self):
        assert DEFAULT_GROUPS == ALL_GROUPS

    def test_a_narrowed_selection_publishes_only_that_group(self):
        published = set(_instance(tool_groups=frozenset({'files'}))._collect_tool_methods())
        assert published == {'file_get', 'file_list', 'file_create', 'file_edit', 'file_delete', RAW_REQUEST_TOOL}

    def test_a_gated_tool_is_refused_on_invoke_too(self):
        """Dispatch reads this same map, so hiding and refusing are one mechanism."""
        assert 'release_create' not in _instance(tool_groups=frozenset({'files'}))._collect_tool_methods()

    @pytest.mark.parametrize('sentinel', ['all', '*', 'ALL'])
    def test_the_all_sentinels_resolve_to_everything(self, sentinel):
        assert normalize_groups([sentinel]) == ALL_GROUPS

    @pytest.mark.parametrize('raw', [[], '', None, 0, ['nope'], 'nope'])
    def test_empty_or_wholly_unknown_falls_back_to_the_defaults(self, raw):
        assert normalize_groups(raw) == DEFAULT_GROUPS

    def test_an_unknown_name_is_dropped_but_the_known_ones_survive(self):
        assert normalize_groups(['files', 'nope']) == frozenset({'files'})

    def test_the_comma_separated_string_form_is_accepted(self):
        assert normalize_groups('files, issues') == frozenset({'files', 'issues'})

    def test_matching_is_case_insensitive(self):
        assert normalize_groups(['FILES', 'Issues']) == frozenset({'files', 'issues'})

    def test_unknown_groups_reports_names_as_typed(self):
        assert unknown_groups(['files', 'Nope', 'ALL']) == ['Nope']

    def test_every_tool_carries_a_known_group(self):
        for name, attr in _tool_attrs():
            if name == RAW_REQUEST_TOOL:
                assert not hasattr(attr, '__github_group__')
                continue
            assert getattr(attr, '__github_group__', None) in ALL_GROUPS, name

    def test_the_decorator_rejects_an_unknown_group_at_import_time(self):
        with pytest.raises(ValueError, match='unknown group'):
            github_tool(group='nope')

    def test_the_group_counts_are_what_the_docs_claim(self):
        assert tool_counts_by_group() == EXPECTED_GROUP_COUNTS
        assert sum(EXPECTED_GROUP_COUNTS.values()) == 37


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


class TestReadOnly:
    def test_write_tools_are_hidden_rather_than_merely_refused(self):
        """An agent should never see a tool it can only ever fail on."""
        writable = set(_instance()._collect_tool_methods())
        read_only = set(_instance(read_only=True)._collect_tool_methods())
        assert writable - read_only == WRITE_TOOLS

    def test_reads_stay_published_in_read_only_mode(self):
        published = set(_instance(read_only=True)._collect_tool_methods())
        assert READ_TOOLS <= published

    def test_the_write_stamps_match_the_require_write_call_sites(self):
        """Catches a stamp with no guard, and a guard with no stamp.

        A stamp without a guard is hidden but still invokable through any direct call
        path; a guard without a stamp is published in read-only mode and guaranteed to
        fail. ``request`` is excluded because it guards per method rather than wholesale.
        """
        stamped = {name for name, attr in _tool_attrs() if getattr(attr, '__github_writes__', False)}
        guarded = _require_write_callers() - {RAW_REQUEST_TOOL}
        assert stamped == guarded == WRITE_TOOLS
        assert len(stamped) == 17

    @pytest.mark.parametrize('tool', ['file_create', 'issue_create', 'release_delete', 'workflow_dispatch'])
    @patch(_CLIENT)
    def test_require_write_still_guards_a_direct_call(self, mock_request, tool):
        """Defence in depth: hidden is not the same as unreachable."""
        inst = _instance(read_only=True)
        with pytest.raises(ValueError, match='read-only mode'):
            getattr(inst, tool)(
                {'path': 'a', 'content': 'c', 'message': 'm', 'title': 't', 'release_id': 1, 'workflow_id': 'w'}
            )
        mock_request.assert_not_called()


# ---------------------------------------------------------------------------
# The raw request tool
# ---------------------------------------------------------------------------


class TestRawRequestTool:
    def test_it_is_gated_by_its_own_switch(self):
        assert RAW_REQUEST_TOOL in _instance()._collect_tool_methods()
        assert RAW_REQUEST_TOOL not in _instance(allow_raw_request=False)._collect_tool_methods()

    def test_it_survives_an_empty_group_selection(self):
        """It carries no group, so the group filter must not catch it."""
        assert RAW_REQUEST_TOOL in _instance(tool_groups=frozenset())._collect_tool_methods()

    def test_it_stays_published_in_read_only_mode(self):
        """It is still a working read tool there."""
        assert RAW_REQUEST_TOOL in _instance(read_only=True)._collect_tool_methods()

    @pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
    @patch(_CLIENT)
    def test_it_refuses_a_write_method_in_read_only_mode(self, mock_request, method):
        """Without this the escape hatch walks around an advertised control."""
        with pytest.raises(ValueError, match='read-only mode'):
            _instance(read_only=True).request({'method': method, 'path': '/gists', 'body': {}})
        mock_request.assert_not_called()

    @patch(_CLIENT)
    def test_it_allows_get_in_read_only_mode(self, mock_request):
        mock_request.return_value = _resp(200, json_data=[{'id': 'g'}])
        assert _instance(read_only=True).request({'method': 'GET', 'path': '/gists'}) == [{'id': 'g'}]

    @patch(_CLIENT)
    def test_it_reaches_an_endpoint_no_typed_tool_models(self, mock_request):
        """The issue's acceptance criterion."""
        mock_request.return_value = _resp(200, json_data={'required_status_checks': {}})
        out = _instance().request({'method': 'GET', 'path': f'/repos/{REPO}/branches/main/protection'})
        assert out == {'required_status_checks': {}}
        assert mock_request.call_args.args[1].endswith('/branches/main/protection')

    @pytest.mark.parametrize('method', ['TRACE', 'CONNECT', 'HEAD'])
    @patch(_CLIENT)
    def test_it_rejects_a_method_outside_the_enum(self, mock_request, method):
        """HEAD is excluded deliberately: GitHub documents no HEAD-only surface."""
        with pytest.raises(ValueError, match='must be one of'):
            _instance().request({'method': method, 'path': '/gists'})
        mock_request.assert_not_called()

    @pytest.mark.parametrize('field', ['params', 'body'])
    @patch(_CLIENT)
    def test_it_type_checks_params_and_body(self, mock_request, field):
        with pytest.raises(ValueError, match=f'"{field}" must be an object'):
            _instance().request({'method': 'GET', 'path': '/gists', field: 'nope'})

    @patch(_CLIENT)
    def test_its_return_value_is_redacted(self, mock_request):
        mock_request.return_value = _resp(200, json_data={'echo': TEST_TOKEN})
        out = _instance().request({'method': 'GET', 'path': '/gists'})
        assert TEST_TOKEN not in json.dumps(out)


# ---------------------------------------------------------------------------
# Descriptors and config surface
# ---------------------------------------------------------------------------


class TestSchemas:
    def test_every_published_tool_has_a_description(self):
        for name, attr in _tool_attrs():
            assert (attr.__tool_meta__.get('description') or '').strip(), name

    def test_every_input_schema_is_a_json_object_schema(self):
        for name, attr in _tool_attrs():
            schema = attr.__tool_meta__.get('input_schema')
            assert isinstance(schema, dict) and schema.get('type') == 'object', name
            properties = schema.get('properties') or {}
            assert set(schema.get('required') or []) <= set(properties), name

    def test_the_request_method_enum_excludes_head(self):
        """github_client can decode a HEAD now, but GitHub documents no HEAD-only surface,
        so offering it would promise something nothing exercises.
        """
        enum = IInstance.request.__tool_meta__['input_schema']['properties']['method']['enum']
        assert 'HEAD' not in enum
        assert enum == sorted(enum)


class TestConfigSurface:
    @staticmethod
    def _services():
        return json.loads(_SERVICES_JSON.read_text(encoding='utf-8'))

    def test_the_group_enum_matches_the_module(self):
        """The one place the group list is duplicated, so pin it."""
        enum = self._services()['fields']['github.toolGroups']['items']['enum']
        assert enum == sorted(ALL_GROUPS) + ['all']

    def test_the_defaults_agree_with_the_preconfig_profile(self):
        data = self._services()
        profile = data['preconfig']['profiles']['default']
        assert profile['toolGroups'] == data['fields']['github.toolGroups']['default'] == []
        assert profile['allowRawRequest'] == data['fields']['github.allowRawRequest']['default'] is True

    def test_both_new_fields_are_on_the_shape(self):
        properties = self._services()['shape'][0]['properties']
        assert 'github.toolGroups' in properties
        assert 'github.allowRawRequest' in properties

    def test_the_read_only_description_says_hidden_not_blocked(self):
        """The behaviour changed, so the operator-facing copy has to change with it."""
        description = self._services()['fields']['github.readOnly']['description']
        assert 'hidden' in description.lower()
