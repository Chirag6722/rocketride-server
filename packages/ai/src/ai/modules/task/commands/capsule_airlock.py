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
Capsule airlock — the mandatory secure-first gate for installable node capsules.

A node capsule is a ``.rrc`` file (a ZIP) carrying a self-contained node:

    <node>.rrc
    ├── capsule.json              # manifest: name, protocol, version, sha256, declares[]
    └── local_nodes/
        └── <node>/
            ├── services.json
            ├── __init__.py, IGlobal.py, IInstance.py, <impl>.py
            └── requirements.txt  (optional)

Because installing a capsule means the engine will later EXECUTE its Python, the
capsule is inspected here BEFORE anything is persisted or run. Nothing passes the
airlock unless it is packaged for evaluation (plain source, no install-time code,
declared intent) and clears the static scan. On any failure this raises
``AirlockRejected`` and the caller must abort — no store write, no execution.
"""

import ast
import hashlib
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List

MANIFEST_NAME = 'capsule.json'
NODES_ROOT = 'local_nodes'

# Calls that have no legitimate use inside a declarative node and are rejected
# outright (dynamic code execution / import escape hatches).
FORBIDDEN_CALLS = frozenset({'eval', 'exec', 'compile', '__import__'})

# Sensitive surfaces gated behind a DECLARED capability in capsule.json. A usage
# that is not declared is a declared-vs-actual mismatch and rejects the capsule.
CAPABILITY_MODULES = {
    'network': frozenset({'socket', 'http', 'urllib', 'urllib3', 'requests', 'httpx', 'ftplib', 'smtplib', 'asyncio'}),
    'subprocess': frozenset({'subprocess', 'multiprocessing', 'pty', 'os'}),
    'filesystem': frozenset({'shutil', 'pathlib', 'tempfile', 'glob'}),
}
# os.system / os.popen / os.exec* specifically require 'subprocess'.
OS_SUBPROCESS_ATTRS = frozenset({'system', 'popen', 'execv', 'execve', 'execvp', 'spawnv', 'spawnl', 'fork'})


class AirlockRejected(Exception):
    """Raised when a capsule fails validation. Carries the list of violations."""

    def __init__(self, violations: List[str]):
        self.violations = violations
        super().__init__('; '.join(violations) if violations else 'capsule rejected')


@dataclass
class CapsuleInfo:
    """Validated capsule metadata, returned only when the airlock passes."""

    name: str
    protocol: str
    version: str
    node_dir: str  # 'local_nodes/<node>'
    services_json: dict
    declares: List[str] = field(default_factory=list)
    files: Dict[str, bytes] = field(default_factory=dict)  # arcname -> bytes


def validate_capsule(zip_bytes: bytes) -> CapsuleInfo:
    """
    Run the full airlock over raw ``.rrc`` bytes.

    Returns a ``CapsuleInfo`` on success. Raises ``AirlockRejected`` (with every
    violation found) on any failure. The caller MUST NOT persist or execute a
    capsule that did not return from here cleanly.
    """
    violations: List[str] = []

    try:
        zf = zipfile.ZipFile(_as_stream(zip_bytes))
    except zipfile.BadZipFile:
        raise AirlockRejected(['capsule is not a valid ZIP archive'])

    names = zf.namelist()

    # Zip-slip / absolute path guard: reject traversal before touching contents.
    for name in names:
        norm = posixpath.normpath(name)
        if name.startswith('/') or norm.startswith('..') or posixpath.isabs(norm):
            violations.append(f'unsafe path in capsule: {name!r}')
    if violations:
        raise AirlockRejected(violations)

    # Manifest is required.
    if MANIFEST_NAME not in names:
        raise AirlockRejected([f'missing {MANIFEST_NAME} at capsule root'])
    try:
        manifest = json.loads(zf.read(MANIFEST_NAME).decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AirlockRejected([f'{MANIFEST_NAME} is not valid JSON: {exc}'])

    name = manifest.get('name')
    if not name or not isinstance(name, str) or not name.replace('_', '').isalnum():
        violations.append('capsule.json: "name" must be an alphanumeric/underscore identifier')
    declares = manifest.get('declares') or []
    if not isinstance(declares, list):
        violations.append('capsule.json: "declares" must be a list of capability strings')
        declares = []

    node_dir = f'{NODES_ROOT}/{name}' if name else None
    services_path = f'{node_dir}/services.json' if node_dir else None
    if not services_path or services_path not in names:
        violations.append(f'missing node manifest at {services_path or "local_nodes/<name>/services.json"}')

    # Everything the capsule ships must live under local_nodes/<name>/ (or be the
    # manifest). No stray files, no code outside the declared node folder.
    payload_files: Dict[str, bytes] = {}
    for name_ in names:
        if name_ == MANIFEST_NAME or name_.endswith('/'):
            continue
        if not node_dir or not name_.startswith(node_dir + '/'):
            violations.append(f'file outside {node_dir or "local_nodes/<name>"}/: {name_!r}')
            continue
        payload_files[name_] = zf.read(name_)

    # Packaged-for-evaluation: plain source only, no install-time execution hooks.
    for arc in payload_files:
        base = posixpath.basename(arc).lower()
        if base.endswith(('.pyc', '.pyo', '.so', '.dylib', '.dll', '.pyd')):
            violations.append(f'compiled/binary artifact not allowed: {arc!r}')
        if base in ('setup.py', 'setup.cfg', 'pyproject.toml', 'conftest.py'):
            violations.append(f'install-time hook not allowed: {arc!r}')

    if violations:
        raise AirlockRejected(violations)

    # sha256: the evaluated bytes must equal the recorded bytes.
    declared_sha = manifest.get('sha256')
    if declared_sha:
        actual = _payload_sha256(payload_files)
        if actual != declared_sha:
            raise AirlockRejected([f'sha256 mismatch: declared {declared_sha[:12]}…, computed {actual[:12]}…'])

    # Static scan of every Python source in the capsule.
    declared_caps = set(declares)
    for arc, data in payload_files.items():
        if not arc.endswith('.py'):
            continue
        try:
            source = data.decode('utf-8')
        except UnicodeDecodeError:
            violations.append(f'{arc}: not valid UTF-8 source')
            continue
        try:
            tree = ast.parse(source, filename=arc)
        except SyntaxError as exc:
            violations.append(f'{arc}: syntax error ({exc.msg})')
            continue
        violations.extend(_scan_python(tree, arc, declared_caps))

    if violations:
        raise AirlockRejected(violations)

    services_json = load_relaxed_json(zf.read(services_path).decode('utf-8'))
    protocol = services_json.get('protocol') or f'{name}://'
    return CapsuleInfo(
        name=name,
        protocol=protocol,
        version=str(manifest.get('version', '0.0.0')),
        node_dir=node_dir,
        services_json=services_json,
        declares=list(declared_caps),
        files=payload_files,
    )


def _scan_python(tree: ast.AST, arc: str, declared_caps: set) -> List[str]:
    """Static AST scan: forbidden calls always reject; sensitive modules require a declared capability."""
    out: List[str] = []
    # Map module -> capability that must be declared to use it.
    module_gate: Dict[str, str] = {}
    for cap, mods in CAPABILITY_MODULES.items():
        for mod in mods:
            module_gate[mod] = cap

    for node in ast.walk(tree):
        # Imports of capability-gated modules.
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split('.')[0]
                cap = module_gate.get(top)
                if cap and cap not in declared_caps:
                    out.append(f'{arc}:{node.lineno}: imports {top!r} but capsule does not declare "{cap}"')
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or '').split('.')[0]
            cap = module_gate.get(top)
            if cap and cap not in declared_caps:
                out.append(f'{arc}:{node.lineno}: imports from {top!r} but capsule does not declare "{cap}"')
        # Calls: forbidden builtins + os.system-family.
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in FORBIDDEN_CALLS:
                out.append(f'{arc}:{node.lineno}: forbidden call {fn.id}()')
            elif isinstance(fn, ast.Attribute) and fn.attr in OS_SUBPROCESS_ATTRS:
                if 'subprocess' not in declared_caps:
                    out.append(f'{arc}:{node.lineno}: {fn.attr}() requires declared "subprocess"')
    return out


def load_relaxed_json(text: str):
    """
    Parse RocketRide's relaxed JSON. Node ``services.json`` files carry ``//`` and
    ``/* */`` comments and trailing commas (the C++ engine tolerates them); strict
    ``json`` does not, so strip those first — without touching string contents.
    """
    return json.loads(_strip_relaxed_json(text))


def _strip_relaxed_json(text: str) -> str:
    out = []
    i, n = 0, len(text)
    in_str = False
    quote = ''
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                in_str = False
            i += 1
            continue
        if c in ('"', "'"):
            in_str = True
            quote = c
            out.append(c)
        elif c == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                i += 1
            continue
        elif c == '/' and i + 1 < n and text[i + 1] == '*':
            i += 2
            while i + 1 < n and not (text[i] == '*' and text[i + 1] == '/'):
                i += 1
            i += 2
            continue
        else:
            out.append(c)
        i += 1
    # Drop trailing commas before a closing } or ].
    return re.sub(r',(\s*[}\]])', r'\1', ''.join(out))


def _payload_sha256(files: Dict[str, bytes]) -> str:
    """Deterministic hash over the payload: names + bytes in sorted order."""
    h = hashlib.sha256()
    for arc in sorted(files):
        h.update(arc.encode('utf-8'))
        h.update(b'\0')
        h.update(files[arc])
        h.update(b'\0')
    return h.hexdigest()


def _as_stream(data: bytes):
    import io

    return io.BytesIO(data)
