# MCP Python SDK v2 API Recon (Task 1)

_As of 2026-07-29. Installed and interrogated in `.venv` (Python 3.12) on branch `feat/http-mcp`._

## Pin decision

**Stable `mcp==2.0.0` exists on PyPI** (not a pre-release) — the STOP condition in the brief
(Step 1: "if only `2.0.0bN` pre-releases exist") does **not** apply. `pip index versions mcp`
showed `2.0.0` as the newest version, immediately followed by the `1.29.0` … `0.9.1` line.

`packages/ai/src/ai/requirements.txt` changed:

```diff
-mcp>=1.9.0
+mcp>=2.0.0,<3
```

Installed with `pip install 'mcp==2.0.0'` into the project venv (clean install, `pip check`
reports no broken requirements — see "Dependency notes" below for the non-blocking items worth
knowing about).

## Executive summary — corrections to `plan.md` / `open-questions.md`

Read these before touching Tasks 2–6 (Step 5 reconciliation is intentionally skipped by this
task per the controller's instructions — these bullets are the input to that reconciliation):

1. **Transport layer (Task 2) is NOT structurally broken.** `open-questions.md` #2 and
   `plan.md`'s Task 2 sketch both assume `StreamableHTTPSessionManager` might be renamed or
   replaced by a `build_streamable_http_asgi_app()`-style function. **That function does not
   exist.** `mcp.server.streamable_http_manager.StreamableHTTPSessionManager` still exists, at
   the same import path, with the same constructor keywords (`app`, `event_store`,
   `json_response`, `stateless`, `security_settings`, `retry_interval`, plus two new optional
   ones: `session_idle_timeout`, `max_request_body_size`), the same `.handle_request(scope,
   receive, send)` method, and the same `.run()` async-context-manager lifespan hook. The
   current code in `packages/ai/src/ai/modules/mcp/__init__.py` (`session_manager =
   StreamableHTTPSessionManager(app=mcp_server, event_store=None, json_response=False,
   stateless=True)`, a local `handle_mcp` ASGI shim calling `session_manager.handle_request`,
   mounted via `Mount(_MOUNT_PATH, app=handle_mcp)`, and `_stack.enter_async_context
   (session_manager.run())` in the lifespan) should keep working **verbatim** against v2 modulo
   whatever else in the file imports `mcp.types`/handler shapes. Re-scope Task 2 down to "run the
   tests, fix whatever v2 actually breaks" rather than a rewrite — see probe 2 below for the full
   evidence and an alternative (`Server.streamable_http_app()`) that Task 2 does NOT need but
   should know exists.
2. **The real breaking change is the decorator API (Task 3's territory), not the transport.**
   `mcp.server.lowlevel.Server` no longer has `.list_tools()` / `.call_tool()` /
   `.list_resources()` / `.read_resource()` decorator methods at all — confirmed by both reading
   the source and `hasattr()` at runtime. `packages/ai/src/ai/modules/mcp/handlers.py` uses
   exactly this now-removed pattern (`@server.list_tools()`, `@server.call_tool()`,
   `@server.list_resources()`, `@server.read_resource()` at lines 87/91/111/115) — this file is
   the actual migration surface `open-questions.md` was probing for. See probe 1 for the v2
   replacement shape (constructor kwargs or `add_request_handler`), including the *return-type*
   change (handlers now return the full typed `Result` object, not a bare list/string).
3. **Tool ordering is handler-order, confirmed at runtime** (probe 4) — resolves
   `open-questions.md` #3. No pinning test needed to prove insertion order beyond what's below,
   though a regression test is still worth adding in Task 3.
4. **`ttlMs`/`cacheScope` wire names confirmed**; Python-side fields are `ttl_ms` / `cache_scope`
   (snake_case, pydantic-aliased) — resolves the mechanical part of `open-questions.md` #4. The
   product question (what value to set for `tools/list`) is still Dylan+Charlie's call.
5. **Types package split**: the wire types moved to a standalone `mcp-types` distribution
   (top-level `import mcp_types`), but `mcp.types` is kept as an exact mirror (`from mcp_types
   import *`, same objects) specifically so `import mcp.types as types` code needs **no
   changes**. Don't rename `mcp.types` call sites unless you want the leaner dependency surface
   `mcp_types` offers (pydantic + typing-extensions only, no transport stack).
6. **`HardError` is gone, no direct replacement class** — confirmed via `grep -rl HardError` over
   the entire venv (zero hits). The v2 pattern is: raise `mcp.shared.exceptions.MCPError(code,
   message, data=None)` (or one of its subclasses) from a handler for a specific JSON-RPC error;
   any other raised exception is logged server-side and surfaces to the client as
   `MCPError(code=-32603, message="Internal server error")` (confirmed by smoke test below). See
   probe 8.

## Environment notes (not blocking, but worth recording)

- **`pip` was not present in `.venv`** (`ModuleNotFoundError: No module named pip`) despite the
  venv's `bin/` being first on `PATH`. Bootstrapped via `python -m ensurepip --upgrade` (installed
  pip 25.0.1) before any of the brief's steps would run. If CI/other machines hit the same thing,
  this is the fix.
- **This venv is not fully synced with `requirements.txt`**: `requests`, `psutil`, `tenacity`,
  `nvidia-ml-py`, `croniter`, and `filetype` are all *not installed* here (`pip show` reports "not
  found" for each) — this predates this task and isn't something the `mcp` install touched. The
  repo root also carries a `uv.lock` (this project appears to use `uv` for real dependency
  locking elsewhere); this task only touched `requirements.txt` per the brief and did not attempt
  to regenerate any lockfile.
- **`pydantic` installed at 2.13.4**, which is newer than the `requirements.txt` pin
  (`pydantic==2.12.5`) — pre-existing drift in this venv, not introduced by installing `mcp
  2.0.0` (mcp only requires `pydantic>=2.12.0`, which the file's `==2.12.5` pin already
  satisfies; a clean install from `requirements.txt` would land on 2.12.5, not 2.13.4). Flagging
  in case a later task's `pip check` behaves differently on a properly-synced venv.

## Dependency notes

- `mcp==2.0.0` requires: `anyio`, `httpx2` (**not** `httpx`+`httpx-sse` — see below),
  `jsonschema>=4.20.0`, `mcp-types` (new, see probe 3/"types package split" above),
  `opentelemetry-api`, `pydantic>=2.12.0`, `pyjwt[crypto]>=2.10.1` (pulls in `cryptography`),
  `python-multipart`, `sse-starlette`, `starlette>=0.27` (resolved to `1.3.1`), `typing-extensions`,
  `typing-inspection`, `uvicorn>=0.31.1`.
- **`httpx` → `httpx2` is a distribution rename, not a version bump.** `httpx2` installs as its
  own top-level module (`import httpx2`, confirmed via `python -c "import httpx2; print(httpx2.
  __file__)"`), separate from the classic `httpx` package. `requirements.txt` still lists plain
  `httpx` on its own line for `packages/ai`'s non-MCP code — that's a **different package now**
  from what `mcp` uses internally, so the two coexist with **zero namespace conflict**. Nothing to
  reconcile here; do not delete the `httpx` line.
- `pip check` after installing `mcp==2.0.0` into this (partially-populated) venv reports "No
  broken requirements found."
- `uvicorn`, `pyjwt`/`cryptography`, `jsonschema`, `opentelemetry-api`, `python-multipart`,
  `sse-starlette` are all new transitive dependencies vs. the v1 `mcp` line's dependency tree
  (v1's `Requires` was much smaller — no uvicorn/jsonschema/pyjwt/otel). Worth a mention to
  whoever owns the container image / dependency-audit process; none of it looked optional via
  extras (no `mcp[...]` markers used here), it's all base install.

## Probe answers

### 1. Low-level `Server` — decorators removed, constructor/`add_request_handler` instead

`mcp.server.lowlevel.Server` still exists at the same import path
(`from mcp.server.lowlevel import Server`, also re-exported as `mcp.server.Server`). **The
`@server.list_tools()` / `@server.call_tool()` / `@server.list_resources()` /
`@server.read_resource()` decorator methods do not exist in v2** — confirmed both by reading
`mcp/server/lowlevel/server.py` (833 lines, no such methods defined) and at runtime:

```python
>>> from mcp.server.lowlevel import Server
>>> s = Server('test')
>>> hasattr(s, 'list_tools'), hasattr(s, 'call_tool')
(False, False)
```

Two replacements, both registering by MCP method name against a typed params model:

**(a) Constructor kwargs** (`on_list_tools`, `on_call_tool`, `on_list_resources`,
`on_list_resource_templates`, `on_read_resource`, `on_subscribe_resource`,
`on_unsubscribe_resource`, `on_subscriptions_listen`, `on_list_prompts`, `on_get_prompt`,
`on_completion`, `on_ping`):

```python
from mcp.server.lowlevel import Server
import mcp.types as types  # or `import mcp_types as types`

async def on_list_tools(ctx, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[...])

async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult | types.InputRequiredResult:
    return types.CallToolResult(content=[...])

server = Server('rocketride-mcp', on_list_tools=on_list_tools, on_call_tool=on_call_tool, ...)
```

**(b) `add_request_handler(method: str, params_type: type[BaseModel], handler)`** — same effect,
usable after construction:

```python
server.add_request_handler('tools/list', types.PaginatedRequestParams, on_list_tools)
```
(`server.add_notification_handler(method, params_type, handler)` is the notification-side
equivalent; `initialize` is reserved and raises `ValueError` if you try to register it — use
`Server.middleware` to observe/wrap the handshake instead.)

**Signature diffs that matter for the port of `handlers.py`:**

Every handler is now `(ctx: ServerRequestContext[LifespanResultT], params: <ParamsModel-or-ParamsModel|None>) -> Awaitable[<Result>]` — always 2 args, always returning the full typed `Result` object (never a bare list/string) — but **the params type is NOT uniformly `| None`**. Per the (non-deprecated) constructor overload at `mcp/server/lowlevel/server.py:146-204`, the `| None` arm on the *params* type appears only on the four paginated-listing handlers plus `on_ping`; the single-item handlers take a required (non-`Optional`) params model:

| Handler | Params type (exact, from `server.py:146-204`) | `\| None` on params? |
| --- | --- | --- |
| `on_list_tools` | `types.PaginatedRequestParams \| None` (line 147) | yes |
| `on_list_resources` | `types.PaginatedRequestParams \| None` (line 157) | yes |
| `on_list_resource_templates` | `types.PaginatedRequestParams \| None` (line 162) | yes |
| `on_list_prompts` | `types.PaginatedRequestParams \| None` (line 187) | yes |
| `on_ping` | `types.RequestParams \| None` (line 202) | yes |
| `on_call_tool` | `types.CallToolRequestParams` (line 152) | **no** |
| `on_read_resource` | `types.ReadResourceRequestParams` (line 167) | **no** |
| `on_subscribe_resource` | `types.SubscribeRequestParams` (line 172) | no |
| `on_unsubscribe_resource` | `types.UnsubscribeRequestParams` (line 177) | no |
| `on_subscriptions_listen` | `types.SubscriptionsListenRequestParams` (line 182) | no |
| `on_get_prompt` | `types.GetPromptRequestParams` (line 192) | no |
| `on_completion` | `types.CompleteRequestParams` (line 197) | no |

Concretely, `on_call_tool` and `on_read_resource` — the two handlers Task 3 actually ports from
`handlers.py` — are typed as:

```python
on_call_tool: Callable[
    [ServerRequestContext[LifespanResultT], types.CallToolRequestParams],
    Awaitable[types.CallToolResult | types.InputRequiredResult],
] | None = None,

on_read_resource: Callable[
    [ServerRequestContext[LifespanResultT], types.ReadResourceRequestParams],
    Awaitable[types.ReadResourceResult | types.InputRequiredResult],
] | None = None,
```
(the outer `| None = None` is only "this kwarg may be omitted, defaulting to no handler
registered" — it is not part of the params type the handler function itself receives.) So a Task
3 port should type these two as `async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult | types.InputRequiredResult` and `async def on_read_resource(ctx, params: types.ReadResourceRequestParams) -> types.ReadResourceResult | types.InputRequiredResult` — **no `if params is None:` guard, and no `| None` in the annotation** for either. `on_list_tools`/`on_list_resources` do keep the `| None` in their declared type.

Separately from the declared type: **at runtime, no registered handler (listing or single-item) is ever invoked with the literal Python value `None`** for `params`, regardless of what its declared type says. Per `Server.add_request_handler`'s docstring and the dispatch code in `mcp/server/runner.py::ServerRunner._on_request` (`typed_params = entry.params_type.model_validate({} if params is None else params, by_name=False)`), a message with no wire `params` member always validates `{}` against the handler's `params_type`: an all-optional model (e.g. `PaginatedRequestParams`, whose `cursor` defaults to `None`) resolves to a real instance with defaults, not `None`; a model with required fields (e.g. `CallToolRequestParams`, which requires `name`) rejects the empty `{}` as `INVALID_PARAMS` before the handler is ever called. So the `| None` on the four listing handlers' declared type is about the *type checker* accepting a handler written defensively for "no params object" — it does not mean the runner ever passes `None` through; it's a static-typing nicety, not a runtime possibility. (This resolves why the type differs per-handler while the "never receives `None`" behavior is uniform across all of them.)

- `on_call_tool` returns `types.CallToolResult | types.InputRequiredResult`, not a bare content list.
- `on_read_resource` returns `types.ReadResourceResult | types.InputRequiredResult`, not `str | bytes` / `Iterable[ReadResourceContents]`.
- `on_set_logging_level` / `on_roots_list_changed` / `on_progress` kwargs still exist but are deprecated (`MCPDeprecationWarning`, `UserWarning` subclass) as of the 2026-07-28 spec (SEP-2577); passing them warns at construction time but still works.

Runtime smoke test (confirms the constructor-kwarg path end-to-end via the in-process `Client`):

```python
import asyncio
from mcp.server.lowlevel import Server
import mcp_types as types
from mcp.client import Client

async def on_list_tools(ctx, params):
    return types.ListToolsResult(tools=[
        types.Tool(name='b_tool', description='b', inputSchema={'type': 'object', 'properties': {}}),
        types.Tool(name='a_tool', description='a', inputSchema={'type': 'object', 'properties': {}}),
    ])

async def on_call_tool(ctx, params):
    return types.CallToolResult(content=[types.TextContent(type='text', text=f'called {params.name}')])

server = Server('smoke-test', on_list_tools=on_list_tools, on_call_tool=on_call_tool)

async def main():
    async with Client(server) as client:
        tools = await client.list_tools()
        print([t.name for t in tools.tools])   # -> ['b_tool', 'a_tool']  (handler order preserved)
        result = await client.call_tool('b_tool', {})
        print(result.content[0].text)           # -> 'called b_tool'

asyncio.run(main())
```
Output observed: `['b_tool', 'a_tool']`, `called b_tool` — both handler-order preservation (probe
4) and the call round-trip confirmed live.

### 2. Transport — `StreamableHTTPSessionManager` unchanged in shape; `Server.streamable_http_app()` is new sugar

`mcp.server.streamable_http_manager.StreamableHTTPSessionManager` (import path unchanged) is
still the class the low-level `Server` itself uses internally. Constructor:

```python
class StreamableHTTPSessionManager:
    def __init__(
        self,
        app: Server[Any],
        event_store: EventStore | None = None,
        json_response: bool = False,
        stateless: bool = False,
        security_settings: TransportSecuritySettings | None = None,
        retry_interval: int | None = None,
        session_idle_timeout: float | None = None,       # NEW in v2
        max_request_body_size: int = DEFAULT_MAX_REQUEST_BODY_SIZE,  # NEW in v2, default 4 MiB
    ): ...
```

All of v1's kwargs (`app`, `event_store`, `json_response`, `stateless`) are unchanged in name and
meaning. The two new kwargs are optional with sane defaults, so existing call sites need no
changes to compile. Async API is unchanged too:
- `await session_manager.handle_request(scope, receive, send)` — still the ASGI-shaped entry
  point per request (`packages/ai`'s current code wraps this in a local `handle_mcp` shim and
  mounts it — that shim still works unmodified).
- `async with session_manager.run(): ...` — still the lifespan context manager to enter once
  (raises `RuntimeError` if entered twice; same as v1).

**New in v2**: the low-level `Server` itself now exposes `.streamable_http_app(...)`
(`mcp/server/lowlevel/server.py:720`), which builds and returns a complete `Starlette` app
(routes + auth + the `StreamableHTTPSessionManager` + its lifespan already wired):

```python
def streamable_http_app(
    self,
    *,
    streamable_http_path: str = "/mcp",
    json_response: bool = False,
    stateless_http: bool = False,
    event_store: EventStore | None = None,
    retry_interval: int | None = None,
    max_request_body_size: int = DEFAULT_MAX_REQUEST_BODY_SIZE,
    transport_security: TransportSecuritySettings | None = None,
    host: str = "127.0.0.1",
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
    auth_server_provider: OAuthAuthorizationServerProvider[Any, Any, Any] | None = None,
    custom_starlette_routes: list[Route] | None = None,
    debug: bool = False,
) -> Starlette: ...
```
Smoke-tested (no network call, just construction): `server.streamable_http_app(stateless_http=True, streamable_http_path='/mcp')` returns a `Starlette` instance with no errors.

**This is NOT what Task 2 needs**: `packages/ai`'s `initModule` mounts onto an *existing*
`server.app.router` via `Mount(_MOUNT_PATH, app=handle_mcp)` (a raw ASGI callable), which is
exactly the `StreamableHTTPSessionManager.handle_request` pattern already in the file — a whole
second `Starlette` app from `.streamable_http_app()` would have to be sub-mounted instead, which
is a strictly worse fit for the existing raw-route-append pattern in `__init__.py:75`. Record it
here so nobody "discovers" it mid-Task-2 and rewrites more than necessary.

### 3. Cache hints — `ttl_ms`/`cache_scope` (wire: `ttlMs`/`cacheScope`)

`mcp.server.caching`:

```python
from mcp.server.caching import CacheHint, CacheableMethod, CACHEABLE_METHODS, validate_cache_hints

@dataclass(frozen=True, slots=True)
class CacheHint:
    ttl_ms: int = 0                                  # >= 0, validated in __post_init__
    scope: Literal["public", "private"] = "private"
```

Result-side fields (on `ListToolsResult`, `ListResourcesResult`, `ListResourceTemplatesResult`,
`ReadResourceResult`, `ListPromptsResult`, `DiscoverResult` — all subclass `mcp_types.
CacheableResult`), from `mcp_types/_v2026_07_28/__init__.py`:

```python
class CacheableResult(WireModel):
    ttl_ms: Annotated[int, Field(alias="ttlMs", ge=0)]
    cache_scope: Annotated[Literal["private", "public"], Field(alias="cacheScope")]
```

**How a low-level handler sets them — both work, and per-field the handler's explicit value
wins:**
1. **Return-type route**: construct the result with `ttl_ms=`/`cache_scope=` explicitly set —
   `types.ListToolsResult(tools=[...], ttl_ms=60_000, cache_scope="public")`.
2. **Server-wide kwarg route**: `Server(..., cache_hints={"tools/list": CacheHint(ttl_ms=60_000, scope="public")})`. Applied post-hoc by the request runner (`mcp/server/caching.py::apply_cache_hint`), and **only fills fields the handler didn't already set** (tracked via pydantic's `model_fields_set`, per-field — not per-result). If a handler used `model_construct(...)` instead of the normal constructor, it's treated as having set nothing.

Defaults when neither route sets anything: `ttl_ms=0` (immediately stale), `cache_scope=
"private"` — confirmed by the probe-1 smoke test (`tools.ttl_ms == 0`, `tools.cache_scope ==
'private'` when the handler didn't set them).

### 4. Ordering — handler order preserved, SDK does not sort

No sorting logic exists anywhere in `mcp/server/` touching tool/resource/prompt lists (grepped
`sort` across the whole `server/` tree — the only hits are unrelated JSON key-sorting and a
`sorted()` call over cache-hint validation error messages). **Confirmed live** by the probe-1
smoke test: a handler returning `[b_tool, a_tool]` round-trips through `Client.list_tools()` as
`['b_tool', 'a_tool']` — exact handler order, no alphabetizing.

### 5. `server/discover` — SDK-provided, auto-derived, replaceable

`Server._handle_discover` is registered as the default `server/discover` handler at
`mcp/server/lowlevel/server.py:660`. It's auto-derived from whatever's currently registered:

```python
async def _handle_discover(self, ctx, params: types.RequestParams | None) -> types.DiscoverResult:
    return types.DiscoverResult(
        supported_versions=list(MODERN_PROTOCOL_VERSIONS),   # ("2026-07-28",)
        capabilities=self.get_capabilities(protocol_version=ctx.protocol_version),
        instructions=self.instructions,
    )
```

`get_capabilities()` populates `ServerCapabilities` by checking which methods have a registered
handler (`"tools/list" in self._request_handlers` etc.) — no separate capabilities-declaration
step needed; registering `on_list_tools` is sufficient for `tools` capability to appear.
Operators can override wholesale via `server.add_request_handler("server/discover", types.
RequestParams, my_handler)`.

### 6. Dual-revision — one endpoint, automatic, no opt-in flag

Confirmed structurally in `mcp/server/streamable_http_manager.py::StreamableHTTPSessionManager.
_handle_request`: every incoming HTTP POST is inspected for the `MCP-Protocol-Version` header;

```python
header = MCP_PROTOCOL_VERSION_HEADER.encode("ascii")
pv = next((v.decode("latin-1") for k, v in scope["headers"] if k == header), None)
if pv is not None and pv not in HANDSHAKE_PROTOCOL_VERSIONS:
    await handle_modern_request(self.app, self.security_settings, self.json_response, self._lifespan_state, scope, receive, send)
    return
# ... else falls through to the legacy stateful/stateless initialize-handshake path
```
`HANDSHAKE_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")`,
`MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)` (`mcp_types/version.py`). There is **no opt-in
flag** — this branch is unconditional inside `_handle_request`, so the same mounted app/session
manager answers both eras automatically, purely by inspecting the header per-request. The 2026-07-28
path (`mcp/server/_streamable_http_modern.py`) is explicitly documented as "a self-contained POST:
no `initialize` handshake, no `Mcp-Session-Id`, one JSON-RPC request in, one JSON-RPC response
out" — i.e. genuinely self-describing per the SDK's own module docstring.

### 7. Test client — `mcp.client.Client`, in-process supported directly

```python
from mcp.client import Client
from mcp.server.lowlevel import Server         # or mcp.server.mcpserver.MCPServer

server = Server(...)  # or MCPServer(...)
async with Client(server) as client:            # in-process, no sockets
    result = await client.call_tool("add", {"a": 1, "b": 2})
```

`Client` (`mcp/client/client.py`) is a `@dataclass` whose single positional field `server` accepts
a `Server[Any] | MCPServer | Transport | str`:
- `Server`/`MCPServer` instance → in-process. `mode="auto"` (the default) does a
  `server/discover` probe and adopts the modern per-request `DirectDispatcher` path with **no
  streams, no JSON-RPC framing, no `initialize` handshake** for a 2026-07-28 server (confirmed:
  `client.protocol_version == '2026-07-28'` in the probe-1 smoke test run against a fresh in-proc
  server); `mode="legacy"` forces the old `InMemoryTransport` + `initialize` handshake path
  byte-identical to v1.
- `str` → URL, wraps `streamable_http_client(url)`.
- `Transport` instance → used directly.

`ClientSession` still exists (`mcp.client.session.ClientSession`), reachable via
`client.session` after entering the context manager — same escape hatch as v1 for advanced use.
`streamable_http_client` still exists at `mcp.client.streamable_http.streamable_http_client`,
unchanged in purpose (build a stream-backed `Transport` for a URL). Both are now wrapped by
`Client` rather than being the primary public surface, but neither was removed.

### 8. Error types — `HardError` gone; `MCPError` + a documented catch-all

No `HardError` (or anything similarly named) exists anywhere in the installed SDK (`grep -rl
HardError` over the entire venv site-packages returns nothing). The mapping function, shared by
every dispatcher, is `mcp.shared.jsonrpc_dispatcher.handler_exception_to_error_data`:

```python
def handler_exception_to_error_data(exc: BaseException) -> ErrorData | None:
    if isinstance(exc, MCPError):
        return exc.error
    if isinstance(exc, ValidationError):          # pydantic
        return ErrorData(code=INVALID_PARAMS, message="Invalid request parameters", data="")
    return None   # caller applies its own catch-all
```
`mcp.shared.exceptions.MCPError(code: int, message: str, data: Any = None)` is the general-purpose
exception a handler raises to control the wire error precisely (it carries `.code`/`.message`/
`.data`, and there's `MCPError.from_error_data(ErrorData)` / `.from_jsonrpc_error(...)`
classmethods). Two ready-made subclasses ship: `NoBackChannelError` (server tries to send a
request over a channel that can't carry one) and `UrlElicitationRequiredError` (SEP-2322 URL
elicitation flow).

**Any other raised exception** (not `MCPError`, not a pydantic `ValidationError`) is logged
server-side and mapped by each dispatcher's own catch-all — legacy `JSONRPCDispatcher` pins
`code=0` for v1 wire compatibility; the modern per-request path (in-process direct-dispatch and
the 2026-07-28 HTTP entry) uses `INTERNAL_ERROR` (`-32603`). **Confirmed live**:

```python
async def bad_call_tool(ctx, params):
    raise ValueError('boom')

server.add_request_handler('tools/call', types.CallToolRequestParams, bad_call_tool)
# ... via Client, in-process, modern mode:
# server logs: "request handler raised" + full traceback (ValueError: boom)
# client sees: MCPError(code=-32603, message='Internal server error')
```
`call_tool`'s per-tool "business" error convention (returning `CallToolResult(isError=True, ...)`
instead of raising) is unchanged — that's a content-shape decision the handler makes, orthogonal
to this exception-to-JSON-RPC mapping.

## Verification evidence log

- `pip index versions mcp` → newest is `2.0.0` (stable), confirming no STOP needed.
- `pip install 'mcp==2.0.0'` → clean install, 22 packages installed/upgraded, no errors.
- `pip check` → "No broken requirements found."
- `python -c "import httpx2; print(httpx2.__file__)"` → resolves to its own top-level package,
  distinct from `httpx`.
- `python -c "import mcp.types as t, mcp_types; print(t is mcp_types, t.ListToolsResult is mcp_types.ListToolsResult)"` → `False True` (different module objects, same classes — confirms the mirror).
- `hasattr(Server('test'), 'list_tools')` etc. → all `False` (decorators gone).
- In-process `Client` smoke test (probe 1) → tool order preserved, call round-trips, default
  `ttl_ms=0`/`cache_scope='private'` confirmed.
- `add_request_handler` + raised `ValueError` smoke test (probe 8) → server logs the traceback,
  client receives `MCPError(code=-32603, message='Internal server error')`.
- `server.streamable_http_app(stateless_http=True, streamable_http_path='/mcp')` → returns a
  `Starlette` instance, no errors (probe 2).
