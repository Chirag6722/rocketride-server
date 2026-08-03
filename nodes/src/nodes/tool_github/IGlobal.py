# =============================================================================
# RocketRide Engine
# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# =============================================================================

"""
GitHub tool node - global (shared) state.

Reads the token, the default repo, the read-only flag, the published tool groups
and the raw-request switch from config.
"""

from __future__ import annotations

from ai.common.config import Config
from rocketlib import IGlobalBase, OPEN_MODE, warning

from .tool_groups import DEFAULT_GROUPS, normalize_groups, unknown_groups


class IGlobal(IGlobalBase):
    """Global state for tool_github."""

    token: str = ''
    default_repo: str = ''
    read_only: bool = False
    tool_groups: frozenset = DEFAULT_GROUPS
    allow_raw_request: bool = True

    def beginGlobal(self) -> None:
        if self.IEndpoint.endpoint.openMode == OPEN_MODE.CONFIG:
            return

        cfg = Config.getNodeConfig(self.glb.logicalType, self.glb.connConfig)
        self.token = str((cfg.get('token') or '')).strip()
        self.default_repo = str((cfg.get('defaultRepo') or '')).strip()
        self.read_only = bool(cfg.get('readOnly', False))
        self.tool_groups = normalize_groups(cfg.get('toolGroups'))
        self.allow_raw_request = bool(cfg.get('allowRawRequest', True))

        if not self.token:
            raise Exception('tool_github: token is required')

    def validateConfig(self) -> None:
        try:
            cfg = Config.getNodeConfig(self.glb.logicalType, self.glb.connConfig)
            if not str((cfg.get('token') or '')).strip():
                warning('token is required')
            unknown = unknown_groups(cfg.get('toolGroups'))
            if unknown:
                warning(f'unknown tool group(s): {", ".join(unknown)}')
        except Exception as e:
            warning(str(e))

    # No oversized-published-set warning here, unlike tool_pipedrive. That node can publish
    # 255 tools against a recommended ceiling of 120; this one tops out at 37 plus the raw
    # request tool, so the check could never fire.

    def endGlobal(self) -> None:
        self.token = ''
        self.default_repo = ''
        self.read_only = False
        self.tool_groups = DEFAULT_GROUPS
        self.allow_raw_request = True
