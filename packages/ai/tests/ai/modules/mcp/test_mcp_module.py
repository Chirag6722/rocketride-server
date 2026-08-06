# Copyright 2026 Aparavi Software AG. MIT License.
import json

import pytest


def test_module_exposes_initmodule():
    import ai.modules.mcp as mcp_module

    assert hasattr(mcp_module, 'initModule')
    assert callable(mcp_module.initModule)


@pytest.mark.asyncio
async def test_build_mcp_server_lists_tools_from_real_registry(fake_engine):
    """End-to-end smoke over the v2 in-memory `Client`, against the real
    `register_all` registry -- dispatch is registry-based now, not the old
    dynamic per-pipeline surface. See test_handlers.py for the
    registry-population/dispatch cases, test_cache_policy.py for the
    cache-hint assertions this test intentionally does not duplicate.

    Covers: tool discovery (count + full set + order pin), one call_tool
    round trip (`list_running_pipelines` against `fake_engine`), one
    read_resource round trip (`rocketride://status`), and -- new in v2 --
    that the auto-mode `Client` actually discovered the server (protocol
    version negotiated via `server/discover`) and that capabilities are
    auto-derived from registered handlers: tools + resources, no prompts
    (sdk-api-notes.md §5, §7).
    """
    from mcp.client import Client
    from ai.modules.mcp.handlers import build_mcp_server

    server = build_mcp_server(lambda: fake_engine)
    async with Client(server) as client:
        # v2 auto-mode does a `server/discover` probe on entry.
        assert client.protocol_version == '2026-07-28'

        result = await client.list_tools()
        names = [t.name for t in result.tools]
        assert set(names) == {
            'list_components',
            'describe_component',
            'validate_pipeline',
            'describe_pipeline',
            'run_pipeline',
            'run_dropper_pipe',
            'send_data',
            'terminate',
            'send_files',
            'store_read',
            'store_list',
            'store_stat',
            'store_get_url',
            'save_template',
            'load_template',
            'deploy_add',
            'deploy_list',
            'deploy_status',
            'deploy_remove',
            'deploy_update',
            'monitor',
            'list_running_pipelines',
            'log_chapters',
            'log_read',
            'log_traces',
            'log_trace',
        }
        # Order pin (see also test_cache_policy.py::test_list_tools_order_is_deterministic_and_pinned).
        assert names[0] == 'list_components'
        assert names[-1] == 'log_trace'

        call_result = await client.call_tool('list_running_pipelines', {})
        assert call_result.is_error is False
        payload = json.loads(call_result.content[0].text)
        assert payload['ok'] is True
        assert payload['count'] == len(payload['tasks'])
        assert payload['tasks'][0]['token']

        resource_result = await client.read_resource('rocketride://status')
        resource_payload = json.loads(resource_result.contents[0].text)
        assert resource_payload['connected'] is True

    # Capabilities are auto-derived from whatever's registered (no separate
    # declaration step) -- tools/resources handlers are registered, prompts
    # never are.
    caps = server.get_capabilities(protocol_version='2026-07-28')
    assert caps.tools is not None
    assert caps.resources is not None
    assert caps.prompts is None


@pytest.mark.asyncio
async def test_initmodule_mounts_mcp_route(monkeypatch, fake_engine):
    from fastapi import FastAPI
    import ai.modules.mcp as mcp_module

    monkeypatch.setattr(mcp_module, 'make_engine_client', lambda cfg, on_event=None: fake_engine)

    class FakeServer:
        def __init__(self):
            self.app = FastAPI()
            self.public = set()

        def add_route(self, path, handler, methods, public=False):
            self.app.add_api_route(path, handler, methods=methods)
            if public:
                self.public.add(path)

        def is_public_route(self, path):
            return path in self.public

    srv = FakeServer()
    mcp_module.initModule(srv, {'mcp_dev_no_auth': True})
    paths = {getattr(r, 'path', None) for r in srv.app.routes}
    assert any(p and p.startswith('/mcp') for p in paths)


@pytest.mark.asyncio
async def test_shutdown_without_client_does_not_raise(monkeypatch, fake_engine):
    """No engine client was ever created (_state['client'] stays None) —
    shutdown must still drain the session manager cleanly without raising.

    Baseline coverage for the _shutdown() path with the client branch a
    no-op, pinning down that `_stack.aclose()` (session-manager teardown)
    alone completes without error.
    """
    from fastapi import FastAPI
    import ai.modules.mcp as mcp_module

    monkeypatch.setattr(mcp_module, 'make_engine_client', lambda cfg, on_event=None: fake_engine)

    class FakeServer:
        def __init__(self):
            self.app = FastAPI()
            self.public = set()

        def add_route(self, path, handler, methods, public=False):
            self.app.add_api_route(path, handler, methods=methods)
            if public:
                self.public.add(path)

        def is_public_route(self, path):
            return path in self.public

    srv = FakeServer()
    mcp_module.initModule(srv, {'mcp_dev_no_auth': True})

    # engine_factory (and therefore make_engine_client) is never invoked, so
    # _state['client'] stays None all the way through shutdown.
    for handler in srv.app.router.on_startup:
        await handler()
    for handler in srv.app.router.on_shutdown:
        await handler()


@pytest.mark.asyncio
async def test_shutdown_closes_engine_client_after_session_manager(monkeypatch):
    """When a request has already lazily created the engine client, shutdown
    must still close it — the reordering to drain-then-close must not turn
    into "never close".

    Drives the module through its real startup/shutdown lifespan hooks and
    the actual `mcp_server` object `initModule` builds internally (captured
    via the `build_mcp_server` seam), so
    `engine_factory()` fires through the real closure created inside
    `initModule` rather than a stand-in built directly in the test. This
    avoids driving the raw HTTP/session-manager transport at all — v1's
    `streamable_http_client` yielded a 3-tuple `(read_stream, write_stream,
    get_session_id)`; v2 narrowed that to 2, and the module's own lifespan
    hooks are sufficient to exercise "engine client closed after session
    manager teardown" without depending on that wire-level shape.
    """
    from fastapi import FastAPI
    from mcp.client import Client

    import ai.modules.mcp as mcp_module

    close_events = []

    class FakeClosableEngine:
        async def list_tasks(self):
            return []

        async def deploy_list(self):
            return []

        async def close(self):
            close_events.append('closed')

    monkeypatch.setattr(mcp_module, 'make_engine_client', lambda cfg, on_event=None: FakeClosableEngine())

    captured = {}
    real_build_mcp_server = mcp_module.build_mcp_server

    def _capturing_build_mcp_server(engine_factory, task_registry=None):
        server = real_build_mcp_server(engine_factory, task_registry)
        captured['server'] = server
        return server

    monkeypatch.setattr(mcp_module, 'build_mcp_server', _capturing_build_mcp_server)

    class FakeServer:
        def __init__(self):
            self.app = FastAPI()
            self.public = set()

        def add_route(self, path, handler, methods, public=False):
            self.app.add_api_route(path, handler, methods=methods)
            if public:
                self.public.add(path)

        def is_public_route(self, path):
            return path in self.public

    srv = FakeServer()
    mcp_module.initModule(srv, {'mcp_dev_no_auth': True})

    for handler in srv.app.router.on_startup:
        await handler()

    # Reading a resource (unlike list_tools, which is purely registry-based
    # and never touches the engine) routes through engine_factory(), lazily
    # creating _state['client'] inside the initModule closure.
    async with Client(captured['server']) as client:
        await client.read_resource('rocketride://status')

    assert close_events == []  # not yet — only shutdown closes it

    for handler in srv.app.router.on_shutdown:
        await handler()

    assert close_events == ['closed']
