# Copyright 2026 Aparavi Software AG. MIT License.
"""MCP resources: deployments and server status.

`rocketride://nodes` is removed -- superseded by the `list_components` tool
and the static Skills map (knowledge lives in Skills, not a resource).
"""

import json
from typing import List

import mcp.types as types

from .engine import EngineClient

_PIPELINES = 'rocketride://pipelines'
_STATUS = 'rocketride://status'


def list_resources() -> List[types.Resource]:
    return [
        types.Resource(
            uri=_PIPELINES,
            name='Deployments',
            description='Deployments registered on the connected RocketRide server',
            mime_type='application/json',
        ),
        types.Resource(
            uri=_STATUS,
            name='Server Status',
            description='Current RocketRide server status and running tasks',
            mime_type='application/json',
        ),
    ]


async def read_resource(engine: EngineClient, uri: str) -> str:
    uri = str(uri)
    if uri == _PIPELINES:
        return json.dumps(await engine.deploy_list())
    if uri == _STATUS:
        tasks = await engine.list_tasks()
        names = [t.get('name') for t in tasks if t.get('name')]
        return json.dumps({'connected': True, 'pipeline_count': len(names), 'pipelines': names})
    raise ValueError(f'Unknown resource: {uri}')
