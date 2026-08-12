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
Shell static file handler.

Serves the shell web application (Module Federation host) from
``dist/server/static/shell/`` and MF remote app bundles from
``dist/server/static/apps/``.

Directory layout:
  - ``static/shell/index.html``              — SPA entry point
  - ``static/shell/static/``                 — JS/CSS bundles
  - ``static/shell/themes/``                 — theme JSON files
  - ``static/shell/favicon.svg``             — favicon
  - ``static/apps/<app>/``                   — MF remote app bundles

Routes:
  GET /                    — shell SPA entry point
  GET /shell/{file_path}   — shell assets (JS, CSS, themes)
  GET /apps/{file_path}    — MF remote app bundles
"""

import hashlib
import os
import sys
import time
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse

from ai.web import Request

# Engine binary directory — all static paths are relative to this.
_root_dir = os.path.dirname(sys.executable)

# Shell root: dist/server/static/shell/ — SPA, themes, JS/CSS bundles.
_shell_root = os.path.join(_root_dir, 'static', 'shell')

# Apps root: dist/server/static/apps/ — MF remote app bundles.
_apps_root = os.path.join(_root_dir, 'static', 'apps')


def _resolve_safe(base_dir: str, requested_path: str) -> Path:
    """
    Resolve a requested path within a base directory, guarding against
    path traversal attacks.

    Args:
        base_dir: Absolute path to the allowed root directory.
        requested_path: Relative path from the URL (may contain ``../``).

    Returns:
        Resolved Path within base_dir, or base_dir/index.html as fallback.
    """
    try:
        file_path = (Path(base_dir) / requested_path).resolve()
        root_path = Path(base_dir).resolve()

        # Traversal attempt — fall back to index.html
        if not file_path.is_relative_to(root_path):
            return root_path / 'index.html'

        return file_path
    except Exception:
        # Any resolution error — safe fallback
        return Path(base_dir) / 'index.html'


async def shell_static(request: Request):
    """
    Serve static files for the shell SPA with client-side routing fallback.

    Handles both ``GET /`` (serves index.html) and ``GET /shell/{path}``
    (strips the prefix and resolves within the shell root directory).

    Args:
        request: Incoming HTTP request.

    Returns:
        FileResponse for the matched file or index.html fallback.

    Raises:
        HTTPException: 503 if the shell has not been built.
    """
    # Map the URL path into the shell directory.
    # "/" → index.html
    # "/shell/static/js/main.js" → static/js/main.js
    # "/shell/themes/dark.json" → themes/dark.json
    raw_path = request.url.path.lstrip('/')

    # Strip the "shell/" prefix for shell-specific routes
    if raw_path.startswith('shell/'):
        raw_path = raw_path[len('shell/') :]

    # Default bare "/" to index.html
    if not raw_path:
        raw_path = 'index.html'

    # Resolve safely within the shell root
    file_path = _resolve_safe(_shell_root, raw_path)

    # Serve the file if it exists
    if file_path.exists() and file_path.is_file():
        return FileResponse(file_path)

    # SPA fallback: serve index.html for any unmatched route so that
    # client-side routing (React Router, etc.) can handle it.
    index_path = Path(_shell_root) / 'index.html'
    if index_path.exists() and index_path.is_file():
        return FileResponse(index_path)

    # Shell hasn't been built yet
    raise HTTPException(
        status_code=503,
        detail='Shell UI not built. Run: ./builder shell:build',
    )


# =============================================================================
# APP-BUNDLE AUTHORIZATION (SaaS only)
# =============================================================================
# OSS is single-tenant — every app bundle is public. SaaS gates each bundle
# behind the caller's per-app permission: the browser carries the user token in
# an /apps-scoped cookie (minted by apps_session), and apps_static validates it
# and checks that the caller can load <app_id> before serving any file.

# The cookie the browser attaches to every /apps/* fetch (set by apps_session).
_APP_COOKIE = 'rr_apps_token'

# Sliding permission cache: sha256('<token>.<app_id>') -> {auth: bool, expiry}.
# DENIALS are cached too — an unauthorized caller hammering denied apps is then
# served from cache instead of a DB walk per file (a cheap-DoS guard). Every
# reference slides the entry 5 min forward. Only the permission DECISION is
# cached; token validity is (cheaply) re-derived inside _resolve_app_access.
_APP_AUTH_TTL = 300.0
_app_auth_cache: dict = {}


def _is_saas() -> bool:
    """True when the loaded account edition gates app bundles (SaaS)."""
    from ai.account import account

    return 'saas' in getattr(account, 'capabilities', ())


def _app_field(entry, name: str):
    """One field off a catalog entry (dict or pydantic model)."""
    return entry.get(name) if isinstance(entry, dict) else getattr(entry, name, None)


async def _resolve_app_access(token: str, app_id: str) -> bool:
    """Uncached: may the caller load ``app_id``'s bundle?

    The served directory IS the app id (the build emits
    ``dist/server/static/apps/<app_id>/``), so the first path segment of an
    ``/apps/...`` request is the app id we authorize. Defers to the same account
    resolver the store listing uses, so every gate (audience, binding state,
    requiredPermissions) folds into one boolean — an app the caller can't see is
    an app they can't fetch.

    An authenticated caller gets their full entitled set; an anonymous caller
    (or an unverifiable token) gets the PUBLIC set only — so public apps (the
    pre-auth landing/home) load without a session, private ones do not.
    """
    from ai.account import account

    info = None
    if token:
        try:
            info = await account.authenticate(token)
        except Exception:
            info = None
    # authenticate() returns an (int, str) error tuple on failure — treat any
    # non-AccountInfo as anonymous rather than a hard deny.
    if info is not None and hasattr(info, 'userId'):
        organizations = [o.model_dump() if hasattr(o, 'model_dump') else o for o in getattr(info, 'organizations', [])]
        sys_perms = list(getattr(info, 'sysPermissions', []) or [])
        try:
            apps = await account.get_apps_for_user(info.userId, organizations, sys_perms)
        except TypeError:
            # Older signature without sys_permissions.
            apps = await account.get_apps_for_user(info.userId, organizations)
    else:
        apps = await account.get_public_apps()

    return any(_app_field(a, 'id') == app_id for a in apps)


async def _authorize_app(token: str, app_id: str) -> bool:
    """Cached wrapper over _resolve_app_access (sliding 5-min window).

    Anonymous (empty token) is a VALID caller for public bundles, so it is
    cached too — the key folds the empty token.
    """
    if not app_id:
        return False
    key = hashlib.sha256(f'{token}.{app_id}'.encode('utf-8')).hexdigest()
    now = time.time()
    hit = _app_auth_cache.get(key)
    if hit is not None and hit['expiry'] > now:
        hit['expiry'] = now + _APP_AUTH_TTL  # slide on reference
        return hit['auth']
    auth = await _resolve_app_access(token, app_id)
    _app_auth_cache[key] = {'auth': auth, 'expiry': now + _APP_AUTH_TTL}
    # Bounded: opportunistically drop expired entries so the map can't grow
    # without limit under token/app churn.
    if len(_app_auth_cache) > 4096:
        for k in [k for k, v in _app_auth_cache.items() if v['expiry'] <= now]:
            _app_auth_cache.pop(k, None)
    return auth


async def apps_session(request: Request):
    """Mint the /apps auth cookie from the caller's Authorization header.

    Deliberately trust-free: it does NOT validate the token — it just stows
    whatever the already-authenticated shell hands over into a path-scoped,
    HttpOnly cookie so the browser attaches it to bundle fetches. The real gate
    is apps_static, which validates the token and checks per-app permission on
    every serve. Called by the shell right after a successful connect.
    """
    auth = request.headers.get('authorization', '')
    token = auth[7:].strip() if auth[:7].lower() == 'bearer ' else auth.strip()
    resp = JSONResponse({'ok': bool(token)})
    if token:
        resp.set_cookie(
            key=_APP_COOKIE,
            value=token,
            path='/apps',
            httponly=True,
            secure=(request.url.scheme == 'https'),
            samesite='lax',
        )
    return resp


async def apps_static(request: Request):
    """
    Serve MF remote app bundles from ``dist/server/static/apps/``.

    Handles ``GET /apps/{path}`` — resolves the path within the apps root
    and returns the file directly. No SPA fallback (these are JS/CSS assets).

    Args:
        request: Incoming HTTP request.

    Returns:
        FileResponse for the matched file.

    Raises:
        HTTPException: 404 if file not found, 503 if apps dir missing.
    """
    # "/apps/rocketride.pipeBuilder/remoteEntry.js" → rocketride.pipeBuilder/remoteEntry.js
    raw_path = request.url.path.lstrip('/')
    if raw_path.startswith('apps/'):
        raw_path = raw_path[len('apps/') :]

    if not raw_path:
        raise HTTPException(status_code=404, detail='Not found')

    # SaaS gates each bundle behind the caller's per-app permission; OSS serves
    # freely (single-tenant). The directory IS the app id, so its first path
    # segment is what we authorize. The user token rides an /apps-scoped cookie
    # minted by apps_session.
    if _is_saas():
        app_id = raw_path.split('/', 1)[0]
        token = request.cookies.get(_APP_COOKIE, '')
        if not await _authorize_app(token, app_id):
            raise HTTPException(status_code=403, detail=f'Not authorized for app {app_id}')

    # Apps haven't been built/copied yet — surface a clearer signal.
    if not os.path.isdir(_apps_root):
        raise HTTPException(status_code=503, detail='App bundles not built. Run the app build/copy step.')
    # Resolve safely within the apps root
    file_path = _resolve_safe(_apps_root, raw_path)

    # Serve the file if it exists
    if file_path.exists() and file_path.is_file():
        return FileResponse(file_path)

    raise HTTPException(status_code=404, detail='Not found')
