# Copyright 2026 Aparavi Software AG. MIT License.
"""Authoring / introspection tools: `list_components`, `describe_component`,
`validate_pipeline`, `describe_pipeline`.

These are read-only / static-analysis tools -- no task tokens, no execution.
`validate_pipeline` is engine-authoritative (zero client-side validation rules,
so zero drift from the engine's own rules). `describe_pipeline` has no backing
SDK method; it is synthesized client-side via a static parse of the pipeline
plus a best-effort `get_service` lookup per component provider.
"""

from typing import Any, Dict

from ..errors import _bad
from ..tooling import ToolRegistry
from ._common import load_pipeline_async

_PIPELINE_OR_FILEPATH_SCHEMA = {
    'type': 'object',
    'properties': {
        'pipeline': {'type': 'object', 'description': 'Inline pipeline definition'},
        'filepath': {'type': 'string', 'description': 'Path to a pipeline file (JSON, JSON5, or .pipe)'},
    },
    'anyOf': [{'required': ['pipeline']}, {'required': ['filepath']}],
}


async def _list_components(client, tasks, args: Dict[str, Any]) -> dict:
    services = await client.get_services()
    definitions = (services or {}).get('services') or {}
    components = [
        {
            'name': name,
            'category': definition.get('classType'),
            'summary': definition.get('description'),
        }
        for name, definition in definitions.items()
        # One malformed (non-mapping) definition must not block discovery
        # for the whole catalog.
        if isinstance(definition, dict)
    ]
    return {'ok': True, 'components': components}


async def _describe_component(client, tasks, args: Dict[str, Any]) -> dict:
    name = args.get('name')
    if not name:
        return _bad('name is required', 'pick a name from list_components')

    service = await client.get_service(name)
    if service is None:
        return _bad(f'Unknown component: {name}', 'call list_components for valid names')

    return {**service, 'ok': True}


async def _validate_pipeline(client, tasks, args: Dict[str, Any]) -> dict:
    pipeline = await load_pipeline_async(args)  # raises ValueError -> normalized by the dispatch layer
    result = (await client.validate(pipeline)) or {}
    errors = result.get('errors') or []
    warnings = result.get('warnings') or []
    return {'ok': not errors, 'errors': errors, 'warnings': warnings}


async def _describe_pipeline(client, tasks, args: Dict[str, Any]) -> dict:
    pipeline = await load_pipeline_async(args)  # raises ValueError -> normalized by the dispatch layer

    service_cache: Dict[str, Any] = {}
    components = []
    for comp in pipeline.get('components', []) or []:
        provider = comp.get('provider')
        service = None
        if provider is not None:
            if provider not in service_cache:
                try:
                    service_cache[provider] = await client.get_service(provider)
                except Exception:  # noqa: BLE001 - unknown/broken provider must not abort the parse
                    service_cache[provider] = None
            service = service_cache[provider]

        components.append(
            {
                'id': comp.get('id'),
                'provider': provider,
                'title': (service or {}).get('title', comp.get('title')),
                'classType': (service or {}).get('classType', comp.get('classType')),
                'inputs': comp.get('input', []),
            }
        )

    return {
        'ok': True,
        'source': pipeline.get('source'),
        'components': components,
    }


def register(registry: ToolRegistry) -> None:
    """Register the 4 authoring/introspection tools against ``registry``."""
    registry.register(
        'list_components',
        'List available RocketRide pipeline components (name, category, summary). '
        'Call describe_component for a component config schema.',
        {'type': 'object', 'properties': {}},
    )(_list_components)

    registry.register(
        'describe_component',
        'Describe a single RocketRide component: full metadata, lanes, and config schema.',
        {
            'type': 'object',
            'properties': {
                'name': {'type': 'string', 'description': 'Component name from list_components'},
            },
            'required': ['name'],
        },
    )(_describe_component)

    registry.register(
        'validate_pipeline',
        "Validate a pipeline against the engine's own rules (zero client-side rules -- zero drift).",
        _PIPELINE_OR_FILEPATH_SCHEMA,
    )(_validate_pipeline)

    registry.register(
        'describe_pipeline',
        'Statically describe a pipeline source and components (id, provider, title, classType, inputs).',
        _PIPELINE_OR_FILEPATH_SCHEMA,
    )(_describe_pipeline)
