# Copyright 2026 Aparavi Software AG. MIT License.
"""Tests for the introspection tools (`tools/introspection.py`):
`list_components`, `describe_component`, `validate_pipeline`,
`describe_pipeline`.
"""

import json

import pytest

from ai.modules.mcp.tooling import ToolRegistry
from ai.modules.mcp.tools import introspection
from ai.modules.mcp.tools import register_all


# --- registration -------------------------------------------------------


def test_register_all_registers_all_four_introspection_tools():
    registry = ToolRegistry()

    register_all(registry)

    # register_all also wires other tool groups (execution, ...) as later
    # tasks land; only assert the introspection tools are present here.
    assert {
        'list_components',
        'describe_component',
        'validate_pipeline',
        'describe_pipeline',
    } <= set(registry.names())


def test_introspection_register_binds_handlers_directly():
    registry = ToolRegistry()

    introspection.register(registry)

    for name in ('list_components', 'describe_component', 'validate_pipeline', 'describe_pipeline'):
        assert registry.handler(name) is not None


# --- list_components -----------------------------------------------------


@pytest.mark.asyncio
async def test_list_components_slims_services(fake_engine):
    registry = ToolRegistry()
    introspection.register(registry)

    result = await registry.handler('list_components')(fake_engine, None, {})

    assert result['ok'] is True
    assert fake_engine.get_services_calls == 1
    by_name = {c['name']: c for c in result['components']}
    assert by_name['ocr'] == {
        'name': 'ocr',
        'category': ['source'],
        'summary': 'Optical character recognition component',
    }
    # No config schema leaked into the slim menu.
    for comp in result['components']:
        assert 'schema' not in comp
        assert 'Pipe' not in comp


# --- describe_component ---------------------------------------------------


@pytest.mark.asyncio
async def test_describe_component_requires_name(fake_engine):
    registry = ToolRegistry()
    introspection.register(registry)

    result = await registry.handler('describe_component')(fake_engine, None, {})

    assert result['ok'] is False
    assert result['error_type'] == 'BadRequest'
    assert fake_engine.get_service_calls == []


@pytest.mark.asyncio
async def test_describe_component_returns_full_definition(fake_engine):
    registry = ToolRegistry()
    introspection.register(registry)

    result = await registry.handler('describe_component')(fake_engine, None, {'name': 'ocr'})

    assert result['ok'] is True
    assert result['name'] == 'ocr'
    assert result['title'] == 'OCR'
    assert result['classType'] == ['source']
    assert fake_engine.get_service_calls == ['ocr']


@pytest.mark.asyncio
async def test_describe_component_unknown_name_returns_bad(fake_engine):
    registry = ToolRegistry()
    introspection.register(registry)

    result = await registry.handler('describe_component')(fake_engine, None, {'name': 'nope'})

    assert result['ok'] is False
    assert result['error_type'] == 'BadRequest'
    assert 'nope' in result['message']


# --- validate_pipeline -----------------------------------------------------


@pytest.mark.asyncio
async def test_validate_pipeline_ok_true_when_no_errors(fake_engine):
    registry = ToolRegistry()
    introspection.register(registry)
    pipeline = {'source': 'a', 'components': []}

    result = await registry.handler('validate_pipeline')(fake_engine, None, {'pipeline': pipeline})

    assert result == {'ok': True, 'errors': [], 'warnings': []}
    assert fake_engine.validate_calls == [{'pipeline': pipeline, 'source': None}]


@pytest.mark.asyncio
async def test_validate_pipeline_ok_false_when_errors(fake_engine):
    fake_engine._validate_result = {'errors': [{'type': 'x', 'message': 'bad', 'id': '1'}], 'warnings': []}
    registry = ToolRegistry()
    introspection.register(registry)
    pipeline = {'source': 'a', 'components': []}

    result = await registry.handler('validate_pipeline')(fake_engine, None, {'pipeline': pipeline})

    assert result['ok'] is False
    assert result['errors'] == [{'type': 'x', 'message': 'bad', 'id': '1'}]


@pytest.mark.asyncio
async def test_validate_pipeline_raises_value_error_when_no_input(fake_engine):
    registry = ToolRegistry()
    introspection.register(registry)

    with pytest.raises(ValueError):
        await registry.handler('validate_pipeline')(fake_engine, None, {})


# --- describe_pipeline -----------------------------------------------------


@pytest.mark.asyncio
async def test_describe_pipeline_parses_source_and_components(fake_engine):
    registry = ToolRegistry()
    introspection.register(registry)
    pipeline = {
        'source': 'my-pipe',
        'components': [
            {'id': 'c1', 'provider': 'ocr', 'input': ['c0']},
        ],
    }

    result = await registry.handler('describe_pipeline')(fake_engine, None, {'pipeline': pipeline})

    assert result['ok'] is True
    assert result['source'] == 'my-pipe'
    assert result['components'] == [
        {
            'id': 'c1',
            'provider': 'ocr',
            'title': 'OCR',
            'classType': ['source'],
            'inputs': ['c0'],
        }
    ]
    assert fake_engine.get_service_calls == ['ocr']


@pytest.mark.asyncio
async def test_describe_pipeline_tolerates_unknown_provider(fake_engine):
    registry = ToolRegistry()
    introspection.register(registry)
    pipeline = {
        'source': 'my-pipe',
        'components': [
            {'id': 'c1', 'provider': 'not_a_real_provider', 'input': []},
        ],
    }

    result = await registry.handler('describe_pipeline')(fake_engine, None, {'pipeline': pipeline})

    assert result['ok'] is True
    assert result['components'] == [
        {
            'id': 'c1',
            'provider': 'not_a_real_provider',
            'title': None,
            'classType': None,
            'inputs': [],
        }
    ]


@pytest.mark.asyncio
async def test_describe_pipeline_caches_get_service_per_provider(fake_engine):
    registry = ToolRegistry()
    introspection.register(registry)
    pipeline = {
        'source': 'my-pipe',
        'components': [
            {'id': 'c1', 'provider': 'ocr', 'input': []},
            {'id': 'c2', 'provider': 'ocr', 'input': ['c1']},
        ],
    }

    result = await registry.handler('describe_pipeline')(fake_engine, None, {'pipeline': pipeline})

    assert len(result['components']) == 2
    assert fake_engine.get_service_calls == ['ocr']  # called once, cached for the 2nd component


@pytest.mark.asyncio
async def test_describe_pipeline_raises_value_error_when_no_input(fake_engine):
    registry = ToolRegistry()
    introspection.register(registry)

    with pytest.raises(ValueError):
        await registry.handler('describe_pipeline')(fake_engine, None, {})


# --- real dispatch (build_mcp_server) --------------------------------------


@pytest.mark.asyncio
async def test_list_components_via_real_dispatch(fake_engine):
    import ai.modules.mcp.handlers as handlers_mod
    from mcp.client import Client

    server = handlers_mod.build_mcp_server(lambda: fake_engine)
    async with Client(server) as client:
        result = await client.call_tool('list_components', {})

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload['ok'] is True
    assert {c['name'] for c in payload['components']} == {'ocr', 'anthropic'}
