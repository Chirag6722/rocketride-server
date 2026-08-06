# Copyright 2026 Aparavi Software AG. MIT License.
"""MCP Apps (io.modelcontextprotocol/ui): embedded widget resources.

Widgets are single-file HTML bundles built from apps/mcp-widgets by
``builder mcp-widgets:build`` into ``apps/dist/`` next to this module (the
ai:build syncDir carries them into the server dist). Each widget is served as
a ``ui://`` resource with the profile mimeType; tools opt in by registering
with ``ui_resource_uri`` (see tooling.py). Hosts without the UI extension see
the plain JSON tool results, unchanged.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import mcp.types as types

UI_MIME_TYPE = 'text/html;profile=mcp-app'
UI_EXTENSION_ID = 'io.modelcontextprotocol/ui'

PIPELINES_TABLE_URI = 'ui://rocketride/pipelines-table.html'
DROPPER_URI = 'ui://rocketride/dropper.html'

_APPS_DIST = Path(__file__).parent / 'apps' / 'dist'


@dataclass(frozen=True)
class AppSpec:
    uri: str
    filename: str
    title: str
    needs_engine_origin: bool = False


APPS: List[AppSpec] = [
    AppSpec(
        uri=PIPELINES_TABLE_URI,
        filename='pipelines-table.html',
        title='Running pipelines',
    ),
    AppSpec(
        uri=DROPPER_URI,
        filename='dropper.html',
        title='Drop files',
        needs_engine_origin=True,
    ),
]


def extension_capability() -> dict:
    return {'mimeTypes': [UI_MIME_TYPE]}


def available_apps(apps_dir: Optional[Path] = None) -> List[AppSpec]:
    """Specs whose built HTML bundle actually exists on disk."""
    base = apps_dir if apps_dir is not None else _APPS_DIST
    return [spec for spec in APPS if (base / spec.filename).is_file()]


def list_ui_resources(apps_dir: Optional[Path] = None, engine_origin: Optional[str] = None) -> List[types.Resource]:
    out = []
    for spec in available_apps(apps_dir):
        meta = None
        if spec.needs_engine_origin and engine_origin:
            meta = {'ui': {'csp': {'connectDomains': [engine_origin]}}}
        out.append(types.Resource(uri=spec.uri, name=spec.title, mimeType=UI_MIME_TYPE, meta=meta))
    return out


def read_ui_resource(uri: str, apps_dir: Optional[Path] = None) -> Optional[str]:
    """Return the widget HTML for ``uri``, or None if unknown/not built."""
    base = apps_dir if apps_dir is not None else _APPS_DIST
    for spec in APPS:
        if spec.uri == uri and (base / spec.filename).is_file():
            return (base / spec.filename).read_text(encoding='utf-8')
    return None
