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

"""Contract tests for app publish control (``rrext_deploy_app``) + the rail's app branch.

DEPLOY = copy code to the server (the generic ``rrext_deploy add`` verb;
``handle_app_add`` is its app branch — zip transport, unpacked at receipt).
The REVIEW state lives on the DEPLOYMENT (private -> submit -> ready |
rejected); ``submit`` flips it, admin approve/reject move it on. PUBLISH
binds a deployment to an audience (user | team | public; no org rung) as a
pure pointer (born 'enabled') — a public binding needs a 'ready' deployment,
internal bindings accept any non-'failed' one. App ids are partitioned by the
caller's developer namespace, and a version's bundle is only reachable when a
caller-visible binding serves it (public counts only when the deployment is
'ready') or the caller deployed it — the security contract under test.

The registry backend (``account.deployments_*`` + ``publish_*``) is faked
with an in-memory registry; the handler's own logic (argument validation,
target resolution, the scope walk, the entitlement checks) runs for real.
"""

import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from ai.account import account as account_singleton
from ai.account import dev_overlay, file_store
from ai.account.app_deploy import handle_app_add, handle_deploy_app, resolve_app_pins


# =============================================================================
# FAKES — conn + in-memory deployments registry
# =============================================================================


class _FakeStore:
    """Records write_bytes calls (the deploy bundle write)."""

    def __init__(self):
        # path -> bytes written
        self.writes = {}

    async def write_bytes(self, path, data):
        """Record the bundle write."""
        self.writes[path] = data


class _FakeConn:
    """Minimal TaskConn stand-in: account info + response builders + store."""

    def __init__(self, user_id='u1', org_id='org1', teams=None, authenticated=True, developer_id='acme'):
        # Account info mirrors the dict-shaped organization the handler accepts
        self._account_info = (
            SimpleNamespace(
                userId=user_id,
                displayName='User One',
                email='u1@example.com',
                organization={'id': org_id, 'teams': teams or [], 'developerId': developer_id},
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
    """In-memory rail + publish rows patched over the account singleton."""

    def __init__(self):
        # Registry entries: [{version, sha256, state, metadata, publishedBy, ...}]
        self.versions = []
        # Registry version -> artifact dict
        self.artifacts = {}
        # (app_id, audience_key) -> publish row (contract shape)
        self.publishes = {}
        # Call records for assertions
        self.publish_calls = []
        self.set_calls = []

    def add_version(self, version, app_version, publisher_id='dev1', kind='app', state='ready', manifest=None):
        """Seed one deployed registry version + its artifact."""
        self.versions.append(
            {
                'version': version,
                'sha256': f'sha-{version}',
                'state': state,
                'metadata': {'manifest': manifest or {'name': 'Brandy', 'version': app_version}},
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
        }

    @staticmethod
    def _key(audience):
        """The fake's audience key (mirrors the backends' encodings)."""
        return f'{audience["type"]}~{audience.get("id", "")}'

    def seed_publish(self, audience, version, art_state=None):
        """Bind one audience to a version (binding born 'enabled').

        The REVIEW state lives on the deployment, so ``art_state`` (when
        given) sets that version's deployment state; the read fakes join it
        back onto the row as ``artifactState``.
        """
        if art_state is not None:
            for v in self.versions:
                if int(v.get('version', 0)) == int(version):
                    v['state'] = art_state
        self.publishes[('acme.brandy', self._key(audience))] = {
            'orgId': 'org1',
            'appId': 'acme.brandy',
            'audience': dict(audience),
            'version': version,
            'state': 'enabled',
            'snapshot': {'name': 'Brandy'},
            'publishedAt': 2000 + version,
        }

    def install(self, monkeypatch):
        """Patch the account singleton's rail + publish methods."""

        async def deployments_versions(org_id, project_id):
            return list(self.versions)

        async def deployments_artifact(org_id, project_id, version):
            return self.artifacts[version]

        async def deployments_publish(org_id, project_id, artifact, actor, comment='', metadata=None, state=None):
            self.publish_calls.append({'artifact': artifact, 'actor': actor, 'comment': comment, 'metadata': metadata})
            entry = {
                'version': len(self.versions) + 1,
                'sha256': 'sha-new',
                'state': state or ('private' if artifact.get('kind') == 'app' else 'ready'),
                'metadata': metadata or {},
                'publishedAt': 3000,
                'publishedBy': actor,
                'comment': comment,
            }
            self.versions.append(entry)
            self.artifacts[entry['version']] = artifact
            return entry

        def _art_state(version):
            """The deployment's review state (from the rail), for publish rows."""
            for v in self.versions:
                if int(v.get('version', 0)) == int(version):
                    return str(v.get('state') or '')
            return ''

        async def set_artifact_state(org_id, project_id, version, new_state, actor):
            for v in self.versions:
                if int(v.get('version', 0)) == int(version):
                    v['state'] = new_state
                    return dict(v)
            raise KeyError(version)

        async def publish_set(org_id, kind, app_id, audience, version, snapshot, actor):
            row = {
                'orgId': org_id,
                'appId': app_id,
                'audience': dict(audience),
                'version': version,
                'state': 'enabled',
                'artifactState': _art_state(version),
                'snapshot': dict(snapshot or {}),
                'publishedAt': 4000,
            }
            self.publishes[(app_id, self._key(audience))] = row
            self.set_calls.append({'audience': dict(audience), 'version': version, 'snapshot': snapshot})
            return row

        def _with_state(row):
            """Refresh the joined artifactState (the deployment may have moved)."""
            return {**row, 'artifactState': _art_state(row.get('version'))}

        async def publish_get(org_id, kind, app_id, audience):
            row = self.publishes.get((app_id, self._key(audience)))
            return None if not row or row.get('state') == 'removed' else _with_state(row)

        async def publish_of_app(org_id, kind, app_id):
            return [
                _with_state(r)
                for (aid, _), r in self.publishes.items()
                if aid == app_id and r.get('state') != 'removed'
            ]

        async def publish_list(org_id, kind, audiences):
            keys = {self._key(a) for a in audiences}
            return [
                _with_state(r)
                for (aid, key), r in self.publishes.items()
                if key in keys and r.get('state') != 'removed'
            ]

        async def publish_set_state(org_id, kind, app_id, audience, state, actor):
            row = self.publishes[(app_id, self._key(audience))]
            row['state'] = state
            return _with_state(row)

        for name, fn in (
            ('deployments_versions', deployments_versions),
            ('deployments_artifact', deployments_artifact),
            ('deployments_publish', deployments_publish),
            ('set_artifact_state', set_artifact_state),
            ('publish_set', publish_set),
            ('publish_get', publish_get),
            ('publish_of_app', publish_of_app),
            ('publish_list', publish_list),
            ('publish_set_state', publish_set_state),
        ):
            monkeypatch.setattr(account_singleton, name, fn, raising=False)


def _request(subcommand, **args):
    """Build a raw rrext_deploy_app DAP request dict."""
    return {'command': 'rrext_deploy_app', 'arguments': {'subcommand': subcommand, 'appId': 'acme.brandy', **args}}


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
    """Silence the publish handler's manifest refresh push; record calls."""
    calls = []

    async def push_refresh(server, user_id, source):
        calls.append({'user_id': user_id, 'source': source})

    monkeypatch.setattr(dev_overlay, 'push_refresh', push_refresh)
    return calls


# Standard team roster used across tests: caller is in team t1 ('Development')
_TEAMS = [{'id': 't1', 'name': 'Development'}]

AUD_USER = {'type': 'user', 'id': 'u1'}
AUD_TEAM = {'type': 'team', 'id': 't1'}
AUD_PUBLIC = {'type': 'public', 'id': ''}


# =============================================================================
# AUTH + ARGUMENT VALIDATION
# =============================================================================


@pytest.mark.asyncio
async def test_requires_authenticated_connection(registry):
    """An unauthenticated connection is refused outright."""
    conn = _FakeConn(authenticated=False)
    result = await handle_deploy_app(conn, _request('versions'))
    assert result['success'] is False
    assert 'authenticated' in result['message']


@pytest.mark.asyncio
async def test_requires_app_id(registry):
    """Every subcommand requires an appId."""
    conn = _FakeConn()
    request = {'command': 'rrext_deploy_app', 'arguments': {'subcommand': 'versions'}}
    result = await handle_deploy_app(conn, request)
    assert result['success'] is False
    assert 'appId' in result['message']


@pytest.mark.asyncio
async def test_unknown_subcommand_errors(registry):
    """An unknown subcommand reports itself instead of falling through."""
    conn = _FakeConn()
    result = await handle_deploy_app(conn, _request('promote'))
    assert result['success'] is False
    assert 'promote' in result['message']


# =============================================================================
# ADD — the generic rail door's app branch (zip transport)
# =============================================================================


def _app_zip(manifest=None, files=None):
    """An in-memory built-bundle zip: remoteEntry.js + package.json (+extras)."""
    manifest = manifest if manifest is not None else {'id': 'acme.brandy', 'name': 'Brandy', 'version': '1.0.0'}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('remoteEntry.js', 'console.log("brandy")')
        archive.writestr('package.json', json.dumps({'name': 'brandy', 'appManifest': manifest}))
        for name, content in (files or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _add_request(**args):
    """A raw rrext_deploy add request dict (kind='app' branch)."""
    return {'command': 'rrext_deploy', 'arguments': {'subcommand': 'add', 'kind': 'app', **args}}


@pytest.mark.asyncio
async def test_add_requires_data_and_valid_zip(registry):
    """The app branch refuses missing data, junk bytes, and manifest-less zips."""
    conn = _FakeConn()
    assert (await handle_app_add(conn, _add_request()))['success'] is False
    assert (await handle_app_add(conn, _add_request(data=b'not-a-zip')))['success'] is False

    # A zip without package.json/appManifest names the real problem
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('remoteEntry.js', 'x')
    result = await handle_app_add(conn, _add_request(data=buffer.getvalue()))
    assert result['success'] is False
    assert 'package.json' in result['message']


@pytest.mark.asyncio
async def test_add_unpacks_zip_and_returns_rail_entry(registry):
    """The zip is retained at bundle/, unpacked at app/, and registered kind:'app'."""
    conn = _FakeConn()
    data = _app_zip(files={'static/js/chunk.js': 'chunk'})
    result = await handle_app_add(conn, _add_request(data=data, comment='first cut', metadata={'projectId': 'wc-1'}))

    # Registry records the app artifact with the FULL manifest as metadata
    assert len(registry.publish_calls) == 1
    call = registry.publish_calls[0]
    assert call['artifact']['kind'] == 'app'
    assert call['artifact']['appVersion'] == '1.0.0'
    assert call['metadata']['manifest']['id'] == 'acme.brandy'
    assert call['metadata']['projectId'] == 'wc-1'  # client working-copy provenance

    # Content: retained transport zip + the unpacked servable tree
    writes = conn._server.store.writes
    home = 'orgs/org1/files/.apps/acme.brandy/v000001'
    assert f'{home}/bundle/acme.brandy-v000001.zip' in writes
    assert writes[f'{home}/app/remoteEntry.js'] == b'console.log("brandy")'
    assert f'{home}/app/static/js/chunk.js' in writes

    # One generic response shape for every kind: add -> {artifact}
    entry = result['body']['artifact']
    assert entry['registryVersion'] == 1
    assert entry['appVersion'] == '1.0.0'
    assert entry['message'] == 'first cut'
    # Deploying published nothing anywhere
    assert registry.set_calls == []


@pytest.mark.asyncio
async def test_add_rejects_unsafe_zip_entries(registry):
    """Path traversal inside the archive is refused before any write."""
    conn = _FakeConn()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('remoteEntry.js', 'x')
        archive.writestr('package.json', json.dumps({'appManifest': {'id': 'acme.brandy', 'name': 'B'}}))
        archive.writestr('../escape.js', 'evil')
    result = await handle_app_add(conn, _add_request(data=buffer.getvalue()))
    assert result['success'] is False
    assert 'unsafe path' in result['message']
    assert conn._server.store.writes == {}


@pytest.mark.asyncio
async def test_add_rejects_foreign_namespace_app_id(registry):
    """Deploy fails fast when the manifest declares an app id outside the
    caller org's developer namespace — the impersonation ('I am org xyz but
    declare rocketride.pipeBuilder') dies before any artifact is created.
    """
    conn = _FakeConn(developer_id='xyz')
    data = _app_zip(manifest={'id': 'rocketride.pipeBuilder', 'name': 'Fake', 'version': '1.0.0'})
    result = await handle_app_add(conn, _add_request(data=data))
    assert result['success'] is False
    assert 'namespace' in result['message']
    assert registry.publish_calls == []
    assert conn._server.store.writes == {}


@pytest.mark.asyncio
async def test_add_requires_a_developer_id(registry):
    """An org with no developer id owns no namespace and cannot deploy apps."""
    conn = _FakeConn(developer_id=None)
    result = await handle_app_add(conn, _add_request(data=_app_zip()))
    assert result['success'] is False
    assert 'developer id' in result['message']
    assert registry.publish_calls == []


@pytest.mark.asyncio
async def test_add_rejects_zip_bomb_on_actual_size(registry, monkeypatch):
    """The unpacked cap is measured on REAL decompressed bytes, not the
    attacker-controlled declared size — a highly-compressible entry over the
    limit is refused before any registry row or file write.
    """
    # 200MB of zeros compresses to a few KB — the classic declared-size lie.
    monkeypatch.setattr('ai.account.app_deploy._ZIP_MAX_UNPACKED', 8 * 1024 * 1024)
    conn = _FakeConn()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('remoteEntry.js', 'x')
        archive.writestr('package.json', json.dumps({'appManifest': {'id': 'acme.brandy', 'name': 'B'}}))
        archive.writestr('bomb.bin', b'\0' * (32 * 1024 * 1024))
    result = await handle_app_add(conn, _add_request(data=buffer.getvalue()))
    assert result['success'] is False
    assert 'unpacks past' in result['message']
    # Nothing was registered and nothing was written.
    assert registry.publish_calls == []
    assert conn._server.store.writes == {}


# =============================================================================
# PUBLISH — bind a deployment to an audience (user | team | public)
# =============================================================================


@pytest.mark.asyncio
async def test_publish_requires_registry_version_int(registry):
    """Publishing rejects a missing or non-int version (semver is display-only)."""
    conn = _FakeConn()
    result = await handle_deploy_app(conn, _request('publish', version='1.0.0', target='@team/Development'))
    assert result['success'] is False
    assert 'version' in result['message']


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['@nope', '@team/ghost', '@org'])
async def test_publish_rejects_unknown_targets(registry, target):
    """Malformed targets, non-member teams, and the retired org rung are refused."""
    registry.add_version(1, '1.0.0')
    conn = _FakeConn(teams=_TEAMS)
    result = await handle_deploy_app(conn, _request('publish', version=1, target=target))
    assert result['success'] is False
    assert registry.set_calls == []


@pytest.mark.asyncio
async def test_publish_user_blocked_without_entitlement(registry, quiet_push):
    """A user cannot publish a version to themselves that they cannot reach."""
    registry.add_version(1, '1.0.0', publisher_id='someone-else')

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_deploy_app(conn, _request('publish', version=1, target='@user'))

    assert result['success'] is False
    assert 'Not entitled' in result['message']
    assert registry.set_calls == []


@pytest.mark.asyncio
async def test_publish_user_allowed_for_deployer(registry, quiet_push):
    """The developer self-publish flow: deploying a version entitles you to publish it."""
    registry.add_version(1, '1.0.0', publisher_id='u1')

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_deploy_app(conn, _request('publish', version=1, target='@user'))

    assert result['success'] is True
    row = result['body']['publish']
    # The binding is a pure pointer born 'enabled' — publish-and-go, no approval
    assert (row['audience'], row['state']) == (AUD_USER, 'enabled')
    # The manifest snapshot rode along from the entry's metadata
    assert registry.set_calls[0]['snapshot']['name'] == 'Brandy'
    # The acting user's manifest is refreshed (data + signal)
    assert quiet_push == [{'user_id': 'u1', 'source': 'app-publish'}]


@pytest.mark.asyncio
async def test_publish_team_needs_membership_only(registry, quiet_push):
    """Team publishing is membership-gated; the binding is born 'enabled'.

    Internal (@team/@me) bindings accept any internal-eligible deployment,
    including one still 'private' (not yet submitted for review).
    """
    registry.add_version(1, '1.0.0', publisher_id='someone-else', state='private')

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_deploy_app(conn, _request('publish', version=1, target='@team/Development'))

    assert result['success'] is True
    assert result['body']['publish']['state'] == 'enabled'


@pytest.mark.asyncio
async def test_public_publish_requires_dev_org_and_ready_deployment(registry, quiet_push):
    """A public binding needs a registered developer org AND an approved
    ('ready') deployment. The review state lives on the deployment: a fresh
    'private' version cannot go public until it is submitted and approved.
    """
    # A freshly-deployed app is deployment-state 'private' (not yet reviewed).
    registry.add_version(1, '1.0.0', publisher_id='u1', state='private')

    # No developerId on the org — no namespace is owned, so nothing publishes
    plain = _FakeConn(teams=_TEAMS, developer_id=None)
    refused = await handle_deploy_app(plain, _request('publish', version=1, target='@public'))
    assert refused['success'] is False
    assert 'developer' in refused['message']

    # Developer org, but the deployment is not yet approved — public refused.
    dev = _FakeConn(teams=_TEAMS, developer_id='acme')
    unapproved = await handle_deploy_app(dev, _request('publish', version=1, target='@public'))
    assert unapproved['success'] is False
    assert 'not approved for the store' in unapproved['message']

    # Once the deployment is 'ready' (approved), the public binding is created.
    registry.versions[0]['state'] = 'ready'
    result = await handle_deploy_app(dev, _request('publish', version=1, target='@public'))
    assert result['success'] is True
    assert result['body']['publish']['state'] == 'enabled'


@pytest.mark.asyncio
async def test_submit_flips_the_deployment_into_review(registry, quiet_push):
    """The 'Submit for review' verb flips the DEPLOYMENT private -> submit."""
    registry.add_version(1, '1.0.0', publisher_id='u1', state='private')
    dev = _FakeConn(teams=_TEAMS, developer_id='acme')

    result = await handle_deploy_app(dev, _request('submit', version=1))
    assert result['success'] is True
    assert result['body']['artifact']['state'] == 'submit'
    assert registry.versions[0]['state'] == 'submit'

    # Only the developer namespace owner may submit.
    outsider = _FakeConn(teams=_TEAMS, developer_id='evil')
    refused = await handle_deploy_app(outsider, _request('submit', version=1))
    assert refused['success'] is False


@pytest.mark.asyncio
async def test_publish_blocked_outside_developer_namespace(registry, quiet_push):
    """THE cross-org guarantee: an org can only publish app ids inside its own
    developer namespace — to ANY audience. An attacker who got a rival's (or a
    forged, via the pipe path) app id onto their own rail still cannot bind it,
    because acme.brandy is not in the 'evil' namespace. This is what makes
    cross-org impersonation of an app id impossible.
    """
    registry.add_version(1, '9.9.9', publisher_id='u1', state='ready')
    attacker = _FakeConn(teams=_TEAMS, developer_id='evil')

    # Every audience is refused — public, team, and self alike.
    for target in ('@public', '@team/Development', '@user'):
        refused = await handle_deploy_app(attacker, _request('publish', version=1, target=target))
        assert refused['success'] is False
        assert 'namespace' in refused['message'], target
    # Nothing was ever bound.
    assert registry.set_calls == []


@pytest.mark.asyncio
async def test_publish_allowed_inside_own_namespace(registry, quiet_push):
    """The guarantee never blocks the owner: acme may publish acme.brandy."""
    registry.add_version(1, '1.0.0', publisher_id='u1', state='ready')
    registry.add_version(2, '2.0.0', publisher_id='u1', state='ready')
    owner = _FakeConn(teams=_TEAMS, developer_id='acme')

    result = await handle_deploy_app(owner, _request('publish', version=2, target='@public'))
    assert result['success'] is True


@pytest.mark.asyncio
async def test_publish_failed_artifact_never_publishes(registry, quiet_push):
    """A 'failed' artifact is unpublishable to ANY audience."""
    registry.add_version(1, '1.0.0', publisher_id='u1', state='failed')
    conn = _FakeConn(teams=_TEAMS)
    result = await handle_deploy_app(conn, _request('publish', version=1, target='@team/Development'))
    assert result['success'] is False
    assert 'failed processing' in result['message']


# =============================================================================
# WHERE — the caller-visible reverse index
# =============================================================================


@pytest.mark.asyncio
async def test_where_lists_visible_rows_with_deployment_states(registry):
    """The reverse index shows the caller's user/team bindings + the public
    binding; each pin's ``state`` is the bound DEPLOYMENT's review state.
    """
    registry.add_version(1, '1.0.0', state='ready')
    registry.add_version(2, '1.1.0', state='submit')
    registry.seed_publish(AUD_USER, 1)
    registry.seed_publish(AUD_TEAM, 2)
    registry.seed_publish(AUD_PUBLIC, 2)
    registry.seed_publish({'type': 'user', 'id': 'someone-else'}, 2)  # invisible

    conn = _FakeConn(teams=_TEAMS)  # developer of acme.brandy -> sees all states
    result = await handle_deploy_app(conn, _request('where'))

    pins = {(p['rung'], p['handle'], p['version'], p['state']) for p in result['body']['pins']}
    assert pins == {
        ('personal', '@user', 1, 'ready'),
        ('team', '@team/Development', 2, 'submit'),
        ('public', '@public', 2, 'submit'),
    }


# =============================================================================
# DISABLE / REMOVE — audience state flips
# =============================================================================


@pytest.mark.asyncio
async def test_disable_flips_the_audience_row(registry):
    """disable/remove resolve the target audience and flip its row state."""
    registry.add_version(1, '1.0.0')
    registry.seed_publish(AUD_TEAM, 1)

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_deploy_app(conn, _request('disable', target='@team/Development'))
    assert result['success'] is True
    assert result['body']['publish']['state'] == 'disabled'


# =============================================================================
# ENTRY — mint a signed URL for ONE version, entitlement-checked, int-only
# =============================================================================


@pytest.mark.asyncio
async def test_entry_is_registry_int_only(registry, mint):
    """Semver strings are display-only — entry refuses them."""
    registry.add_version(1, '1.0.0')
    registry.seed_publish(AUD_TEAM, 1)
    conn = _FakeConn(teams=_TEAMS)
    result = await handle_deploy_app(conn, _request('entry', version='1.0.0'))
    assert result['success'] is False
    assert 'registry version number' in result['message']


@pytest.mark.asyncio
async def test_entry_mints_for_visible_publishes(registry, mint):
    """A version served by a caller-visible row mints the signed entry URL."""
    registry.add_version(1, '1.0.0')
    registry.seed_publish(AUD_TEAM, 1)

    conn = _FakeConn(teams=_TEAMS)
    result = await handle_deploy_app(conn, _request('entry', version=1))

    body = result['body']
    # Zip-mode artifacts derive the serving dir by convention
    assert body['url'] == 'https://signed/orgs/org1/files/.apps/acme.brandy/v000001/app/remoteEntry.js?sub=app-entry'
    assert body['moduleId'] == 'acme_brandy'
    assert (body['appVersion'], body['registryVersion']) == ('1.0.0', 1)


@pytest.mark.asyncio
async def test_entry_public_counts_only_when_deployment_ready(registry, mint):
    """A public binding on an un-approved deployment grants nothing; once the
    DEPLOYMENT is 'ready' it serves everyone.
    """
    registry.add_version(1, '1.0.0', publisher_id='someone-else', state='submit')
    registry.seed_publish(AUD_PUBLIC, 1)

    conn = _FakeConn(teams=_TEAMS)
    blocked = await handle_deploy_app(conn, _request('entry', version=1))
    assert blocked['success'] is False

    registry.versions[0]['state'] = 'ready'
    served = await handle_deploy_app(conn, _request('entry', version=1))
    assert served['success'] is True


@pytest.mark.asyncio
async def test_entry_deployer_is_entitled(registry, mint):
    """The deployer of a version may mint it before any publish exists."""
    registry.add_version(1, '1.0.0', publisher_id='u1')
    conn = _FakeConn(teams=_TEAMS)
    result = await handle_deploy_app(conn, _request('entry', version=1))
    assert result['success'] is True


@pytest.mark.asyncio
async def test_entry_rejects_non_app_artifacts(registry, mint):
    """Pipeline deployments share the registry — entry only mints app artifacts."""
    registry.add_version(1, '1.0.0', publisher_id='u1', kind='pipeline')
    conn = _FakeConn(teams=_TEAMS)
    result = await handle_deploy_app(conn, _request('entry', version=1))
    assert result['success'] is False
    assert 'not an app artifact' in result['message']


# =============================================================================
# RESOLVE — the manifest scope walk (user > team > public; serving gate)
# =============================================================================


@pytest.mark.asyncio
async def test_resolve_app_pins_most_specific_audience_wins(registry, mint):
    """On id collisions the user binding beats team, team beats public."""
    registry.add_version(1, '1.0.0', state='ready')
    registry.add_version(2, '1.1.0', state='private')
    registry.seed_publish(AUD_PUBLIC, 1)  # deployment 1 is 'ready' -> public serves
    registry.seed_publish(AUD_USER, 2)  # deployment 2 is 'private' -> internal serves

    resolved = await resolve_app_pins('org1', 'u1', ['t1'])

    assert len(resolved) == 1
    assert resolved[0]['id'] == 'acme.brandy'
    assert resolved[0]['version'] == '1.1.0'
    assert resolved[0]['public'] is False


@pytest.mark.asyncio
async def test_resolve_public_serves_only_ready_deployments(registry, mint):
    """A public binding whose deployment is not 'ready' never reaches the store;
    a disabled binding never serves.
    """
    registry.add_version(1, '1.0.0', state='submit')
    registry.seed_publish(AUD_PUBLIC, 1)  # deployment still in review -> hidden

    resolved = await resolve_app_pins('org1', 'other-user', [])
    assert resolved == []

    # Approve the deployment -> the public store now serves it.
    registry.versions[0]['state'] = 'ready'
    resolved = await resolve_app_pins('org1', 'other-user', [])
    assert [r['id'] for r in resolved] == ['acme.brandy']
    assert resolved[0]['public'] is True


@pytest.mark.asyncio
async def test_resolve_internal_serves_unapproved_but_not_failed(registry, mint):
    """Internal (team/user) bindings serve any internal-eligible deployment
    ('private'/'submit'/'ready') but never a 'failed' one.
    """
    registry.add_version(1, '1.0.0', state='submit')
    registry.seed_publish(AUD_TEAM, 1)

    resolved = await resolve_app_pins('org1', 'u1', ['t1'])
    assert [r['id'] for r in resolved] == ['acme.brandy']

    registry.versions[0]['state'] = 'failed'
    resolved = await resolve_app_pins('org1', 'u1', ['t1'])
    assert resolved == []
