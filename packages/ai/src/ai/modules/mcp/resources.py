# Copyright 2026 Aparavi Software AG. MIT License.
"""MCP resources: deployments and server status.

`rocketride://nodes` is removed -- superseded by the `list_components` tool
and the static Skills map (knowledge lives in Skills, not a resource).
"""

import json
from typing import List

import mcp.types as types

from .engine import EngineClient

# Public: handlers.py keys its per-URI cache TTLs off these — a renamed
# URI must fail loudly there, not silently fall through to ttl_ms=0.
PIPELINES_URI = 'rocketride://pipelines'
STATUS_URI = 'rocketride://status'


def list_resources() -> List[types.Resource]:
    return [
        types.Resource(
            uri=PIPELINES_URI,
            name='Deployments',
            description='Deployments registered on the connected RocketRide server',
            mime_type='application/json',
        ),
        types.Resource(
            uri=STATUS_URI,
            name='Server Status',
            description='Current RocketRide server status and running tasks',
            mime_type='application/json',
        ),
    ]


async def read_resource(engine: EngineClient, uri: str) -> str:
    uri = str(uri)
    if uri == PIPELINES_URI:
        return json.dumps(await engine.deploy_list())
    if uri == STATUS_URI:
        tasks = await engine.list_tasks()
        names = [t.get('name') for t in tasks if t.get('name')]
        # Count ALL running tasks; `pipelines` lists only the resolvable names.
        return json.dumps({'connected': True, 'pipeline_count': len(tasks), 'pipelines': names})
    raise ValueError(f'Unknown resource: {uri}')
