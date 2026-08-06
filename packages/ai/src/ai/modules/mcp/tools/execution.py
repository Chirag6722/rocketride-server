# Copyright 2026 Aparavi Software AG. MIT License.
"""Token-based execution tools: `run_pipeline`, `run_dropper_pipe`, `send_data`,
`terminate`, `send_files`.

Token-based execution, no sessions: `use()` returns a task token -- the
single run identity -- and the server-owned `TaskRegistry` (`registry.py`)
tracks `{token -> metadata}` since the SDK keeps none of its own. One-shot
runs let the token expire via `ttl`; long-lived runs keep the token and call
`send_data` repeatedly (`use_existing`). `terminate` tears the task down and
is also the stop-runaway-task path.

Server-imposed timeout: neither the SDK's `use()` nor `send()` has a
wall-clock timeout unless the client itself is built with a
`request_timeout` -- a slow/wedged engine connection would otherwise hang a
tool call indefinitely. Every blocking seam call in this module is wrapped
in `asyncio.wait_for(..., timeout=DEFAULT_TIMEOUT_SECONDS)`. A bare
`asyncio.TimeoutError` is caught locally rather than left to propagate to
`errors.normalize_error`: that normalizer treats the `TimeoutError` type name
as a hard, non-self-correctable failure (`HARD_EXC_NAMES`) and raises
`HardError`, which surfaces as an MCP tool error -- appropriate for a lost
connection, but not for "this one call happened to run long." Timing out a
single `send`/`use` doesn't mean the task itself is dead (it may still be
running engine-side), so we report it as a structured, self-correctable
`{ok: False, error_type: 'Timeout', ...}` result instead, using a distinct
error_type ('Timeout', not 'TimeoutError') so it never collides with the
normalizer's hard-failure set.
"""

import asyncio
from typing import Any, Dict

from ..apps import DROPPER_URI
from ..errors import _bad
from ..tooling import ToolRegistry

# Default wall-clock budget for a single blocking `use`/`send`/`terminate`/
# `send_files` seam call. Chosen as a generous-but-bounded default for
# document-processing pipelines; not user-configurable in v1.
DEFAULT_TIMEOUT_SECONDS = 120

_OPTIONAL_USE_KWARGS = ('ttl', 'use_existing', 'source', 'threads', 'pipelineTraceLevel')

_RUN_PIPELINE_SCHEMA = {
    'type': 'object',
    'properties': {
        'pipeline': {'type': 'object', 'description': 'Inline pipeline definition'},
        'filepath': {'type': 'string', 'description': 'Path to a pipeline file (JSON, JSON5, or .pipe)'},
        'inputs': {'type': 'string', 'description': 'Data to send to the pipeline immediately after starting it'},
        'ttl': {'type': 'integer', 'description': 'Task time-to-live in seconds; 0 = no timeout'},
        'use_existing': {'type': 'boolean', 'description': 'Reuse an existing task instead of starting a new one'},
        'source': {'type': 'string', 'description': 'Optional source label forwarded to use()'},
        'threads': {'type': 'integer', 'description': 'Optional thread count forwarded to use()'},
        'pipelineTraceLevel': {
            'type': 'string',
            'enum': ['none', 'metadata', 'summary', 'full'],
            'description': (
                'Capture the per-node trace stream at this detail level; drain it with get_pipeline_trace (use "full")'
            ),
        },
    },
    'anyOf': [{'required': ['pipeline']}, {'required': ['filepath']}],
}

_RUN_DROPPER_PIPE_SCHEMA = {
    'type': 'object',
    'properties': {
        'pipeline': {'type': 'object', 'description': 'Inline pipeline definition'},
        'filepath': {'type': 'string', 'description': 'Path to a pipeline file (JSON, JSON5, or .pipe)'},
        'ttl': {'type': 'integer', 'description': 'Task time-to-live in seconds; 0 = no timeout'},
        'use_existing': {'type': 'boolean', 'description': 'Reuse an existing task instead of starting a new one'},
        'source': {'type': 'string', 'description': 'Optional source label forwarded to use()'},
        'threads': {'type': 'integer', 'description': 'Optional thread count forwarded to use()'},
        'pipelineTraceLevel': {
            'type': 'string',
            'enum': ['none', 'metadata', 'summary', 'full'],
            'description': (
                'Capture the per-node trace stream at this detail level; drain it with get_pipeline_trace (use "full")'
            ),
        },
    },
    'anyOf': [{'required': ['pipeline']}, {'required': ['filepath']}],
}

_SEND_DATA_SCHEMA = {
    'type': 'object',
    'properties': {
        'task_token': {'type': 'string', 'description': 'Task token returned by run_pipeline'},
        'input': {'type': 'string', 'description': 'Data to send to the running task'},
    },
    'required': ['task_token', 'input'],
}

_TERMINATE_SCHEMA = {
    'type': 'object',
    'properties': {
        'task_token': {'type': 'string', 'description': 'Task token returned by run_pipeline'},
    },
    'required': ['task_token'],
}

_SEND_FILES_SCHEMA = {
    'type': 'object',
    'properties': {
        'task_token': {'type': 'string', 'description': 'Task token returned by run_pipeline'},
        'files': {
            'type': 'array',
            'items': {'type': 'string'},
            'minItems': 1,
            'description': 'Store-relative file paths to upload to the running task',
        },
    },
    'required': ['task_token', 'files'],
}


def _timeout(message: str, hint: str) -> dict:
    """Build a structured, self-correctable timeout result.

    Deliberately uses `error_type: 'Timeout'` (not `'TimeoutError'`) so it
    never collides with `errors.HARD_EXC_NAMES` -- see the module docstring.
    """
    return {'ok': False, 'error_type': 'Timeout', 'message': message, 'hint': hint}


async def _run_pipeline(client, tasks, args: Dict[str, Any]) -> dict:
    pipeline = args.get('pipeline')
    filepath = args.get('filepath')
    if not pipeline and not filepath:
        return _bad('pipeline or filepath is required', 'pass an inline pipeline object or a filepath')

    kwargs: Dict[str, Any] = {'filepath': filepath} if filepath else {'pipeline': pipeline}
    for key in _OPTIONAL_USE_KWARGS:
        if args.get(key) is not None:
            kwargs[key] = args[key]

    try:
        started = await asyncio.wait_for(client.use(**kwargs), timeout=DEFAULT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return _timeout(
            'run_pipeline timed out waiting for the engine to start the task',
            'the task may still be starting; call monitor once a task_token is known, or retry',
        )

    token = (started or {}).get('token')
    if not token:
        return _bad(
            'engine did not return a task token',
            'the pipeline may have failed to start, or the engine response was malformed',
        )
    tasks.add(token, pipeline_ref=filepath or '<inline>')

    result_payload: Dict[str, Any] = {'ok': True, 'task_token': token}

    if args.get('pipelineTraceLevel') not in (None, 'none'):
        tasks.set_flow_id(token, (started or {}).get('id'))
        try:
            await asyncio.wait_for(
                client.add_monitor({'token': token}, ['flow']),
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            tasks.mark_flow_subscribed(token)
            result_payload['flow_subscribed'] = True
        except Exception as exc:  # noqa: BLE001 - pipeline started; subscription is best-effort
            result_payload['flow_subscribed'] = False
            result_payload['flow_warning'] = str(exc)

    inputs = args.get('inputs')
    if inputs is not None:
        try:
            result = await asyncio.wait_for(
                client.send(token, inputs),
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return _timeout(
                'run_pipeline started the task but timed out waiting for the initial send() result',
                f'the task_token is {token}; call monitor or send_data to check on it',
            )
        result_payload['result'] = result
        # One-shot run: inputs were sent and a result came back synchronously,
        # so this token is done -- drop it rather than leak it in the
        # registry for the life of the process (see registry.py).
        tasks.remove(token)

    return result_payload


async def _run_dropper_pipe(client, tasks, args: Dict[str, Any]) -> dict:
    """Start a pipeline and return a self-contained upload URL.

    Bytes cannot ride the MCP tool call (transport payload limits), so this
    tool returns an HTTP endpoint an out-of-band uploader POSTs files to. The
    URL embeds both the task token (routing) and the task's public auth key
    (``pk_``, credential) so it needs no ``Authorization`` header. Unlike
    ``run_pipeline`` there is no inline-send path.
    """
    pipeline = args.get('pipeline')
    filepath = args.get('filepath')
    if not pipeline and not filepath:
        return _bad('pipeline or filepath is required', 'pass an inline pipeline object or a filepath')

    kwargs: Dict[str, Any] = {'filepath': filepath} if filepath else {'pipeline': pipeline}
    for key in _OPTIONAL_USE_KWARGS:
        if args.get(key) is not None:
            kwargs[key] = args[key]

    try:
        started = await asyncio.wait_for(client.use(**kwargs), timeout=DEFAULT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return _timeout(
            'run_dropper_pipe timed out waiting for the engine to start the task',
            'the task may still be starting; call monitor once a task_token is known, or retry',
        )

    started = started or {}
    token = started.get('token')
    public_token = started.get('publicToken')
    if not token or not public_token:
        return _bad(
            'engine did not return a task token and public auth for the dropper URL',
            'the pipeline may lack a data-ingress source, or the engine response was malformed',
        )
    tasks.add(token, pipeline_ref=filepath or '<inline>')

    result_payload: Dict[str, Any] = {
        'ok': True,
        'task_token': token,
        'upload_url': f'{client.base_url}/task/data?token={token}&auth={public_token}',
        'dropper_url': f'{client.base_url}/dropper?auth={public_token}',
    }

    if args.get('pipelineTraceLevel') not in (None, 'none'):
        tasks.set_flow_id(token, started.get('id'))
        try:
            await asyncio.wait_for(
                client.add_monitor({'token': token}, ['flow']),
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            tasks.mark_flow_subscribed(token)
            result_payload['flow_subscribed'] = True
        except Exception as exc:  # noqa: BLE001 - pipeline started; subscription is best-effort
            result_payload['flow_subscribed'] = False
            result_payload['flow_warning'] = str(exc)

    return result_payload


async def _send_data(client, tasks, args: Dict[str, Any]) -> dict:
    token = args.get('task_token') or args.get('token')
    data = args.get('input')
    if data is None:
        data = args.get('data')

    if not token:
        return _bad('task_token is required', 'call run_pipeline first to obtain a task_token')
    if data is None:
        return _bad('input is required', 'pass the data to send to the running task')

    try:
        result = await asyncio.wait_for(
            client.send(token, data),
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return _timeout(
            'send_data timed out waiting for the pipeline result',
            'the task may still be processing; retry send_data or call monitor',
        )

    return {'ok': True, 'result': result}


async def _terminate(client, tasks, args: Dict[str, Any]) -> dict:
    token = args.get('task_token')
    if not token:
        return _bad('task_token is required', 'pass the token returned by run_pipeline')

    try:
        await asyncio.wait_for(client.terminate(token), timeout=DEFAULT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return _timeout(
            'terminate timed out waiting for the engine to tear down the task',
            'the task may still be shutting down; retry terminate',
        )

    tasks.remove(token)
    return {'ok': True, 'terminated': token}


async def _send_files(client, tasks, args: Dict[str, Any]) -> dict:
    token = args.get('task_token')
    files = args.get('files')
    if not token:
        return _bad('task_token is required', 'pass the token returned by run_pipeline')
    if not files:
        return _bad('files is required and must be a non-empty array', 'pass one or more file paths to upload')

    try:
        # SDK arg order is (files, token) -- token second, not first.
        result = await asyncio.wait_for(client.send_files(files, token), timeout=DEFAULT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return _timeout(
            'send_files timed out waiting for the upload result',
            'the upload may still be in progress; retry send_files or call monitor',
        )

    return {'ok': True, 'result': result}


def register(registry: ToolRegistry) -> None:
    """Register the 5 token-based execution tools against ``registry``."""
    registry.register(
        'run_pipeline',
        'Start a RocketRide pipeline from an inline definition or filepath, returning a task_token. '
        'Pass inputs to also send data immediately and get a result back in the same call.',
        _RUN_PIPELINE_SCHEMA,
    )(_run_pipeline)

    registry.register(
        'run_dropper_pipe',
        'Start a RocketRide pipeline and return two self-contained URLs for getting files in over a '
        'separate HTTP data channel (file bytes cannot ride the MCP tool call): upload_url for a '
        'programmatic multipart POST, and dropper_url for a human to drag-drop files in a browser. '
        'Same inputs as run_pipeline, minus the inline-send path.',
        _RUN_DROPPER_PIPE_SCHEMA,
        ui_resource_uri=DROPPER_URI,
    )(_run_dropper_pipe)

    registry.register(
        'send_data',
        'Send data to a running pipeline task (by task_token) and return its result.',
        _SEND_DATA_SCHEMA,
    )(_send_data)

    registry.register(
        'terminate',
        'Terminate a running pipeline task by task_token -- also the stop-runaway-task path.',
        _TERMINATE_SCHEMA,
    )(_terminate)

    registry.register(
        'send_files',
        'Upload one or more store-relative file paths to a running pipeline task by task_token.',
        _SEND_FILES_SCHEMA,
    )(_send_files)
