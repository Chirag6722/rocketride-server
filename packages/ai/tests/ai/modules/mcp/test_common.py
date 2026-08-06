# Copyright 2026 Aparavi Software AG. MIT License.
"""Tests for `tools/_common.py` `load_pipeline`."""

import json

import pytest

from ai.modules.mcp.tools._common import load_pipeline


def test_load_pipeline_inline_dict():
    pipeline = {'source': 'a', 'components': []}

    assert load_pipeline({'pipeline': pipeline}) == pipeline


def test_load_pipeline_unwraps_nested_wrapper():
    inner = {'source': 'a', 'components': []}

    assert load_pipeline({'pipeline': {'pipeline': inner}}) == inner


def test_load_pipeline_reads_dot_pipe_file(tmp_path):
    pipeline = {'source': 'a', 'components': []}
    f = tmp_path / 'x.pipe'
    f.write_text(json.dumps(pipeline), encoding='utf-8')

    assert load_pipeline({'filepath': str(f)}) == pipeline


def test_load_pipeline_reads_and_unwraps_dot_pipe_file(tmp_path):
    inner = {'source': 'a', 'components': []}
    f = tmp_path / 'x.pipe'
    f.write_text(json.dumps({'pipeline': inner}), encoding='utf-8')

    assert load_pipeline({'filepath': str(f)}) == inner


def test_load_pipeline_raises_when_neither_supplied():
    with pytest.raises(ValueError):
        load_pipeline({})


def test_load_pipeline_raises_when_not_an_object():
    with pytest.raises(ValueError):
        load_pipeline({'pipeline': ['not', 'an', 'object']})
