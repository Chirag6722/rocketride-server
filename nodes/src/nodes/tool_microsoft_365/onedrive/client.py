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


"""OneDrive service bindings, addressing helpers, and response cleaners."""

from __future__ import annotations

import functools
import json
import urllib.error
import urllib.parse
import urllib.request

from .. import graph_client

SERVICE = graph_client.GraphService(product='OneDrive', superset_scopes=frozenset({'Files.ReadWrite.All'}))

token_scope_report = functools.partial(graph_client.token_scope_report, SERVICE)
request = functools.partial(graph_client.request, SERVICE)

# Graph's ceiling for a single PUT .../content upload; above this a resumable
# upload session is required.
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
# Graph's recommended resumable-upload chunk size (a multiple of 320 KiB).
CHUNK_SIZE = 5 * 1024 * 1024
# Above this, onedrive_download returns a downloadUrl instead of inlining bytes.
DOWNLOAD_INLINE_LIMIT = 1024 * 1024

# Local-parts that read as a distribution/org-wide alias rather than a named
# individual (e.g. 'all@contoso.com'). onedrive_invite gates any of these
# behind allowPublicSharing, the same spirit as Drive's domain-wide sharing
# gate: these addresses fan out to everyone in the tenant, not one person.
ORG_WIDE_ALIAS_LOCALPARTS = frozenset({'all', 'everyone', 'staff', 'company', 'org', 'orgwide', 'team'})


def _seg(value: str) -> str:
    """URL-encode a single path segment (item/permission ids may contain '!' etc.)."""
    return urllib.parse.quote(value, safe='')


def it(base: str, item: str) -> str:
    """Item address for a drive path ('Docs/a.pdf', containing '/') or a single-segment item id.

    Paths are left unencoded (they may contain multiple already-valid
    segments, mirroring excel/client.py's ``wb()``); a bare item id is a
    single path component and is URL-escaped.
    """
    if '/' in item:
        return f'{base}/drive/root:/{item}:'
    return f'{base}/drive/items/{_seg(item)}'


def parent_ref(target_folder: str) -> dict:
    """``parentReference`` body for a target given as a drive path or an item id."""
    if '/' in target_folder:
        return {'path': f'/drive/root:/{target_folder}'}
    return {'id': target_folder}


def is_org_wide_alias(email: str) -> bool:
    """True when an email's local-part reads as a distribution/org-wide alias."""
    local = email.split('@', 1)[0].strip().lower()
    return local in ORG_WIDE_ALIAS_LOCALPARTS


def upload_chunk(auth, session_url: str, chunk: bytes, start: int, end: int, total: int) -> dict:
    """PUT one chunk of a resumable upload session directly via urllib.

    ``graph_client.request()`` has no extra-headers kwarg yet (Task 9 adds
    one); a chunked upload PUT must carry a ``Content-Range`` header
    alongside the bearer token, so this call goes straight through urllib
    instead of the shared ``request()`` helper. It reuses
    ``graph_client._urlopen`` — the module's one HTTP seam — so tests can
    monkeypatch every Graph call, chunked or not, in one place.
    """
    req = urllib.request.Request(
        session_url,
        data=chunk,
        headers={
            'Authorization': f'Bearer {auth.token()}',
            'Content-Length': str(len(chunk)),
            'Content-Range': f'bytes {start}-{end}/{total}',
            'Content-Type': 'application/octet-stream',
        },
        method='PUT',
    )
    try:
        with graph_client._urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode(errors='replace')
        except Exception:
            detail = str(exc)
        raise graph_client.GraphError(f'OneDrive: chunked upload failed (HTTP {exc.code}; {detail[:200]}).') from exc
    except urllib.error.URLError as exc:
        raise graph_client.GraphError(f'OneDrive: chunked upload failed (connection error: {exc}).') from exc
    return json.loads(raw.decode()) if raw.strip() else {}


# ---------------------------------------------------------------------------
# Response cleaners
# ---------------------------------------------------------------------------

_ITEM_FIELDS = ('id', 'name', 'size', 'webUrl', 'folder', 'file', 'lastModifiedDateTime')


def clean_item(item: dict) -> dict:
    out = {k: item.get(k) for k in _ITEM_FIELDS if k in item}
    parent = item.get('parentReference')
    if isinstance(parent, dict) and 'path' in parent:
        out['parentReference'] = {'path': parent.get('path')}
    return out


def clean_permission(p: dict) -> dict:
    return {k: p.get(k) for k in ('id', 'roles', 'link', 'grantedToV2', 'grantedToIdentitiesV2') if k in p}
