# MCP 2026-07-28 Phase-1 Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the HTTP MCP server (`packages/ai/src/ai/modules/mcp`, 23-tool surface) from `mcp` SDK v1 (`>=1.9.0`) to v2 (2026-07-28 spec): transport rewiring, required cache hints on list results, deterministic-order pinning, and the test-harness rewrite. Auth broker is explicitly OUT of scope (deferred — audit §3).

**Architecture:** Minimal-delta migration. Keep the existing design — low-level MCP `Server` + `ToolRegistry` dispatch + token-keyed `TaskRegistry` — and swap only the layers v2 changes: the Streamable-HTTP transport wiring in `__init__.py`, result construction where cache hints now live, and the client side of the tests. The server already runs stateless with token-keyed cross-call state, which is the v2-blessed pattern, so no state-model changes.

**Tech Stack:** Python 3.10+, FastAPI/Starlette (existing `ai/web`), `mcp` v2 (2026-07-28 line), pytest + pytest-asyncio.

## Global Constraints

- **Branch:** `feat/http-mcp` (already checked out). Base for any PR: `develop`.
- **MIT header** on every new file: `# Copyright 2026 Aparavi Software AG. MIT License.`
- **Python:** single quotes, `python -m ruff check` / `ruff format` clean, Python 3.10+.
- **Never** stage/commit/push anything under `pipelines/` (gitignored sandbox).
- **Suite floor:** `packages/ai` tests were 1532 passed / 0 failed before this work; every task ends ≥ that bar for the files it touches, full suite green at plan end. `./builder nodes:test` must stay ≥310 (untouched by this plan — don't regress it).
- **Conventional commits**: `feat(ai,mcp): ...` / `test(ai,mcp): ...` / `chore(ai,mcp): ...`.
- **SDK ground truth:** Task 1 produces `claude/tasks/mcp-spec-2026-07-28-migration/sdk-api-notes.md` from the *installed* v2 package. That file is authoritative over this plan's v2 API spellings. If a class/kwarg name below differs from what Task 1 records, **update this plan file in place first** (mechanical rename, not a redesign), then execute. Names most likely to need this: the session-manager/transport class, cache-hint field names (`ttlMs`/`cacheScope` vs snake_case aliases), and the in-memory test client entry point.
- **Do NOT change:** `ToolRegistry`/`TaskRegistry` semantics, tool handler signatures (`handler(engine, task_registry, arguments)`), the in-band `{ok: ...}` error envelope, the 23-tool surface, or the auth seam behavior.

---

### Task 1: SDK recon + pin bump (the gate for everything else)

**Files:**
- Modify: `packages/ai/src/ai/requirements.txt` (the `mcp>=1.9.0` line)
- Create: `claude/tasks/mcp-spec-2026-07-28-migration/sdk-api-notes.md`

**Interfaces:**
- Produces: `sdk-api-notes.md` — the authoritative v2 API record every later task consults; the new exact pin (e.g. `mcp>=2.0.0,<3`).

- [ ] **Step 1: Discover the exact stable v2 version**

Run: `pip index versions mcp 2>/dev/null || pip install mcp== 2>&1 | head -3`
Expected: the available-versions list. Record the newest stable 2.x (per the SDK blog, stable v2 targeted 2026-07-27; if only `2.0.0bN` pre-releases exist, STOP and surface to Dylan — pinning a beta is a decision he must make, per open-questions #1).

- [ ] **Step 2: Install v2 into the project venv**

Run: `pip install 'mcp==<version from step 1>'`
Expected: clean install (watch for the `httpx2` dependency replacing `httpx`+`httpx-sse` — record any dependency conflicts with other `packages/ai` pins).

- [ ] **Step 3: Interrogate the installed package and write `sdk-api-notes.md`**

Run each probe and record the answer with the exact import path + signature (use `python -c "import inspect, mcp; ..."` / read the installed source under `$(python -c 'import mcp, os; print(os.path.dirname(mcp.__file__))')`):

```
1. Low-level Server: does `mcp.server.lowlevel.Server` still exist? Do
   @server.list_tools()/@server.call_tool()/@server.list_resources()/@server.read_resource()
   decorators keep their v1 shapes? Signature diffs?
2. Transport: fate of `mcp.server.streamable_http_manager.StreamableHTTPSessionManager`
   (exists / renamed / replaced by ServerRunner-based ASGI app). What is the v2 way to get
   a stateless Streamable-HTTP ASGI app from a low-level Server, and what lifespan does it
   need? (v2 docs mention `streamable_http_app()` on the high-level MCPServer — find the
   low-level equivalent.)
3. Cache hints: exact field names + types for ttlMs/cacheScope on ListToolsResult,
   ListResourcesResult, ReadResourceResult (`grep -ri 'ttl' <sdk-dir>/types.py`), and HOW a
   low-level handler sets them (return-type change vs kwargs vs result post-processing hook).
4. Ordering: does the SDK sort tools itself or preserve handler order?
5. server/discover: SDK-provided? How are capabilities populated for a low-level Server?
6. Dual-revision: confirm one endpoint answers BOTH legacy initialize (2025-11-25) and
   2026-07-28 self-describing posts ("one endpoint serves both protocol eras side by side"
   per the v2 announcement). Record any opt-in flag.
7. Test client: v2 in-memory client API (`mcp.client.Client(target)` where target can be a
   server object / ASGI app?), and the fate of `ClientSession` + `streamable_http_client`.
8. Error types: `HardError` equivalents / how exceptions from call_tool map to JSON-RPC now.
```

- [ ] **Step 4: Bump the pin**

In `packages/ai/src/ai/requirements.txt` change `mcp>=1.9.0` to the recorded stable pin (e.g. `mcp>=2.0.0,<3`).

- [ ] **Step 5: Reconcile this plan**

Diff every class/kwarg named in Tasks 2–6 against `sdk-api-notes.md`; fix spellings in this plan file in place. If the transport architecture differs *structurally* (not just names) from Task 2's sketch, STOP and re-plan Task 2 with Dylan before proceeding.

- [ ] **Step 6: Commit**

```bash
git add packages/ai/src/ai/requirements.txt claude/tasks/mcp-spec-2026-07-28-migration/sdk-api-notes.md
git commit -m "chore(ai,mcp): bump mcp SDK to v2 (2026-07-28 spec) + record v2 API recon"
```

---

> **RECONCILED 2026-07-29 after Task 1 recon** (`sdk-api-notes.md` is authoritative): the transport
> layer is UNCHANGED in v2 (same `StreamableHTTPSessionManager` import/kwargs/`run()`), so Task 2 is
> now verification-only. The real breaking change is the removal of the low-level decorator API —
> Task 3 carries the port. Cache hints are plain result fields (`ttl_ms`/`cache_scope`, wire-aliased
> `ttlMs`/`cacheScope`). Tests use the v2 in-memory `Client(server)`. Tasks 2–5 below are rewritten
> accordingly; original text superseded.

### Task 2: Transport verification + baseline breakage inventory (no code changes expected)

**Files:**
- Read (verify, do NOT modify unless a check fails): `packages/ai/src/ai/modules/mcp/__init__.py`
- Create: `.superpowers/sdd/plan/task-2-baseline.md` (breakage inventory — workspace artifact, never committed)

**Interfaces:**
- Consumes: `sdk-api-notes.md` §2 (transport unchanged: same import path, same kwargs `app`/`event_store`/`json_response`/`stateless`, same `.handle_request()`/`.run()`).
- Produces: the baseline breakage inventory Task 3 consumes; confirmation that `initModule` needs zero changes.

- [ ] **Step 1: Verify the transport block against the installed v2 SDK**

Run: `python -c "from mcp.server.streamable_http_manager import StreamableHTTPSessionManager; import inspect; print(inspect.signature(StreamableHTTPSessionManager.__init__))"`
Expected: signature contains `app`, `event_store`, `json_response`, `stateless` (plus new optional `session_idle_timeout`, `max_request_body_size`). If any of the four v1 kwargs is missing, STOP and report BLOCKED — the recon was wrong.

- [ ] **Step 2: Import-check the module against v2**

Run: `python -c "import sys; sys.path.insert(0, 'packages/ai/src'); import ai.modules.mcp"`
Expected: either clean import, or an error rooted in `handlers.py`'s removed decorator API (Task 3's surface) — record which. An error rooted in `__init__.py`'s transport imports means BLOCKED (recon wrong).

- [ ] **Step 3: Run the full module test dir and write the breakage inventory**

Run: `python -m pytest packages/ai/tests/ai/modules/mcp/ -q 2>&1 | tail -25`
Expected: failures/errors rooted in `handlers.py`'s `@server.list_tools()`/`@server.call_tool()`/`@server.list_resources()`/`@server.read_resource()` calls (AttributeError at `build_mcp_server` time) — Task 3's surface. Write the inventory (each failing test file + root-cause line) to `.superpowers/sdd/plan/task-2-baseline.md`. Any failure NOT rooted in the decorator removal gets a ⚠️ marker in the inventory — Task 3 must not silently absorb unknowns.

- [ ] **Step 4: No commit**

This task changes no tracked files (the inventory is workspace scratch). If Steps 1–2 forced a code change, that IS a plan deviation — stop and report it rather than committing.

---

### Task 3: Port `handlers.py` from the removed decorator API to v2 constructor kwargs

**Files:**
- Modify: `packages/ai/src/ai/modules/mcp/handlers.py` (`build_mcp_server` — the four `@server.*` decorated handlers become module-level-style closures passed as `Server(...)` constructor kwargs)
- Modify: `packages/ai/src/ai/modules/mcp/tooling.py` (`ToolRegistry.tools()`: `types.Tool(..., inputSchema=...)` → `input_schema=` — v2 renamed the python-side field; wire alias stays `inputSchema`. Baseline failure: `test_framework.py::test_tool_registry_register_and_tools`)
- Test: `packages/ai/tests/ai/modules/mcp/test_handlers.py`, `packages/ai/tests/ai/modules/mcp/test_mcp_module.py` (replace `request_handlers` introspection with the v2 in-memory `Client`), `test_framework.py` (any `inputSchema` attribute assertions → `input_schema`)

**Interfaces:**
- Consumes: `sdk-api-notes.md` §1 (constructor kwargs + handler signatures — note the fix-round correction: `on_call_tool`/`on_read_resource` params are REQUIRED, not `| None`), §7 (in-memory `Client`), §8 (`MCPError`); the Task 2 baseline inventory (`.superpowers/sdd/plan/task-2-baseline.md`).
- Produces: `build_mcp_server(engine_factory, task_registry=None) -> Server` — signature unchanged, callers (`__init__.py:58`, tests) untouched. Registry tool handlers keep the `handler(engine, task_registry, arguments) -> dict` contract. Task 4 relies on the four v2 handlers being named `_on_list_tools`, `_on_call_tool`, `_on_list_resources`, `_on_read_resource` inside `build_mcp_server`.

- [ ] **Step 1: Port the four handlers in `handlers.py`**

Replace the decorator block in `build_mcp_server` with closures + constructor kwargs (existing `ToolRegistry`/dispatch logic is unchanged — only the MCP-facing shells change):

```python
from mcp.shared.exceptions import MCPError

    async def _on_list_tools(ctx, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=registry.tools())

    async def _on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        name, arguments = params.name, dict(params.arguments or {})
        handler = registry.handler(name)
        if handler is None:
            result = {
                'ok': False,
                'error_type': 'UnknownTool',
                'message': f'Unknown tool: {name}',
                'hint': f'Call list_tools to see the {len(registry.names())} available tool(s).',
            }
        else:
            try:
                result = await handler(engine_factory(), task_registry, arguments)
            except HardError as exc:
                # v2 maps arbitrary exceptions to a generic -32603 'Internal server
                # error', which would swallow the message agents need (connection
                # lost, auth failed). MCPError carries it to the wire verbatim.
                raise MCPError(types.INTERNAL_ERROR, exc.message) from exc
            except Exception as exc:  # noqa: BLE001 - normalized below
                logger.exception('Unhandled exception in MCP tool %r', name)
                result = normalize_error(exc)
        return types.CallToolResult(
            content=[types.TextContent(type='text', text=json.dumps(result, default=str))]
        )

    async def _on_list_resources(ctx, params: types.PaginatedRequestParams | None) -> types.ListResourcesResult:
        return types.ListResourcesResult(resources=resources_mod.list_resources())

    async def _on_read_resource(ctx, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        text = await resources_mod.read_resource(engine_factory(), str(params.uri))
        return types.ReadResourceResult(contents=[
            types.TextResourceContents(uri=params.uri, mimeType='application/json', text=text)
        ])

    server = Server(
        'rocketride-mcp',
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
        on_list_resources=_on_list_resources,
        on_read_resource=_on_read_resource,
    )
```

Rules:
- The `{ok: ...}` in-band JSON envelope inside the `TextContent` is a published contract (doc.md) — byte-for-byte identical to v1 output.
- `HardError` (ours, `errors.py:19`) previously propagated as a v1 tool error carrying its message; under v2's catch-all it would become a generic `Internal server error`. The `MCPError(types.INTERNAL_ERROR, exc.message)` mapping preserves the message on the wire. If `types.INTERNAL_ERROR` doesn't exist in v2's mirror, use the literal `-32603`.
- Do not register `on_ping`/logging/roots kwargs (deprecated per SEP-2577; we never used them).
- `mimeType='application/json'` matches the v1 resources' declared mimeType (`resources.py`).

- [ ] **Step 2: Rewrite the two introspection-style tests onto the in-memory Client**

In `test_mcp_module.py`, `test_build_mcp_server_lists_tools_from_real_registry` becomes:

```python
@pytest.mark.asyncio
async def test_build_mcp_server_lists_tools_from_real_registry(fake_engine):
    from mcp.client import Client
    from ai.modules.mcp.handlers import build_mcp_server

    server = build_mcp_server(lambda: fake_engine)
    async with Client(server) as client:
        result = await client.list_tools()
    names = {t.name for t in result.tools}
    assert names == { ... }   # keep the existing 23-name set literal verbatim
```

Apply the same Client-based pattern to every `request_handlers[...]` introspection in `test_handlers.py` (list/call/resource cases). Dispatch-behavior assertions (unknown tool → `{ok: False, error_type: 'UnknownTool'}` envelope, `normalize_error` shaping) keep asserting on the parsed JSON from `result.content[0].text`. Add one NEW test pinning the HardError mapping:

```python
@pytest.mark.asyncio
async def test_hard_error_surfaces_message_as_mcp_error(fake_engine):
    from mcp.client import Client
    from mcp.shared.exceptions import MCPError
    from ai.modules.mcp.handlers import build_mcp_server
    from ai.modules.mcp.tooling import ToolRegistry

    registry = ToolRegistry()

    @registry.register('boom', 'always hard-fails', {'type': 'object', 'properties': {}})
    async def _boom(engine, task_registry, arguments):
        raise ConnectionError('engine unreachable')   # HARD_EXC_NAMES member

    server = build_mcp_server(lambda: fake_engine, registry=None)
    # build with the real registry, then hit the private path via a monkeypatched
    # register_all is NOT needed: simplest is to build a server whose registry
    # contains only `boom` — if build_mcp_server does not accept a registry today,
    # extend it with an optional keyword-only `registry=None` test seam (default
    # builds the real one), preserving the public call shape.
    with pytest.raises(MCPError) as exc_info:
        async with Client(server) as client:
            await client.call_tool('boom', {})
    assert 'engine unreachable' in str(exc_info.value)
```

(If the in-memory Client surfaces server-side `MCPError` as a raised `MCPError` client-side — per `sdk-api-notes.md` §8 it does — the assertion holds; adjust the expect-shape to whatever the notes' probe-8 snippet showed if not.)

- [ ] **Step 3: Run the module test dir**

Run: `python -m pytest packages/ai/tests/ai/modules/mcp/ -q 2>&1 | tail -10`
Expected: everything from the Task 2 baseline inventory now PASSES except tests that import v1 client APIs directly (Task 5's surface, if any) — cross-check against the inventory; nothing new may break.

- [ ] **Step 4: Lint**

Run: `python -m ruff check packages/ai/src/ai/modules/mcp packages/ai/tests/ai/modules/mcp && python -m ruff format --check packages/ai/src/ai/modules/mcp packages/ai/tests/ai/modules/mcp`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add packages/ai/src/ai/modules/mcp/handlers.py packages/ai/tests/ai/modules/mcp/test_handlers.py packages/ai/tests/ai/modules/mcp/test_mcp_module.py
git commit -m "feat(ai,mcp): port MCP handlers to SDK v2 constructor-kwarg API"
```

---

### Task 4: Cache hints (`ttlMs`/`cacheScope`) + deterministic-order pin

**Files:**
- Modify: `packages/ai/src/ai/modules/mcp/handlers.py` (`_list_tools`, `_list_resources`, `_read_resource`)
- Create: `packages/ai/src/ai/modules/mcp/cache_policy.py`
- Test: `packages/ai/tests/ai/modules/mcp/test_cache_policy.py` (new), `test_handlers.py` (ordering pin)

**Interfaces:**
- Consumes: `sdk-api-notes.md` §3 — mechanism decision (controller, reconciliation): use the **explicit result-field route** everywhere (`types.ListToolsResult(tools=..., ttl_ms=..., cache_scope=...)`), NOT the server-wide `cache_hints=` kwarg, because `resources/read` needs per-URI TTLs which the per-method kwarg cannot express, and one mechanism beats two. Also consumes §4 (SDK preserves handler order; no sorting) and Task 3's `_on_list_tools`/`_on_list_resources`/`_on_read_resource` closures.
- Produces: `cache_policy.py` constants: `TOOLS_TTL_MS = 3_600_000`, `RESOURCES_LIST_TTL_MS = 30_000`, `STATUS_READ_TTL_MS = 0`, `PIPELINES_READ_TTL_MS = 30_000`, `CACHE_SCOPE = 'private'`.

Policy rationale (from audit §2): the 23-tool surface is static per build → long TTL; but scope is `private` because the node-auth workstream plans entitlement-filtered listings (open-questions #4). `rocketride://status` is live state → no caching. `rocketride://pipelines` reflects running tasks → 30s.

- [ ] **Step 1: Write the failing tests**

```python
# packages/ai/tests/ai/modules/mcp/test_cache_policy.py
# Copyright 2026 Aparavi Software AG. MIT License.
import pytest

from mcp.client import Client


@pytest.mark.asyncio
async def test_list_tools_carries_cache_hints(fake_engine):
    from ai.modules.mcp.handlers import build_mcp_server
    from ai.modules.mcp.cache_policy import TOOLS_TTL_MS, CACHE_SCOPE

    server = build_mcp_server(lambda: fake_engine)
    async with Client(server) as client:
        result = await client.list_tools()
    # python-side snake_case fields, wire-aliased ttlMs/cacheScope (sdk-api-notes.md §3)
    assert result.ttl_ms == TOOLS_TTL_MS
    assert result.cache_scope == CACHE_SCOPE


@pytest.mark.asyncio
async def test_list_tools_order_is_deterministic_and_pinned(fake_engine):
    """Spec 2026-07-28 SHOULD: deterministic order for client/prompt caching.
    Pins the exact registration order from tools/__init__.register_all:
    introspection, execution, capability, visibility."""
    from ai.modules.mcp.handlers import build_mcp_server

    server = build_mcp_server(lambda: fake_engine)
    async with Client(server) as client:
        names_1 = [t.name for t in (await client.list_tools()).tools]
        names_2 = [t.name for t in (await client.list_tools()).tools]
    assert names_1 == names_2
    assert names_1[:4] == ['list_components', 'describe_component', 'validate_pipeline', 'describe_pipeline']
    assert names_1[-3:] == ['monitor', 'list_running_pipelines', 'get_pipeline_trace']
    assert len(names_1) == 23


@pytest.mark.asyncio
async def test_list_resources_carries_cache_hints(fake_engine):
    from ai.modules.mcp.handlers import build_mcp_server
    from ai.modules.mcp.cache_policy import RESOURCES_LIST_TTL_MS, CACHE_SCOPE

    server = build_mcp_server(lambda: fake_engine)
    async with Client(server) as client:
        result = await client.list_resources()
    assert result.ttl_ms == RESOURCES_LIST_TTL_MS
    assert result.cache_scope == CACHE_SCOPE


@pytest.mark.asyncio
@pytest.mark.parametrize('uri,ttl_name', [
    ('rocketride://status', 'STATUS_READ_TTL_MS'),
    ('rocketride://pipelines', 'PIPELINES_READ_TTL_MS'),
])
async def test_read_resource_carries_cache_hints(fake_engine, uri, ttl_name):
    from ai.modules.mcp import cache_policy
    from ai.modules.mcp.handlers import build_mcp_server

    server = build_mcp_server(lambda: fake_engine)
    async with Client(server) as client:
        result = await client.read_resource(uri)
    assert result.ttl_ms == getattr(cache_policy, ttl_name)
    assert result.cache_scope == cache_policy.CACHE_SCOPE
```

(If the v2 `Client` exposes different method spellings — e.g. `client.resources.read(...)` — adjust per `sdk-api-notes.md` §7; the assertions on `ttl_ms`/`cache_scope` stand either way.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest packages/ai/tests/ai/modules/mcp/test_cache_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: ai.modules.mcp.cache_policy`, then missing-field assertions.

- [ ] **Step 3: Implement**

```python
# packages/ai/src/ai/modules/mcp/cache_policy.py
# Copyright 2026 Aparavi Software AG. MIT License.
"""Cache-hint policy for 2026-07-28 CacheableResult fields.

Tools: static per build, but 'private' because listings become
entitlement-filtered when node-auth lands. Status: live state, uncacheable.
Pipelines resource: reflects running tasks.
"""

TOOLS_TTL_MS = 3_600_000
RESOURCES_LIST_TTL_MS = 30_000
STATUS_READ_TTL_MS = 0
PIPELINES_READ_TTL_MS = 30_000
CACHE_SCOPE = 'private'
```

Then wire the fields at each handler's return site in `handlers.py` (Task 3 made handlers construct full result objects, so this is adding kwargs):
- `_on_list_tools`: `types.ListToolsResult(tools=registry.tools(), ttl_ms=TOOLS_TTL_MS, cache_scope=CACHE_SCOPE)`
- `_on_list_resources`: `types.ListResourcesResult(resources=..., ttl_ms=RESOURCES_LIST_TTL_MS, cache_scope=CACHE_SCOPE)`
- `_on_read_resource`: per-URI — `STATUS_READ_TTL_MS` when `str(params.uri) == 'rocketride://status'`, `PIPELINES_READ_TTL_MS` when `'rocketride://pipelines'`, else `0`; always `cache_scope=CACHE_SCOPE`.
Import the constants at the top of `handlers.py` (`from .cache_policy import ...`).

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest packages/ai/tests/ai/modules/mcp/test_cache_policy.py packages/ai/tests/ai/modules/mcp/test_handlers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/ai/src/ai/modules/mcp/cache_policy.py packages/ai/src/ai/modules/mcp/handlers.py packages/ai/tests/ai/modules/mcp/test_cache_policy.py
git commit -m "feat(ai,mcp): required ttlMs/cacheScope cache hints + pinned deterministic tool order"
```

---

### Task 5: Test-harness rewrite (in-memory v2 client) + full-suite green

**Files:**
- Modify: `packages/ai/tests/ai/modules/mcp/test_mcp_module.py` (any `ClientSession`/`streamable_http_client` usage → v2 in-memory client), `conftest.py` (only if fixtures import v1 client APIs)
- Test: the whole `packages/ai/tests/ai/modules/mcp/` directory + full `packages/ai` suite

**Interfaces:**
- Consumes: `sdk-api-notes.md` §7 — the v2 in-memory client (expected: `mcp.client.Client(target)` where target is the server object; exact per notes).
- Produces: a `mcp_inmemory_client` pattern later live-verification reuses.

- [ ] **Step 1: Inventory the v1 client-API usage**

Run: `grep -rn 'ClientSession\|streamable_http_client' packages/ai/tests/ | grep -v __pycache__`
Expected: the end-to-end smoke path(s) in `test_mcp_module.py` (and possibly a helper). List them all.

- [ ] **Step 2: Rewrite each on the v2 client**

Expected shape (adjust per sdk-api-notes.md §7):

```python
from mcp.client import Client

async with Client(mcp_server) as client:   # in-memory: no port, no subprocess
    tools = await client.list_tools()
    assert len(tools.tools) == 23
```

The rewritten smoke must assert, over the in-memory connection: `list_tools` count + first/last names (order pin), one `call_tool` round-trip (`list_running_pipelines` against `fake_engine`), one `read_resource` (`rocketride://status`), that `list_tools` carries the cache hints (client-visible, not just handler-visible), and — new in v2 — that discovery happened: `client.protocol_version == '2026-07-28'` after entering the context (the auto-mode `Client` does a `server/discover` probe, `sdk-api-notes.md` §7), plus `server.get_capabilities(...)` advertises tools + resources and NOT prompts (§5 — capabilities are auto-derived from registered handlers).

- [ ] **Step 3: Run the module directory**

Run: `python -m pytest packages/ai/tests/ai/modules/mcp/ -q 2>&1 | tail -5`
Expected: ALL PASS, 0 failures.

- [ ] **Step 4: Run the full packages/ai suite + lint**

Run: `python -m pytest packages/ai/tests -q 2>&1 | tail -5 && python -m ruff check packages/ai/src/ai/modules/mcp packages/ai/tests/ai/modules/mcp`
Expected: ≥1532 passed / 0 failed (new tests raise the count), ruff clean. If unrelated suites broke via the `httpx2` dependency swap, STOP — surface the dependency conflict to Dylan rather than "fixing" other packages' pins unilaterally.

- [ ] **Step 5: Commit**

```bash
git add packages/ai/tests/ai/modules/mcp/
git commit -m "test(ai,mcp): rewrite MCP harness on SDK v2 in-memory client"
```

---

### Task 6: Dual-revision verification + docs

**Files:**
- Create: `packages/ai/tests/ai/modules/mcp/test_dual_revision.py`
- Modify: `packages/ai/src/ai/modules/mcp/doc.md` (protocol section), `packages/client-mcp/docs/index.md` (SSE-mode deprecation note)

**Interfaces:**
- Consumes: `sdk-api-notes.md` §6; the audit's compat-window strategy (§4: don't strand 2025-era clients).

- [ ] **Step 1: Write the failing dual-revision test**

This is the compat-window gate in code: a 2025-11-25 client must still connect after the bump. Simulate one at the HTTP layer (raw JSON-RPC, no SDK client) via Starlette's TestClient against the mounted app:

```python
# packages/ai/tests/ai/modules/mcp/test_dual_revision.py
# Copyright 2026 Aparavi Software AG. MIT License.
"""A legacy (2025-11-25) client must still complete initialize + tools/list
against the v2-mounted /mcp endpoint (SDK dual-revision serving)."""
import pytest


LEGACY_INIT = {
    'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
    'params': {
        'protocolVersion': '2025-11-25',
        'capabilities': {},
        'clientInfo': {'name': 'legacy-probe', 'version': '0.0.1'},
    },
}


@pytest.mark.asyncio
async def test_legacy_initialize_still_answered(mcp_test_app):
    resp = await mcp_test_app.post('/mcp', json=LEGACY_INIT, headers={
        'content-type': 'application/json',
        'accept': 'application/json, text/event-stream',
    })
    assert resp.status_code == 200
    body = _first_jsonrpc_payload(resp)   # helper: parse JSON or first SSE data: line
    assert 'result' in body
    assert body['result']['protocolVersion'] == '2025-11-25'
```

(`mcp_test_app` = httpx-style ASGI test client over the FastAPI app built by `initModule` with `FakeServer` + `fake_engine` — reuse the fixture pattern from `test_initmodule_mounts_mcp_route`, wrapped so the app's lifespan runs, e.g. `httpx.ASGITransport`/`httpx2` equivalent inside `LifespanManager`.)

```python
import json


def _first_jsonrpc_payload(resp):
    """Parse a JSON-RPC payload from either framing the endpoint may use."""
    ctype = resp.headers.get('content-type', '')
    if ctype.startswith('application/json'):
        return resp.json()
    # text/event-stream: first `data:` line carries the JSON-RPC message
    for line in resp.text.splitlines():
        if line.startswith('data:'):
            return json.loads(line[len('data:'):].strip())
    raise AssertionError(f'no JSON-RPC payload in response (content-type={ctype!r})')
```

- [ ] **Step 2: Run to verify current behavior**

Run: `python -m pytest packages/ai/tests/ai/modules/mcp/test_dual_revision.py -v`
Expected: PASS if the v2 SDK's dual-revision serving is on by default (per its announcement); FAIL if it needs an opt-in flag — then set that flag in `__init__.py` per `sdk-api-notes.md` §6 and re-run to PASS. Either way the test stays as the regression pin.

- [ ] **Step 3: Update the co-located docs**

- `doc.md`: protocol paragraph → served revisions ("2026-07-28 and 2025-11-25 via SDK dual-revision serving"), the new required request headers (`Mcp-Method`/`Mcp-Name` — SDK-validated), and the cache-hint policy table (values from `cache_policy.py` with the rationale one-liner).
- `packages/client-mcp/docs/index.md`: in the "SSE Mode" section add: the legacy HTTP+SSE transport is formally Deprecated in MCP 2026-07-28 (12-month window); the maintained surface is the Streamable-HTTP server in `ai/modules/mcp`.

- [ ] **Step 4: Verify docs build**

Run: `./builder docs:build 2>&1 | tail -5`
Expected: success — modulo the pre-existing develop-inherited broken-link failure (`/integrations/neo4j`); if that is still the only error, treat as pass (known issue, not ours).

- [ ] **Step 5: Commit**

```bash
git add packages/ai/tests/ai/modules/mcp/test_dual_revision.py packages/ai/src/ai/modules/mcp/doc.md packages/client-mcp/docs/index.md
git commit -m "test(ai,mcp): pin dual-revision serving; docs for 2026-07-28 migration"
```

---

### Task 7: Live verification against a real engine (manual gate, no code)

**Files:** none (verification only; results recorded in this plan's Review section)

**Interfaces:**
- Consumes: the in-process live-verification pattern from the 25-tool port (in-process ASGI harness built from `initModule`, real `WsEngineClient` via `ROCKETRIDE_URI=http://localhost:5565` + `ROCKETRIDE_AUTH=MYAPIKEY`) — see `claude/research/connectors-project/http-mcp/mcp-deployment.md` decisions-log 2026-07-23 for why the in-process server (not the engine's own `/mcp` route) is the correct target.

- [ ] **Step 1: Boot check** — engine running on :5565 (if the local dist engine is still codesign-broken, use the App-Support-engine workaround documented in memory `engine_codesign_sigkill`).
- [ ] **Step 2: Run the v2 in-memory client smoke from Task 5 with the REAL engine client** (same env-var pattern as the 25-tool verification): `list_tools` (23, ordered), `run_dropper_pipe` with `pipelineTraceLevel='summary'` → token, POST a small file to the returned `upload_url`, `monitor` until terminal, `get_pipeline_trace` drain returns ≥1 FLOW event, `terminate`.
- [ ] **Step 3: Record results** in a `## Review` section appended to this plan: pass/fail per sub-check, any deviations, and the exact `mcp` version verified.
- [ ] **Step 4: Final commit** (plan review section only): `git add claude/tasks/mcp-spec-2026-07-28-migration/plan.md && git commit -m "docs(ai,mcp): record phase-1 live verification results"`

---

## Review — phase-1 execution results (2026-07-29)

Executed via subagent-driven development, commits `b6a7bb59..1fbd2da3` on `feat/http-mcp`.

- **Task 1** — `mcp>=2.0.0,<3` pinned (stable 2.0.0 on PyPI); `sdk-api-notes.md` recon verified against installed source (1 fix round: handler-signature table corrected).
- **Task 2** — transport claim CONFIRMED: `StreamableHTTPSessionManager` unchanged in v2; `__init__.py` needed zero changes. No commit (verification-only).
- **Task 3** — handlers ported to constructor kwargs; `Tool.input_schema` rename; HardError→`MCPError` mapping (the plan's literal code had a bug — nested except needed because `normalize_error` re-raises inside `except Exception`; reviewer-verified).
- **Task 4** — `cache_policy.py` + explicit result-field hints + 23-tool order pin (1 fix round: three-way TTL branch restored with an honest else-branch test).
- **Task 5** — harness on the v2 in-memory `Client`; shutdown test rebuilt on lifespan hooks; broader-suite sweep: zero migration-caused failures outside the module (68 pre-existing env walls classified in the workspace report).
- **Task 6** — dual-revision pinned with the FULL legacy sequence (initialize → initialized → tools/list on 2025-11-25); doc.md + client-mcp SSE deprecation notes. `builder docs:build` blocked by a pre-existing unrelated gather failure (`tool_google_workspace` README has no docs mount) — prettier fallback used.
- **Task 7 (live, real engine, WS/DAP)** — 7/7: list_tools (23, ordered, hints on the wire), run_dropper_pipe + flow_subscribed, file POST to upload_url, monitor (non-terminal snapshot at timeout — correct for a long-lived webhook source), get_pipeline_trace drained 10 real FLOW events, terminate, store_get_url expected `RR_SIGNING_KEY` gap surfaced in-band. mcp 2.0.0 verified on both sides of the seam.

**Deployment gotcha (action item):** `dist/server/cache/constraints.txt` — a gitignored build-cache artifact — pins `mcp==1.28.1` and blocks engine boot (and every task subprocess) under the new pin until regenerated/edited. Anyone provisioning from cache after this branch lands will hit it; the build/cache regeneration step must pick up the new requirements.

**Local test recipe** (venv lacks the compiled engine env): `PYTHONPATH=.superpowers/sdd/plan/stub python -m pytest packages/ai/tests/ai/modules/mcp/` — 211 passed / 1 env-walled (`test_module_registration`, needs compiled rocketlib/engLib; passes only under the builder env).

## Explicitly out of scope (phase 1)

Auth broker / CIMD (deferred — audit §3) · tasks-extension adoption · `subscriptions/listen` feeds · OTel traceparent propagation (all phase 2, audit "Phase 2" section) · flipping any deployed environment to the new SDK (branch-local until Dylan's compat-window call, audit §4 / open-questions #5).
