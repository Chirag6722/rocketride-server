# Copyright 2026 Aparavi Software AG. MIT License.
"""Shared helpers for MCP tool handlers."""

import asyncio
import json
from typing import Any, Dict

try:
    import json5
except ImportError:  # pragma: no cover - json5 is a declared dependency
    json5 = None


def load_pipeline(args: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a pipeline object from tool arguments.

    Reads ``filepath`` (JSON, JSON5, or ``.pipe``) or an inline ``pipeline``
    dict, unwrapping a ``{'pipeline': {...}}`` wrapper either way — matching
    the SDK's own ``use()``/``validate()`` auto-unwrap behavior.

    Args:
        args: Tool arguments dict; expected to carry either ``filepath`` or
            ``pipeline``.

    Returns:
        The resolved pipeline object (a JSON object / dict).

    Raises:
        ValueError: if neither ``filepath`` nor ``pipeline`` is supplied, or
            the resolved value is not a JSON object.
    """
    filepath = args.get('filepath')
    pipeline = args.get('pipeline')

    if filepath:
        with open(filepath, 'r', encoding='utf-8') as fh:
            parsed = json5.load(fh) if json5 else json.load(fh)
    elif pipeline is not None:
        parsed = pipeline
    else:
        raise ValueError('load_pipeline requires either "filepath" or "pipeline"')

    resolved = parsed.get('pipeline', parsed) if isinstance(parsed, dict) else parsed

    if not isinstance(resolved, dict):
        raise ValueError('load_pipeline resolved a non-object pipeline value')

    return resolved


async def load_pipeline_async(args: Dict[str, Any]) -> Dict[str, Any]:
    """`load_pipeline` off the event loop: the ``filepath`` branch does a
    blocking ``open()``/parse, which would stall every in-flight request on
    the in-process ASGI server for the duration of the read.
    """
    return await asyncio.to_thread(load_pipeline, args)
