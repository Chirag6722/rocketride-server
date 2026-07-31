# MIT License
#
# Copyright (c) 2026 Aparavi Software AG
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
Capsule packer — turn a node folder into a ``.rrc`` capsule the airlock accepts.

Mirrors the format defined in ``capsule_airlock``: a ZIP with ``capsule.json`` at
the root and the node under ``local_nodes/<name>/``. The sha256 is computed with
the SAME function the airlock verifies with, so a freshly packed capsule always
passes the checksum gate.

CLI:
    python -m ai.modules.task.commands.capsule_pack \\
        --node /path/to/claude_code_agent --out claude_code_agent.rrc \\
        --declares network subprocess
"""

import io
import json
import os
import zipfile
from typing import List, Optional

from .capsule_airlock import MANIFEST_NAME, NODES_ROOT, _payload_sha256

# Files that never belong in a capsule (build/runtime cruft).
_SKIP_DIRS = frozenset({'__pycache__', '.git', '.mypy_cache', '.ruff_cache'})
_SKIP_SUFFIX = ('.pyc', '.pyo')


def pack_node_dir(node_src_dir: str, version: str = '0.0.0', declares: Optional[List[str]] = None) -> bytes:
    """
    Build capsule bytes from a node source folder.

    Args:
        node_src_dir: Path to the node folder (must contain services.json).
        version: Capsule version string, recorded in the manifest.
        declares: Capability strings the node needs (network/subprocess/filesystem).

    Returns:
        The ``.rrc`` bytes, ready to send to ``rrext_install_node``.
    """
    node_src_dir = os.path.abspath(node_src_dir)
    name = os.path.basename(node_src_dir.rstrip('/'))
    services_path = os.path.join(node_src_dir, 'services.json')
    if not os.path.isfile(services_path):
        raise ValueError(f'no services.json under {node_src_dir}')
    with open(services_path, 'r', encoding='utf-8') as fh:
        services = json.load(fh)
    protocol = services.get('protocol') or f'{name}://'

    # Collect payload arcname -> bytes under local_nodes/<name>/.
    node_arc_root = f'{NODES_ROOT}/{name}'
    payload = {}
    for root, dirs, files in os.walk(node_src_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if fname.endswith(_SKIP_SUFFIX):
                continue
            abs_path = os.path.join(root, fname)
            rel = os.path.relpath(abs_path, node_src_dir).replace(os.sep, '/')
            with open(abs_path, 'rb') as fh:
                payload[f'{node_arc_root}/{rel}'] = fh.read()

    manifest = {
        'name': name,
        'protocol': protocol,
        'version': version,
        'declares': list(declares or []),
        'sha256': _payload_sha256(payload),
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True))
        for arc in sorted(payload):
            zf.writestr(arc, payload[arc])
    return buf.getvalue()


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description='Pack a node folder into a .rrc capsule')
    ap.add_argument('--node', required=True, help='path to the node source folder')
    ap.add_argument('--out', required=True, help='output .rrc path')
    ap.add_argument('--version', default='0.0.0')
    ap.add_argument('--declares', nargs='*', default=[], help='declared capabilities')
    args = ap.parse_args()

    data = pack_node_dir(args.node, version=args.version, declares=args.declares)
    with open(args.out, 'wb') as fh:
        fh.write(data)
    print(f'wrote {args.out} ({len(data)} bytes)')
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
