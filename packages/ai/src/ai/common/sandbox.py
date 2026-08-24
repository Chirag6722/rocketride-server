# =============================================================================
# MIT License
# Copyright (c) 2024 RocketRide Inc.
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
Restricted Python execution sandbox.

Runs agent-supplied code via RestrictedPython inside a controlled namespace with:

1. **RestrictedPython compilation** — ``compile_restricted`` transforms the AST
   to inject runtime guard calls that prevent attribute/item access escapes.

2. **Safe builtins** — RestrictedPython's ``safe_builtins`` replaces the full
   ``__builtins__``, removing dangerous functions by default.

3. **Allowlist-only ``__import__``** — a gated ``__import__`` is injected that
   only permits modules explicitly listed in ``allowed_modules``.  Everything
   else raises ``ImportError``.

4. **stdout capture** via a ``StringIO``-backed ``print()`` override.

5. **Timeout enforcement** via a worker thread that is *interrupted* once its
   deadline passes.  ``thread.join(timeout)`` only stops waiting — it does not
   stop the script — so the deadline is followed by asynchronous exception
   injection (``PyThreadState_SetAsyncExc``) into the worker.  CPython checks
   for pending async exceptions between bytecodes, so a runaway pure-Python
   loop unwinds, its frame is dropped, and everything it allocated is
   reclaimed.  Injection is interlocked against thread-id reuse — see
   :class:`_WorkerHandle` and :func:`_terminate_thread`.
"""

from __future__ import annotations

import ctypes
import importlib
import subprocess
import operator
import sys
import threading
import traceback
import warnings
from typing import Any, Dict, List, Set

from RestrictedPython import compile_restricted, safe_builtins, PrintCollector
from RestrictedPython.Eval import default_guarded_getiter
from RestrictedPython.Guards import (
    full_write_guard,
    guarded_unpack_sequence,
    safer_getattr,
)

_TIMEOUT = 20
_MAX_OUTPUT = 51200  # 50 KB

# ── Timeout termination tuning ──────────────────────────────────────────────
# How many times an async exception is injected into a worker that blew its
# deadline. One injection is enough for ordinary runaway code; the retries
# exist for scripts that catch broadly inside their loop (a bare ``except:``
# swallows even a BaseException) — each retry lands on a later bytecode, and
# the count is bounded so a pathological script cannot spin us forever.
_KILL_ATTEMPTS = 5

# Grace period waited after each injection before checking again.
_KILL_GRACE_SECONDS = 0.25

# How long a terminator waits for a worker to announce itself before giving up
# on reaching it. A worker publishes its ident as its first statement, so this
# is scheduler latency and nothing more; the bound exists so a pathological
# interpreter state cannot wedge the caller.
_STARTUP_WAIT_SECONDS = 5.0

# Sandbox threads that survived every injection attempt (only reachable via a
# long-running C call, which cannot be interrupted between bytecodes) are
# tracked here. Past _MAX_ABANDONED_THREADS still-alive entries the sandbox
# stops accepting work rather than letting the engine process degrade with no
# signal to anyone.
_MAX_ABANDONED_THREADS = 4

# ── Default allowed modules ─────────────────────────────────────────────────
# Safe, pure-computation modules with no filesystem, network, or OS access.
_DEFAULT_ALLOWED_MODULES = frozenset(
    {
        'math',
        'cmath',
        'decimal',
        'fractions',
        'statistics',
        'random',
        'string',
        'textwrap',
        're',
        'json',
        'csv',
        'collections',
        'itertools',
        'functools',
        'operator',
        'copy',
        'dataclasses',
        'enum',
        'typing',
        'datetime',
        'time',
        'calendar',
        'base64',
        'hashlib',
        'hmac',
        'struct',
        'difflib',
        'pprint',
        'bisect',
        'heapq',
        'array',
        'numbers',
        'unicodedata',
    }
)


# ── Extra builtins added on top of RestrictedPython's safe_builtins ───────
# safe_builtins is intentionally minimal (no dict, list, enumerate, etc.).
# These are non-dangerous builtins agents need for everyday data work.
_EXTRA_SAFE_BUILTINS = frozenset(
    {
        'all',
        'any',
        'ascii',
        'bin',
        'bytearray',
        'dict',
        'enumerate',
        'filter',
        'format',
        'frozenset',
        'hasattr',
        'iter',
        'list',
        'map',
        'max',
        'min',
        'next',
        'object',
        'print',
        'reversed',
        'set',
        'sum',
        'super',
        'type',
    }
)


_INPLACE_OPS = {
    '+=': operator.iadd,
    '-=': operator.isub,
    '*=': operator.imul,
    '/=': operator.itruediv,
    '%=': operator.imod,
    '**=': operator.ipow,
    '<<=': operator.ilshift,
    '>>=': operator.irshift,
    '|=': operator.ior,
    '^=': operator.ixor,
    '&=': operator.iand,
    '//=': operator.ifloordiv,
    '@=': operator.imatmul,
}


def _guarded_getitem(obj: Any, key: Any) -> Any:
    """Allow subscript access — RestrictedPython requires this guard."""
    return obj[key]


# warnings.catch_warnings() swaps the interpreter-GLOBAL filter list, so two
# concurrent compiles would race on it (one thread's restore can clobber the
# other's filter mid-compile). The compile step is fast — one lock serializes
# it without meaningfully gating sandbox throughput.
_COMPILE_LOCK = threading.Lock()


class _SandboxTimeout(BaseException):
    """Injected into a sandbox worker whose deadline has passed.

    Derives from ``BaseException``, not ``Exception``, so the ordinary
    ``except Exception:`` an agent script is likely to write around its own
    work cannot swallow the interruption.
    """


# Workers that could not be interrupted (see _MAX_ABANDONED_THREADS). Guarded
# by _ABANDONED_LOCK; pruned of finished threads on every call.
_ABANDONED_LOCK = threading.Lock()
_ABANDONED_THREADS: List[threading.Thread] = []


def _async_raise(thread_id: int, exc_type: type) -> bool:
    """Schedule *exc_type* to be raised inside the thread with *thread_id*.

    Thin wrapper over ``PyThreadState_SetAsyncExc``. The exception is not
    raised at once: CPython delivers it the next time that thread crosses a
    bytecode boundary, which is why a script blocked inside a single long C
    call (one huge allocation, ``time.sleep``) cannot be reached this way.

    Returns True when the exception was armed, False when the thread had
    already finished or the call misbehaved.
    """
    # The C signature takes an `unsigned long` thread id, which is what
    # ctypes.c_ulong maps to on every platform we build for.
    modified = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread_id),
        ctypes.py_object(exc_type),
    )
    if modified == 0:
        # No such thread state — the worker finished on its own.
        return False
    if modified > 1:
        # Documented contract: more than one thread state touched means we
        # armed something we did not intend to. Undo it and report failure.
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread_id), None)
        return False
    return True


class _WorkerHandle:
    """Interlock that makes async-exception injection safe to aim.

    CPython recycles ``Thread.ident`` values once a thread exits, so a
    terminator that checks ``is_alive()`` and *then* injects into a captured
    ident can miss its window: the worker finishes in between, a brand-new
    thread inherits the id, and ``_SandboxTimeout`` — a ``BaseException``, so
    nothing ordinary catches it — lands in an unrelated engine thread.

    The handle closes that window. The worker sets ``running`` under ``lock``
    before it starts and clears it under the same lock on its way out; the
    terminator only injects while holding the lock and seeing ``running``.
    Because the worker cannot complete its own clear-and-exit without that
    lock, its id cannot be recycled while an injection is in flight.
    """

    __slots__ = ('lock', 'running', 'ident', 'started')

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.ident: int | None = None
        # Distinguishes "has not started yet" from "already finished"; both
        # read as running == False, and they need opposite responses.
        self.started = threading.Event()


def _clear_running(handle: _WorkerHandle) -> None:
    """Mark a worker finished, retrying if an injected timeout lands here.

    ``running`` MUST read False before the worker thread exits: the terminator
    trusts it to decide whether the captured ident still belongs to this
    worker, and a stale True is exactly the mis-aimed injection the handle
    exists to prevent. An injection can arrive while this runs (the worker is
    past ``exec`` but has not exited yet), so swallow it and try again.
    """
    while True:
        try:
            with handle.lock:
                handle.running = False
            return
        except _SandboxTimeout:
            continue


def _terminate_thread(thread: threading.Thread, handle: _WorkerHandle) -> bool:
    """Interrupt a sandbox worker that overran its deadline.

    Injects :class:`_SandboxTimeout` and waits a short grace period, repeating
    up to ``_KILL_ATTEMPTS`` times so a script that catches broadly inside its
    loop still loses the race. Returns True once the worker is gone, False if
    it survived every attempt (it is then tracked as abandoned).

    Every injection happens under ``handle.lock`` with ``handle.running`` still
    set — see :class:`_WorkerHandle` for why aiming at a bare ident is unsafe.
    """
    # Wait for the worker to announce itself first. Until it does,
    # ``running == False`` means "not started yet", not "already finished" —
    # and those need opposite responses. Reading the first as the second is
    # how a script could outlive a very short deadline entirely: the
    # terminator would decline to inject, report the worker as
    # uninterruptible, and leave it running for the life of the process.
    if not handle.started.wait(timeout=_STARTUP_WAIT_SECONDS) and thread.is_alive():
        return False

    for _ in range(_KILL_ATTEMPTS):
        if not thread.is_alive():
            return True

        with handle.lock:
            if not handle.running:
                # The worker is already past exec and on its way out. Never
                # inject now: the ident may belong to another thread by the
                # time the call lands.
                break
            if handle.ident is None or not _async_raise(handle.ident, _SandboxTimeout):
                break

        thread.join(timeout=_KILL_GRACE_SECONDS)

    # Give a worker that broke out of the loop mid-exit a moment to finish.
    if thread.is_alive():
        thread.join(timeout=_KILL_GRACE_SECONDS)
    return not thread.is_alive()


def _abandon(thread: threading.Thread) -> None:
    """Record a worker that could not be interrupted."""
    with _ABANDONED_LOCK:
        _ABANDONED_THREADS.append(thread)


def _live_abandoned_count() -> int:
    """Number of still-running uninterruptible workers, pruning finished ones."""
    with _ABANDONED_LOCK:
        _ABANDONED_THREADS[:] = [t for t in _ABANDONED_THREADS if t.is_alive()]
        return len(_ABANDONED_THREADS)


def execute_sandboxed(
    code: str,
    *,
    allowed_modules: Set[str] | None = None,
    timeout: int | None = None,
) -> Dict[str, Any]:
    """Run *code* in a RestrictedPython sandbox and return the result.

    Returns a dict with ``stdout``, ``stderr``, ``exit_code``, ``timed_out``,
    and ``result`` (the value of a variable named ``result`` if set by the
    code).

    *allowed_modules*, if provided, is merged with ``_DEFAULT_ALLOWED_MODULES``
    to form the full allowlist.  Only modules in this set can be imported.
    """
    # ── 0. Refuse to add load when earlier scripts could not be stopped ─
    # Uninterruptible workers keep burning CPU and holding memory for the
    # life of the process. Failing loudly here is strictly better than
    # quietly stacking more of them behind a timeout that cannot bite.
    stuck = _live_abandoned_count()
    if stuck >= _MAX_ABANDONED_THREADS:
        return {
            'stdout': '',
            'stderr': (
                f'[Sandbox unavailable: {stuck} script(s) from earlier timeouts could not be '
                f'interrupted and are still running. Restart the engine process to clear them.]'
            ),
            'exit_code': 1,
            'timed_out': False,
        }

    # ── 0b. Compile with RestrictedPython ──────────────────────────────
    try:
        # compile_restricted emits a SyntaxWarning ("Prints, but never reads
        # 'printed' variable") for ANY code that prints without reading the
        # collector variable. Stdout is collected via PrintCollector below,
        # so the hint is meaningless noise for every sandboxed script —
        # suppress exactly that message around the compile; any OTHER
        # SyntaxWarning a script provokes still surfaces. The lock makes
        # the global-filter swap safe under concurrent executions.
        with _COMPILE_LOCK, warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', category=SyntaxWarning, message=r".*Prints, but never reads 'printed' variable"
            )
            compiled = compile_restricted(
                code,
                filename='<agent_script>',
                mode='exec',
            )
    except SyntaxError as exc:
        return {
            'stdout': '',
            'stderr': str(exc),
            'exit_code': 1,
            'timed_out': False,
        }

    # compile_restricted returns None when it encounters policy violations
    if compiled is None:
        return {
            'stdout': '',
            'stderr': 'Code blocked by RestrictedPython compilation policy.',
            'exit_code': 1,
            'timed_out': False,
        }

    allowlist = _DEFAULT_ALLOWED_MODULES | (allowed_modules or set())

    # ── 1. Build safe builtins ─────────────────────────────────────────
    # RestrictedPython's safe_builtins is very minimal — it omits common
    # data-processing builtins that agents need (dict, list, enumerate, etc.).
    # We add back the ones that are safe for sandboxed computation.
    sandbox_builtins: Dict[str, Any] = dict(safe_builtins)
    import builtins as _builtins

    for _name in _EXTRA_SAFE_BUILTINS:
        sandbox_builtins[_name] = getattr(_builtins, _name)

    # ── 2. Inject allowlist-only __import__ ────────────────────────────
    original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

    def restricted_import(name: str, *args: Any, **kwargs: Any) -> Any:
        top_level = name.split('.')[0]
        if top_level not in allowlist:
            raise ImportError(f"Import of '{name}' is not allowed. Allowed modules: {', '.join(sorted(allowlist))}")
        try:
            return original_import(name, *args, **kwargs)
        except ModuleNotFoundError:
            # Module is allowed but not installed — auto-install via pip
            if top_level not in _DEFAULT_ALLOWED_MODULES:
                _pip_install(top_level)
                return original_import(name, *args, **kwargs)
            raise

    sandbox_builtins['__import__'] = restricted_import

    # ── 3. Execution namespace with RestrictedPython guards ──────────
    # RestrictedPython transforms print() calls to use PrintCollector.
    # After execution, the collected output is in the ``printed`` variable.
    sandbox_globals: Dict[str, Any] = {
        '__builtins__': sandbox_builtins,
        '_getattr_': safer_getattr,
        '_getitem_': _guarded_getitem,
        '_getiter_': default_guarded_getiter,
        '_iter_unpack_sequence_': guarded_unpack_sequence,
        '_write_': full_write_guard,
        '_inplacevar_': lambda op, x, y: _INPLACE_OPS[op](x, y),
        '_print_': PrintCollector,
        '_unpack_sequence_': guarded_unpack_sequence,
        '__metaclass__': type,
        '__name__': '<agent_script>',
    }

    # ── 5. Run in a worker thread, interrupting it on timeout ──────────
    # The worker publishes its outcome into `outcome` rather than closing over
    # the caller's variables: on the timeout path the worker is still unwinding
    # while this function builds the response, and a script that turns the
    # injected _SandboxTimeout into some other error must not be able to
    # overwrite the timeout verdict on its way out.
    outcome: Dict[str, Any] = {}
    handle = _WorkerHandle()

    def _run() -> None:
        # Announce first, before any work: the terminator blocks on `started`
        # so it can tell an unstarted worker from a finished one.
        handle.ident = threading.get_ident()
        with handle.lock:
            handle.running = True
        handle.started.set()
        try:
            exec(compiled, sandbox_globals)  # noqa: S102
        except _SandboxTimeout:
            # Deadline injection — the caller already owns the verdict.
            return
        except SystemExit as e:
            if e.code is None:
                outcome['exit_code'] = 0
            elif isinstance(e.code, int):
                outcome['exit_code'] = e.code
            else:
                outcome['stderr'] = f'SystemExit: {e.code}'
                outcome['exit_code'] = 1
        except Exception:
            outcome['stderr'] = traceback.format_exc()
            outcome['exit_code'] = 1
        finally:
            _clear_running(handle)

    effective_timeout = timeout if timeout is not None else _TIMEOUT
    thread = threading.Thread(target=_run, daemon=True, name='sandbox-exec')
    thread.start()
    thread.join(timeout=effective_timeout)

    timed_out = thread.is_alive()
    if timed_out:
        # join() only stopped waiting — actually stop the script, so its frame
        # is dropped and everything it allocated is released.
        killed = _terminate_thread(thread, handle)
        stderr = f'[Execution timed out after {effective_timeout}s]'
        if not killed:
            _abandon(thread)
            stderr += (
                ' [Warning: the script could not be interrupted and is still running '
                'in the background — it is likely blocked inside a single long-running '
                'operation. Restart the engine process to reclaim its resources.]'
            )
        exit_code = -1
    else:
        stderr = outcome.get('stderr', '')
        exit_code = outcome.get('exit_code', 0)

    # ── 6. Collect output ──────────────────────────────────────────────
    # RestrictedPython stores the PrintCollector instance as '_print';
    # calling it returns the collected text.
    _print_collector = sandbox_globals.get('_print')
    stdout = _truncate(_print_collector() if callable(_print_collector) else '')
    stderr = _truncate(stderr)

    result_val = sandbox_globals.get('result')
    response: Dict[str, Any] = {
        'stdout': stdout,
        'stderr': stderr,
        'exit_code': exit_code,
        'timed_out': timed_out,
    }

    if result_val is not None:
        try:
            response['result'] = (
                result_val
                if isinstance(result_val, (str, int, float, bool, list, dict, type(None)))
                else repr(result_val)
            )
        except Exception:
            response['result'] = repr(result_val)

    return response


def _pip_install(package: str) -> None:
    """Auto-install a package via pip. Only called for non-default allowlisted modules."""
    subprocess.check_call(
        [sys.executable, '-m', 'pip', 'install', '--quiet', package],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    # Clear the import cache so the freshly installed module is found
    importlib.invalidate_caches()


def _truncate(text: str, max_size: int = _MAX_OUTPUT) -> str:
    """Truncate output to *max_size* characters, keeping head and tail."""
    if len(text) <= max_size:
        return text
    marker = f'\n\n... [truncated — {len(text)} chars total, limit {max_size}] ...\n\n'
    half = (max_size - len(marker)) // 2
    return text[:half] + marker + text[-half:]
