# Copyright 2026 Aparavi Software AG. MIT License.
"""Assemble the low-level MCP Server: registry-based tool dispatch + resources.

Tools are no longer a dynamic per-pipeline surface. A single `ToolRegistry` is
built once per server and populated by `tools.register_all`; `list_tools`
returns its contents and `call_tool` dispatches to its handlers. There is no
prompt surface -- "knowledge lives in Skills," not MCP prompts.
"""

import json
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError

from .cache_policy import (
    CACHE_SCOPE,
    PIPELINES_READ_TTL_MS,
    RESOURCES_LIST_TTL_MS,
    STATUS_READ_TTL_MS,
    TOOLS_TTL_MS,
)
from .engine import EngineClient
from .errors import HardError, normalize_error
from .registry import TaskRegistry
from .tooling import ToolRegistry
from . import resources as resources_mod
from . import tools as tools_pkg

logger = logging.getLogger(__name__)


def make_flow_dispatcher(tasks: TaskRegistry) -> Callable[[Dict[str, Any]], Awaitable[None]]:
    """Build the client `on_event` callback that buffers per-node trace events.

    The engine pushes ``apaevt_flow`` events for any task subscribed via
    ``add_monitor({'token': ...}, ['flow'])``. Each delivered event carries the
    task's short id at ``body.__id`` (injected by the DAP layer), which maps to
    a registry token. We route the trace payload into that token's ring buffer
    so a pull-based `get_pipeline_trace` tool can drain it later. Non-flow
    events and events with no ``__id`` are ignored.
    """

    async def _on_event(message: Dict[str, Any]) -> None:
        if (message or {}).get('event') != 'apaevt_flow':
            return
        body = message.get('body') or {}
        flow_id = body.get('__id')
        if flow_id is None:
            return
        tasks.record_flow(
            flow_id,
            {
                'pipe': body.get('id'),
                'op': body.get('op'),
                'pipes': body.get('pipes'),
                'trace': body.get('trace'),
                'source': body.get('source'),
            },
        )

    return _on_event


def build_mcp_server(
    engine_factory: Callable[[], EngineClient],
    task_registry: Optional[TaskRegistry] = None,
    *,
    registry: Optional[ToolRegistry] = None,
) -> Server:
    """Build and return a low-level MCP Server wired with tools and resources.

    Args:
        engine_factory: Zero-arg callable returning an EngineClient. In production
            this wraps a lazy SINGLETON (see `__init__.initModule`): the first call
            constructs one long-lived `WsEngineClient` and every later call returns
            the same instance, so all handlers here share one client for the life
            of the process. Concurrent `/mcp` requests therefore multiplex a single
            underlying `RocketRideClient` WS connection — the client's connect lock
            only guards the one-time `connect()` race, it does not serialize or
            correlate concurrent in-flight requests on that connection.
        task_registry: Optional pre-built `TaskRegistry` (e.g. one already wired
            to a flow-event dispatcher via `make_flow_dispatcher`). When omitted,
            a fresh registry is created here (back-compat for callers/tests that
            don't need flow-event buffering).
        registry: Optional pre-built `ToolRegistry`. Keyword-only test seam --
            production callers never pass this; when omitted (the normal case),
            a fresh registry is built here via `tools.register_all`.

    Returns:
        A configured mcp.server.lowlevel.Server ready to run.
    """
    if registry is None:
        registry = ToolRegistry()
        tools_pkg.register_all(registry)
    task_registry = task_registry if task_registry is not None else TaskRegistry()

    async def _on_list_tools(ctx, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=registry.tools(),
            ttl_ms=TOOLS_TTL_MS,
            cache_scope=CACHE_SCOPE,
        )

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
                try:
                    result = normalize_error(exc)
                except HardError as hard_exc:
                    # normalize_error itself reclassifies some exceptions (by type
                    # name) into HardError -- that raise happens from *inside* this
                    # except block, so it is not seen by the `except HardError`
                    # clause above (a new exception raised in an except suite is
                    # never matched against sibling clauses of the same try). Map
                    # it here too, for the same reason: preserve the message
                    # instead of letting v2's catch-all reduce it to "Internal
                    # server error".
                    raise MCPError(types.INTERNAL_ERROR, hard_exc.message) from hard_exc
        return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(result, default=str))])

    async def _on_list_resources(ctx, params: types.PaginatedRequestParams | None) -> types.ListResourcesResult:
        return types.ListResourcesResult(
            resources=resources_mod.list_resources(),
            ttl_ms=RESOURCES_LIST_TTL_MS,
            cache_scope=CACHE_SCOPE,
        )

    async def _on_read_resource(ctx, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        uri_str = str(params.uri)
        text = await resources_mod.read_resource(engine_factory(), uri_str)
        # Per-URI cache TTL: status is live state (no caching), pipelines reflect running
        # tasks (30s), unknown URIs default to uncached (0) for safety.
        if uri_str == 'rocketride://status':
            ttl_ms = STATUS_READ_TTL_MS
        elif uri_str == 'rocketride://pipelines':
            ttl_ms = PIPELINES_READ_TTL_MS
        else:
            ttl_ms = 0
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=params.uri, mimeType='application/json', text=text)],
            ttl_ms=ttl_ms,
            cache_scope=CACHE_SCOPE,
        )

    server = Server(
        'rocketride-mcp',
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
        on_list_resources=_on_list_resources,
        on_read_resource=_on_read_resource,
    )

    return server
