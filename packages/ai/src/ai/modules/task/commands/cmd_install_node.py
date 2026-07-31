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
InstallNodeCommands: DAP handler for ``rrext_install_node`` — the capsule launcher.

Installs a self-contained node capsule (``.rr``) into the caller's store so it
becomes usable in pipelines, without restarting the engine:

    client (VSCode / SaaS web)  --rrext_install_node-->  engine
        1. AIRLOCK (mandatory)  — validate_capsule(); reject before anything persists
        2. STORE WRITE          — user's joined filesystem (#1686), local_nodes/<name>/
        3. DEPENDS              — install the node's pinned requirements.txt
        --> catalog overlay picks it up live (cmd_misc.on_rrext_services)
        --> per-run child loads it from the store (task_engine node_path materialization)

Secure-first: nothing is written to the store and no dependency is installed
until the airlock passes.
"""

import asyncio
import base64
import posixpath
from typing import Any, Dict

from ai.common.dap.dap_conn import DAPConn

from .capsule_airlock import AirlockRejected, CapsuleInfo, validate_capsule

# Root (relative to the caller's file area) under which installed node capsules
# live. Matches the capsule's internal ``local_nodes/`` layout and the path the
# catalog overlay and per-run materializer read back from.
STORE_NODES_ROOT = 'local_nodes'


class InstallNodeCommands(DAPConn):
    """DAP router for ``rrext_install_node`` — install a node capsule into the caller's store."""

    def __init__(self, connection_id: int, server: 'TaskServer', transport, **kwargs) -> None:  # noqa: F821
        """No per-command state; identity/store access come from the other TaskConn mixins."""
        pass

    async def on_rrext_install_node(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Install a node capsule for the authenticated caller.

        Arguments (in ``request['arguments']``):
            capsule (str): base64-encoded ``.rr`` bytes (primary path), OR
            path (str):    store path to an already-uploaded ``.rr``.

        Returns a DAP response whose body is either
            {'ok': True, 'installed': <name>, 'protocol': <p>, 'version': <v>} or
            {'ok': False, 'violations': [...]}  when the airlock rejects it.
        """
        args = request.get('arguments') or {}
        try:
            zip_bytes = await self._resolve_capsule_bytes(args)

            # 1. AIRLOCK — mandatory gate. On rejection nothing else runs.
            try:
                info = validate_capsule(zip_bytes)
            except AirlockRejected as rej:
                self.debug_message(f'Capsule rejected by airlock: {rej.violations}')
                return self.build_response(request, body={'ok': False, 'violations': rej.violations})

            # 2. STORE WRITE — into the caller's joined filesystem (#1686).
            await self._write_capsule_to_store(info)

            # 3. DEPENDS — install the node's pinned requirements into the shared env.
            await self._install_requirements(info)

            self.debug_message(f'Installed node capsule {info.name!r} ({info.protocol})')
            return self.build_response(
                request,
                body={'ok': True, 'installed': info.name, 'protocol': info.protocol, 'version': info.version},
            )
        except Exception as e:
            self.debug_message(f'rrext_install_node failed: {e}')
            raise

    # -------------------------------------------------------------------------

    async def _resolve_capsule_bytes(self, args: Dict[str, Any]) -> bytes:
        """Get the raw .rr bytes from a base64 payload or an uploaded store path."""
        b64 = args.get('capsule')
        if b64:
            try:
                return base64.b64decode(b64)
            except (ValueError, TypeError) as exc:
                raise ValueError(f'"capsule" is not valid base64: {exc}')

        path = args.get('path')
        if path:
            from ai.account import Store

            fs = Store.file_store(self.request_context())
            meta = await fs.open_read(path)
            handle = meta['handle']
            chunks = bytearray()
            offset = 0
            try:
                while True:
                    chunk = await fs.read_chunk(handle, offset)
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    offset += len(chunk)
            finally:
                await fs.close_read(handle)
            return bytes(chunks)

        raise ValueError('rrext_install_node requires "capsule" (base64) or "path"')

    async def _write_capsule_to_store(self, info: CapsuleInfo) -> None:
        """Persist the validated capsule files under the caller's ``local_nodes/<name>/``."""
        from ai.account import Store

        fs = Store.file_store(self.request_context())

        # Replace any prior install of this node so re-uploads are idempotent.
        node_dir = f'{STORE_NODES_ROOT}/{info.name}'
        try:
            await fs.rmdir(node_dir, recursive=True)
        except Exception:
            pass  # first install: nothing to remove

        made: set = set()
        for arc in sorted(info.files):
            parent = posixpath.dirname(arc)
            if parent and parent not in made:
                await fs.mkdir(parent)
                made.add(parent)
            handle = await fs.open_write(arc)
            try:
                await fs.write_chunk(handle, info.files[arc])
            finally:
                await fs.close_write(handle)

    async def _install_requirements(self, info: CapsuleInfo) -> None:
        """Install the node's pinned requirements.txt (if any) into the shared engine env."""
        req_arc = f'{info.node_dir}/requirements.txt'
        req_bytes = info.files.get(req_arc)
        if not req_bytes or not req_bytes.strip():
            return

        import os
        import tempfile

        from depends import depends  # type: ignore

        with tempfile.TemporaryDirectory(prefix=f'capsule-{info.name}-') as tmp:
            req_path = os.path.join(tmp, 'requirements.txt')
            with open(req_path, 'wb') as fh:
                fh.write(req_bytes)
            # depends() is synchronous (bootstraps uv/pip); keep the loop free.
            await asyncio.get_event_loop().run_in_executor(None, depends, req_path)
