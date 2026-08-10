# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""The managed SFU resolves a MediaMTX build per platform and degrades to the spool on failure."""

import io
import tarfile

import pytest

from ai.account import sfu


def test_asset_maps_known_platforms(monkeypatch):
    monkeypatch.setattr(sfu.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(sfu.platform, 'machine', lambda: 'arm64')
    assert sfu._asset() == 'darwin_arm64'
    monkeypatch.setattr(sfu.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(sfu.platform, 'machine', lambda: 'x86_64')
    assert sfu._asset() == 'linux_amd64'


def test_asset_unknown_platform_is_none(monkeypatch):
    monkeypatch.setattr(sfu.platform, 'system', lambda: 'Plan9')
    monkeypatch.setattr(sfu.platform, 'machine', lambda: 'pdp11')
    assert sfu._asset() is None


def test_safe_extract_rejects_traversal(tmp_path):
    evil = tmp_path / 'evil.tar'
    with tarfile.open(evil, 'w') as t:
        info = tarfile.TarInfo('../escape')
        info.size = 1
        t.addfile(info, io.BytesIO(b'x'))
    with pytest.raises(ValueError):
        sfu._safe_extract(evil, tmp_path / 'out')


def test_ensure_managed_sfu_degrades_when_binary_unavailable(monkeypatch):
    monkeypatch.setattr(sfu, '_started', False)
    monkeypatch.setattr(sfu, '_ensure_binary', lambda: None)  # unsupported platform / download failed
    assert sfu.ensure_managed_sfu() is None
