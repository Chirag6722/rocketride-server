# MIT License
#
# Copyright (c) 2026 Aparavi Software AG
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Contract tests for the app publish ladder (``rrext_app_deploy``).

The ladder is shared platform infrastructure (OSS and SaaS alike): publish
snapshots an immutable version, deploy pins a rung, where reads the reverse
index, and entry mints a signed bundle URL for ONE specific version. The
entitlement rule is the security contract under test: a version's bundle is
only reachable when it is pinned on a rung the caller belongs to, or the
caller published it — enforced both at personal-rung deploys (any user
could otherwise pin any bundle onto themselves) and at entry minting.

The registry backend (``account.deployments_*``) is faked with an
in-memory registry; the handler's own logic (argument validation, target
resolution, the scope walk, the entitlement checks) runs for real.
"""

from types import SimpleNamespace

import pytest

from ai.account import account as account_singleton
from ai.account import dev_overlay, file_store
from ai.account.app_deploy import handle_app_deploy, resolve_app_pins


# =============================================================================
# FAKES — conn + in-memory deployments registry
# =============================================================================


class _FakeStore:
    """Records write_bytes calls (the publish bundle write)."""

    def __init__(self):
        # path -> bytes written
        self.writes = {}

    async def write_bytes(self, path, data):
        """Record the bundle write."""
        self.writes[path] = data


class _FakeConn:
    """Minimal TaskConn stand-in: account info + response builders + store."""

    def __init__(self, user_id='u1', org_id='org1', teams=None, authenticated=True):
        # Account info mirrors the dict-shaped organization the handler accepts
        self._account_info = (
            SimpleNamespace(
                userId=user_id,
                displayName='User One',
                email='u1@example.com',
                organization={'id': org_id, 'teams': teams or []},
            )
            if authenticated
            else None
        )
        self._server = SimpleNamespace(store=_FakeStore())

    def build_response(self, request, body=None):
        """Success envelope — shape is this fake's own convention."""
        return {'success': True, 'body': body or {}}

    def build_error(self, request, message):
        """Error envelope — shape is this fake's own convention."""
        return {'success': False, 'message': message}


class _FakeRegistry:
    """In-memory deployments registry patched over ``account.deployments_*``."""

    def __init__(self):
        # Registry entries: [{version, sha256, publishedAt, publishedBy, comment}]
        self.versions = []
        # Registry version -> artifact dict
        self.artifacts = {}
        # Pointer key ('user~u1' | '<teamId>' | '~org') -> deployment record
        self.pointers = {}
        # Call records for assertions
        self.deploy_calls = []
        self.publish_calls = []

    def add_version(self, version, app_version, publisher_id='dev1', kind='app'):
        """Seed one published registry version + its artifact."""
        self.versions.append(
            {
                'version': version,
                'sha256': f'sha-{version}',
                'publishedAt': 1000 + version,
                'publishedBy': {'userId': publisher_id, 'display': 'Dev', 'email': 'dev@example.com'},
                'comment': f'v{app_version}',
            }
        )
        self.artifacts[version] = {
            'kind': kind,
            'appId': 'acme.brandy',
            'moduleId': 'acme_brandy',
            'name': 'Brandy',
            'appVersion': app_version,
            'bundleDir': f'appbundles/org1/acme.brandy/{app_version}',
        }

    def pin(self, key, version, state='enabled'):
        """Seed one rung pointer record."""
        self.pointers[key] = {'version': version, 'state': state, 'deployedAt': 2000 + version}

    def install(self, monkeypatch):
        """Patch the account singleton's deployments_* with this registry."""

        async def deployments_versions(org_id, project_id):
            return list(self.versions)

        async def deployments_artifact(org_id, project_id, version):
            # Missing artifact raises, like the real backend — callers catch
            return self.artifacts[version]

        async def deployments_get(org_id, key, project_id):
            return self.pointers[key]

        async def deployments_deploy(org_id, key, project_id, version, actor):
            self.deploy_calls.append({'key': key, 'version': version, 'actor': actor})
            return {'version': version, 'state': 'enabled', 'deployedAt': 999}

        async def deployments_publish(org_id, project_id, artifact, actor, comment=''):
            self.publish_calls.append({'artifact': artifact, 'actor': actor, 'comment': comment})
            entry = {
                'version': len(self.versions) + 1,
                'sha256': 'sha-new',
                'publishedAt': 3000,
                'publishedBy': actor,
                'comment': comment,
            }
            self.versions.append(entry)
            self.artifacts[entry['version']] = artifact
            return entry

        monkeypatch.setattr(account_singleton, 'deployments_versions', deployments_versions, raising=False)
        monkeypatch.setattr(account_singleton, 'deployments_artifact', deployments_artifact, raising=False)
        monkeypatch.setattr(account_singleton, 'deployments_get', deployments_get, raising=False)
        monkeypatch.setattr(account_singleton, 'deployments_deploy', deployments_deploy, raising=False)
        monkeypatch.setattr(account_singleton, 'deployments_publish', deployments_publish, raising=False)


def _request(subcommand, **args):
    """Build a raw rrext_app_deploy DAP request dict."""
    return {'command': 'rrext_app_deploy', 'arguments': {'subcommand': subcommand, 'appId': 'acme.brandy', **args}}


@pytest.fixture
def registry(monkeypatch):
    """Fresh in-memory registry installed over the account singleton."""
    reg = _FakeRegistry()
    reg.install(monkeypatch)
    return reg


@pytest.fixture
def mint(monkeypatch):
    """Deterministic signed-URL minter; records nothing, raises never."""

    def mint_directory_url(bundle_dir, name, sub=None):
        return f'https://signed/{bundle_dir}/{name}?sub={sub}'

    monkeypatch.setattr(file_store, 'mint_directory_url', mint_directory_url)
    return mint_directory_url


@pytest.fixture
def quiet_push(monkeypatch):
    """Silence the deploy handler's manifest refresh push; record calls."""
    calls = []

    async def push_refresh(server, user_id, source):
        calls.append({'user_id': user_id, 'source': source})

    monkeypatch.setattr(dev_overlay, 'push_refresh', push_refresh)
    return calls


# Standard team roster used across tests: caller is in team t1 ('Development')
_TEAMS = [{'id': 't1', 'name': 'Development'}]


# =============================================================================
# AUTH + ARGUMENT VALIDATION
# =============================================================================


@pytest.mark.asyncio
async def test_requires_authenticated_connection(registry):
    """An unauthenticated connection is refused outright."""
    conn = _FakeConn(authenticated=False)
    result = await handle_app_deploy(conn, _request('versions'))
    assert result['success'] is False
    assert 'authenticated' in result['message']


@pytest.mark.asyncio
async def test_requires_app_id(registry):
    """Every subcommand requires an appId."""
    conn = _FakeConn()
    request = {'command': 'rrext_app_deploy', 'arguments': {'subcommand': 'versions'}}
    result = await handle_app_deploy(conn, request)
    assert result['success'] is False
    assert 'appId' in result['message']


@pytest.mark.asyncio
async def test_unknown_subcommand_errors(registry):
    """An unknown subcommand reports itself instead of falling through."""
    conn = _FakeConn()
    result = await handle_app_deploy(conn, _request('promote'))
    assert result['success'] is False
    assert 'promote' in result['message']


# =============================================================================
# PUBLISH — immutable snapshot, never activates
# =============================================================================


@pytest.mark.asyncio
async def test_publish_requires_version_and_data(registry):
    """Publishing without a semver or without bundle bytes is rejected."""
    conn = _FakeConn()
    no_version = await handle_app_deploy(conn, _request('publish', data=b'js'))
    no_data = await handle_app_deploy(conn, _request('publish', version='1.0.0'))
    assert no_version['success'] is False
    assert no_data['success'] is False


@pytest.mark.asyncio
async def test_publish_writes_bundle_and_returns_rail_entry(registry):
    """Publishing stores the bundle bytes and records a kind:'app' artifact."""
    conn = _FakeConn()
    result = await handle_app_deploy(
        conn, _request('publish', version='1.0.0', data=b'bundle-bytes', message='first cut')
    )

    # Bundle bytes land at the org-scoped platform path
    assert conn._server.store.writes == {'appbundles/org1/acme.brandy/1.0.0/remoteEntry.js': b'bundle-bytes'}
    # The registry records an app artifact with the caller as actor
    assert len(registry.publish_calls) == 1
    artifact = registry.publish_calls[0]['artifact']
    assert artifact['kind'] == 'app'
    assert artifact['appVersion'] == '1.0.0'
    assert registry.publish_calls[0]['actor']['userId'] == 'u1'
    # The response is a rail entry — registry identity + artifact semver
    entry = result['body']['entry']
    assert entry['registryVersion'] == 1
    assert entry['appVersion'] == '1.0.0'
    assert entry['message'] == 'first cut'
    # Publishing pinned nothing anywhere
    assert registry.deploy_calls == []


# =============================================================================
# VERSIONS — the rail, newest first, rung chips merged
# =============================================================================


@pytest.mark.asyncio
async def test_versions_rail_newest_first_with_rung_chips(registry):
    """The rail sorts newest-first and tags each row with its pinned rungs."""
    registry.add_version(1, '1.0.0')
    registry.add_version(2, '1.1.0')
    registry.pin('user~u1', 1)
    registry.pin('~org', 2)

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_app_deploy(conn, _request('versions'))

    rail = result['body']['versions']
    assert [row['registryVersion'] for row in rail] == [2, 1]
    assert rail[0]['rungs'] == ['org']
    assert rail[1]['rungs'] == ['personal']


# =============================================================================
# DEPLOY — pin a rung; the personal rung is entitlement-guarded
# =============================================================================


@pytest.mark.asyncio
async def test_deploy_requires_registry_version_int(registry):
    """Deploying rejects a missing or non-int version (semver goes to entry, not deploy)."""
    conn = _FakeConn()
    result = await handle_app_deploy(conn, _request('deploy', version='1.0.0', target='@org'))
    assert result['success'] is False
    assert 'version' in result['message']


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['@nope', '@team/ghost'])
async def test_deploy_rejects_unknown_targets(registry, target):
    """Malformed targets and non-member teams are refused."""
    registry.add_version(1, '1.0.0')
    conn = _FakeConn(teams=_TEAMS)
    result = await handle_app_deploy(conn, _request('deploy', version=1, target=target))
    assert result['success'] is False
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_deploy_personal_rung_blocked_without_entitlement(registry, quiet_push):
    """A user cannot pin a version onto themselves that they cannot already reach."""
    registry.add_version(1, '1.0.0', publisher_id='someone-else')

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_app_deploy(conn, _request('deploy', version=1, target='@user'))

    assert result['success'] is False
    assert 'Not entitled' in result['message']
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_deploy_personal_rung_allowed_for_publisher(registry, quiet_push):
    """The developer self-publish flow: publishing a version entitles you to pin it."""
    registry.add_version(1, '1.0.0', publisher_id='u1')

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_app_deploy(conn, _request('deploy', version=1, target='@user'))

    assert result['success'] is True
    assert registry.deploy_calls == [{'key': 'user~u1', 'version': 1, 'actor': registry.deploy_calls[0]['actor']}]
    assert result['body']['rung'] == 'personal'
    # The acting user's manifest is refreshed (data + signal)
    assert quiet_push == [{'user_id': 'u1', 'source': 'app-deploy'}]


@pytest.mark.asyncio
async def test_deploy_personal_rung_allowed_when_pinned_on_a_rung(registry, quiet_push):
    """A version already on one of the caller's rungs may be self-pinned (drop-list flow)."""
    registry.add_version(1, '1.0.0', publisher_id='someone-else')
    registry.pin('t1', 1)

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_app_deploy(conn, _request('deploy', version=1, target='@user'))

    assert result['success'] is True
    assert registry.deploy_calls[0]['key'] == 'user~u1'


@pytest.mark.asyncio
async def test_deploy_team_and_org_rungs_are_not_entitlement_guarded(registry, quiet_push):
    """Audience rungs (team/org) pin without the personal entitlement check."""
    registry.add_version(1, '1.0.0', publisher_id='someone-else')

    conn = _FakeConn(teams=_TEAMS)
    team_result = await handle_app_deploy(conn, _request('deploy', version=1, target='@team/Development'))
    org_result = await handle_app_deploy(conn, _request('deploy', version=1, target='@org'))

    assert team_result['success'] is True
    assert team_result['body']['rung'] == 'team'
    assert org_result['success'] is True
    assert [c['key'] for c in registry.deploy_calls] == ['t1', '~org']


# =============================================================================
# WHERE — the caller-visible reverse index
# =============================================================================


@pytest.mark.asyncio
async def test_where_lists_caller_pins_and_skips_removed(registry):
    """The reverse index lists one row per live pin, removed pins skipped."""
    registry.add_version(1, '1.0.0')
    registry.add_version(2, '1.1.0')
    registry.pin('user~u1', 1)
    registry.pin('t1', 2)
    registry.pin('~org', 1, state='removed')

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_app_deploy(conn, _request('where'))

    pins = result['body']['pins']
    assert [(p['rung'], p['handle'], p['version'], p['appVersion']) for p in pins] == [
        ('personal', '@user', 1, '1.0.0'),
        ('team', '@team/Development', 2, '1.1.0'),
    ]


# =============================================================================
# ENTRY — mint a signed URL for ONE version, entitlement-checked
# =============================================================================


@pytest.mark.asyncio
async def test_entry_resolves_registry_int_when_pinned(registry, mint):
    """An int version pinned on a caller rung mints the signed entry URL."""
    registry.add_version(1, '1.0.0')
    registry.pin('t1', 1)

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_app_deploy(conn, _request('entry', version=1))

    body = result['body']
    assert body['url'] == 'https://signed/appbundles/org1/acme.brandy/1.0.0/remoteEntry.js?sub=app-entry'
    assert body['moduleId'] == 'acme_brandy'
    assert body['appVersion'] == '1.0.0'
    assert body['registryVersion'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('wire_version', ['1.1.0', 'v1.1.0'])
async def test_entry_resolves_semver_string(registry, mint, wire_version):
    """A semver string ('v' prefix tolerated) resolves to its registry version."""
    registry.add_version(1, '1.0.0')
    registry.add_version(2, '1.1.0')
    registry.pin('~org', 2)

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_app_deploy(conn, _request('entry', version=wire_version))

    assert result['success'] is True
    assert result['body']['registryVersion'] == 2


@pytest.mark.asyncio
async def test_entry_semver_republish_resolves_newest_registry_entry(registry, mint):
    """A re-published semver resolves to the NEWEST registry entry carrying it."""
    registry.add_version(1, '1.0.0')
    registry.add_version(2, '1.0.0')
    registry.pin('~org', 2)

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_app_deploy(conn, _request('entry', version='1.0.0'))

    assert result['body']['registryVersion'] == 2


@pytest.mark.asyncio
async def test_entry_unknown_version_errors(registry, mint):
    """A version the registry has never seen is reported, not minted."""
    registry.add_version(1, '1.0.0')

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_app_deploy(conn, _request('entry', version=99))

    assert result['success'] is False
    assert 'not found' in result['message']


@pytest.mark.asyncio
async def test_entry_blocked_without_entitlement(registry, mint):
    """A version on nobody's rung, published by someone else, does not mint."""
    registry.add_version(1, '1.0.0', publisher_id='someone-else')

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_app_deploy(conn, _request('entry', version=1))

    assert result['success'] is False
    assert 'Not entitled' in result['message']


@pytest.mark.asyncio
async def test_entry_publisher_is_entitled(registry, mint):
    """The publisher of a version may mint it even before any rung pins it."""
    registry.add_version(1, '1.0.0', publisher_id='u1')

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_app_deploy(conn, _request('entry', version=1))

    assert result['success'] is True
    assert result['body']['registryVersion'] == 1


@pytest.mark.asyncio
async def test_entry_rejects_non_app_artifacts(registry, mint):
    """Pipeline deployments share the registry — entry only mints app artifacts."""
    registry.add_version(1, '1.0.0', publisher_id='u1', kind='pipeline')

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_app_deploy(conn, _request('entry', version=1))

    assert result['success'] is False
    assert 'not an app artifact' in result['message']


@pytest.mark.asyncio
async def test_entry_mint_failure_is_reported(registry, monkeypatch):
    """A signing failure surfaces as an error instead of a raw exception."""
    registry.add_version(1, '1.0.0', publisher_id='u1')

    def broken_mint(bundle_dir, name, sub=None):
        raise RuntimeError('signing unconfigured')

    monkeypatch.setattr(file_store, 'mint_directory_url', broken_mint)

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_app_deploy(conn, _request('entry', version=1))

    assert result['success'] is False
    assert 'signing unconfigured' in result['message']


# =============================================================================
# RESOLVE — the manifest scope walk (org -> team -> personal, specific wins)
# =============================================================================


@pytest.mark.asyncio
async def test_resolve_app_pins_most_specific_rung_wins(registry, mint, monkeypatch):
    """On id collisions the personal pin beats team, team beats org."""
    registry.add_version(1, '1.0.0')
    registry.add_version(2, '1.1.0')

    async def deployments_list(org_id, key):
        # Org rung runs v1; the personal rung runs v2 — personal must win
        if key == '~org':
            return [{'projectId': 'acme.brandy', 'version': 1, 'state': 'enabled'}]
        if key == 'user~u1':
            return [{'projectId': 'acme.brandy', 'version': 2, 'state': 'enabled'}]
        return []

    monkeypatch.setattr(account_singleton, 'deployments_list', deployments_list, raising=False)

    resolved = await resolve_app_pins('org1', 'u1', ['t1'])

    assert len(resolved) == 1
    assert resolved[0]['id'] == 'acme.brandy'
    assert resolved[0]['version'] == '1.1.0'
    assert 'personal' in resolved[0]['description']


@pytest.mark.asyncio
async def test_resolve_app_pins_skips_non_apps_and_disabled(registry, mint, monkeypatch):
    """Pipeline artifacts and non-enabled pins never reach the manifest."""
    registry.add_version(1, '1.0.0', kind='pipeline')
    registry.add_version(2, '1.1.0')

    async def deployments_list(org_id, key):
        if key == '~org':
            return [
                {'projectId': 'acme.brandy', 'version': 1, 'state': 'enabled'},  # pipeline artifact
                {'projectId': 'acme.brandy', 'version': 2, 'state': 'removed'},  # disabled pin
            ]
        return []

    monkeypatch.setattr(account_singleton, 'deployments_list', deployments_list, raising=False)

    resolved = await resolve_app_pins('org1', 'u1', [])

    assert resolved == []
