"""
Unit tests for ai.common.sandbox.execute_sandboxed.

execute_sandboxed compiles agent code through RestrictedPython, executes it
inside a guarded namespace (limited builtins, allowlist-only ``__import__``,
PrintCollector for stdout, watchdog thread for timeout), and returns a
dict with stdout / stderr / exit_code / timed_out / optional result.

RestrictedPython is bundled with the engine via ai/common/requirements.txt,
so the real library is exercised here — no mocking needed for the happy
paths. Tests are written so they finish well before the default 20-second
timeout.
"""

from __future__ import annotations

import gc
import threading
import time

import pytest

from ai.common import sandbox
from ai.common.sandbox import execute_sandboxed


# ---------------------------------------------------------------------------
# Happy path — code execution + result capture
# ---------------------------------------------------------------------------


def test_simple_code_runs_and_collects_stdout():
    """A plain ``print`` call is captured into ``stdout`` and exit_code is 0."""
    result = execute_sandboxed('print("hello world")')
    assert result['exit_code'] == 0
    assert result['timed_out'] is False
    assert 'hello world' in result['stdout']
    assert result['stderr'] == ''


def test_result_variable_is_returned_for_primitive_values():
    """A ``result`` variable in the script is round-tripped in the return dict."""
    result = execute_sandboxed('result = 1 + 2')
    assert result['exit_code'] == 0
    assert result['result'] == 3


@pytest.mark.parametrize(
    'code, expected',
    [
        ('result = 42', 42),
        ('result = 3.14', 3.14),
        ('result = "hello"', 'hello'),
        ('result = True', True),
        ('result = [1, 2, 3]', [1, 2, 3]),
        ('result = {"a": 1, "b": 2}', {'a': 1, 'b': 2}),
        ('result = None', None),  # None is allowed but the dict will omit the key
    ],
)
def test_result_captures_primitive_types(code, expected):
    """All JSON-serialisable primitives in ``result`` are returned as-is."""
    out = execute_sandboxed(code)
    if expected is None:
        assert 'result' not in out  # None result is dropped by the source
    else:
        assert out['result'] == expected


def test_complex_object_falls_back_to_repr():
    """Non-primitive ``result`` values are stringified via ``repr``.

    Sets are not in the primitive allowlist for the ``result`` field
    (``str | int | float | bool | list | dict | None``), so they take the
    ``repr(...)`` fallback path.
    """
    out = execute_sandboxed('result = frozenset([1, 2, 3])')
    assert out['exit_code'] == 0
    assert isinstance(out['result'], str)
    assert 'frozenset' in out['result']


# ---------------------------------------------------------------------------
# Compilation errors
# ---------------------------------------------------------------------------


def test_syntax_error_is_returned_in_stderr():
    """A SyntaxError during compile yields exit_code=1 and the message in stderr."""
    out = execute_sandboxed('def : pass')  # invalid syntax
    assert out['exit_code'] == 1
    assert out['timed_out'] is False
    assert 'invalid' in out['stderr'].lower() or 'syntax' in out['stderr'].lower()


def test_restricted_python_policy_violation_is_blocked():
    """RestrictedPython rejects dunder name access at compile time."""
    out = execute_sandboxed('result = (1).__class__')
    # Compilation either returns None (policy violation) or raises a
    # SyntaxError-shaped message; either way the function exits non-zero.
    assert out['exit_code'] == 1
    assert out['timed_out'] is False
    assert out['stderr']  # non-empty


# ---------------------------------------------------------------------------
# Import allowlist
# ---------------------------------------------------------------------------


def test_allowed_default_module_can_be_imported():
    """``math`` is in the default allowlist; ``math.sqrt`` works inside the sandbox."""
    out = execute_sandboxed('import math\nresult = math.sqrt(16)')
    assert out['exit_code'] == 0
    assert out['result'] == 4.0


def test_disallowed_import_raises_import_error():
    """``os`` is not in the default allowlist; the import is rejected."""
    out = execute_sandboxed('import os\nresult = os.getcwd()')
    assert out['exit_code'] == 1
    assert 'not allowed' in out['stderr']


def test_custom_allowed_modules_extend_the_allowlist():
    """A caller-supplied ``allowed_modules`` set is merged with the defaults.

    Uses ``os``, which is **not** in the default allowlist, so the test
    actually exercises the merge path: the import fails without
    ``allowed_modules`` and succeeds once ``os`` is added.
    """
    # Without the extension, importing ``os`` is blocked.
    blocked = execute_sandboxed('import os\nresult = os.name')
    assert blocked['exit_code'] == 1
    assert 'not allowed' in blocked['stderr']

    # With ``os`` explicitly added, the import succeeds and runs.
    import os as _os

    allowed = execute_sandboxed('import os\nresult = os.name', allowed_modules={'os'})
    assert allowed['exit_code'] == 0
    assert allowed['result'] == _os.name


def test_submodule_top_level_check():
    """The allowlist is enforced on the top-level package, not the dotted submodule."""
    # ``json.decoder`` should be importable because ``json`` is allowed at the top.
    out = execute_sandboxed('import json.decoder\nresult = 1')
    assert out['exit_code'] == 0


# ---------------------------------------------------------------------------
# SystemExit handling
# ---------------------------------------------------------------------------


def test_sys_exit_with_int_code_is_captured():
    """Raise SystemExit(2) becomes exit_code=2 without stderr."""
    out = execute_sandboxed('raise SystemExit(2)')
    assert out['exit_code'] == 2
    assert out['timed_out'] is False
    assert out['stderr'] == ''


def test_sys_exit_with_no_arg_is_treated_as_zero():
    """Raise SystemExit() (no arg) sets exit_code=0."""
    out = execute_sandboxed('raise SystemExit()')
    assert out['exit_code'] == 0


def test_sys_exit_with_message_string_is_captured_in_stderr():
    """Raise SystemExit('msg') captures 'SystemExit: msg' in stderr and exit_code=1."""
    out = execute_sandboxed('raise SystemExit("explicit error")')
    assert out['exit_code'] == 1
    assert 'SystemExit' in out['stderr']
    assert 'explicit error' in out['stderr']


# ---------------------------------------------------------------------------
# Exception handling
# ---------------------------------------------------------------------------


def test_runtime_exception_lands_in_stderr_with_exit_one():
    """An unhandled exception during execution sets exit_code=1 and fills stderr."""
    out = execute_sandboxed('result = 1 / 0')
    assert out['exit_code'] == 1
    assert out['timed_out'] is False
    assert 'ZeroDivisionError' in out['stderr']


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_timeout_exits_with_minus_one_and_timed_out_flag():
    """A long-running script (much longer than the configured timeout) is killed."""
    # 1-second budget; the loop spins for far longer than that.
    out = execute_sandboxed(
        """
total = 0
for i in range(100_000_000):
    total += i
result = total
""",
        timeout=1,
    )
    assert out['timed_out'] is True
    assert out['exit_code'] == -1
    assert '1s' in out['stderr']


def _live_sandbox_threads() -> set[threading.Thread]:
    """Sandbox worker threads still running in this process."""
    return {t for t in threading.enumerate() if t.name == 'sandbox-exec' and t.is_alive()}


def _assert_new_workers_terminated(baseline: set[threading.Thread], message: str) -> None:
    """Wait for workers started after *baseline* to die, then assert they did.

    Compares thread IDENTITY against the baseline set rather than counting.
    Every worker shares the name ``sandbox-exec``, so a leftover from an
    earlier test in the same process can exit mid-wait and move a count in
    either direction; a set difference is independent of test order and of
    what any other worker does. Termination is asynchronous — the exception
    is delivered at the worker's next bytecode boundary — hence the wait.
    """
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _live_sandbox_threads() - baseline:
        time.sleep(0.05)
    assert not _live_sandbox_threads() - baseline, message


def test_timeout_actually_terminates_the_worker_thread():
    """The deadline stops the script, it does not merely stop waiting for it.

    ``Thread.join(timeout)`` returns without touching the thread, so before
    the fix a runaway script kept running inside the engine for the life of
    the process while the tool reported it as killed.
    """
    baseline = _live_sandbox_threads()

    out = execute_sandboxed('x = 0\nwhile True:\n    x += 1', timeout=1)

    assert out['timed_out'] is True
    assert out['exit_code'] == -1

    _assert_new_workers_terminated(baseline, 'timed-out script is still running')


def test_timeout_terminates_a_script_that_swallows_exceptions():
    """A loop wrapped in ``except Exception`` cannot survive its deadline.

    The interrupt is a ``BaseException`` subclass precisely so the broad
    handler an agent is likely to write around its own work does not eat it.
    """
    baseline = _live_sandbox_threads()

    out = execute_sandboxed(
        """
x = 0
while True:
    try:
        x += 1
    except Exception:
        pass
""",
        timeout=1,
    )

    assert out['timed_out'] is True

    _assert_new_workers_terminated(baseline, 'exception-swallowing script survived its deadline')


def test_timeout_releases_memory_allocated_by_the_script():
    """Killing the worker drops its frame, so its allocations are reclaimed.

    The pre-fix failure mode was an abandoned thread appending to a list at
    hundreds of MB/s until the OS killed the engine process — measured here
    as live objects rather than RSS, which the allocator may not return to
    the OS promptly.
    """
    baseline = _live_sandbox_threads()

    out = execute_sandboxed(
        """
chunks = []
while True:
    chunks.append('x' * 100000)
""",
        timeout=1,
    )
    assert out['timed_out'] is True

    _assert_new_workers_terminated(baseline, 'allocating script is still running')

    # Smoke check, not a bound: gc.get_objects() counts every tracked object in
    # the interpreter, so pytest internals and other threads contribute noise.
    # The tolerance is wide on purpose — the regression it guards against added
    # objects by the hundred thousand per second, which no amount of ambient
    # churn resembles. The assertion above already proves the worker is gone.
    gc.collect()
    first = len(gc.get_objects())
    time.sleep(0.5)
    gc.collect()
    second = len(gc.get_objects())
    assert second <= first + 20000, f'allocation continued after timeout ({first} -> {second} objects)'


def test_timeout_verdict_is_not_overwritten_by_the_dying_worker():
    """A script that re-raises on interruption cannot rewrite the verdict.

    The worker publishes into its own dict and the timeout path ignores it,
    so the caller always sees the timeout, never the script's parting error.
    """
    out = execute_sandboxed(
        """
x = 0
try:
    while True:
        x += 1
except BaseException:
    raise ValueError('script had the last word')
""",
        timeout=1,
    )

    assert out['timed_out'] is True
    assert out['exit_code'] == -1
    assert 'timed out' in out['stderr']
    assert 'script had the last word' not in out['stderr']


def test_sandbox_refuses_work_once_uninterruptible_threads_pile_up(monkeypatch):
    """Saturation is reported, not absorbed silently.

    Some blocking C calls cannot be interrupted between bytecodes. Rather
    than stacking more of them behind a timeout that cannot bite, the
    sandbox fails fast and says why.
    """
    monkeypatch.setattr(sandbox, '_live_abandoned_count', lambda: sandbox._MAX_ABANDONED_THREADS)

    out = execute_sandboxed('result = 1')

    assert out['exit_code'] == 1
    assert out['timed_out'] is False
    assert 'Sandbox unavailable' in out['stderr']
    assert 'result' not in out


# ---------------------------------------------------------------------------
# Termination interlock (thread-id reuse)
# ---------------------------------------------------------------------------


def test_terminator_never_injects_once_the_worker_has_finished(monkeypatch):
    """No injection is aimed at an ident the worker no longer owns.

    CPython recycles ``Thread.ident`` after a thread exits. A terminator that
    checked ``is_alive()`` and then injected into a captured ident could land
    ``_SandboxTimeout`` — a ``BaseException`` — in an unrelated engine thread.
    ``_WorkerHandle.running`` is the interlock; with it clear, nothing fires.
    """
    injected: list[tuple[int, type]] = []
    monkeypatch.setattr(
        sandbox,
        '_async_raise',
        lambda ident, exc: injected.append((ident, exc)) or True,
    )

    # A live thread standing in for one whose id was recycled by another.
    release = threading.Event()
    victim = threading.Thread(target=release.wait, name='sandbox-exec', daemon=True)
    victim.start()
    try:
        handle = sandbox._WorkerHandle()
        handle.ident = victim.ident
        handle.started.set()  # it did start...
        handle.running = False  # ...and has already left exec

        sandbox._terminate_thread(victim, handle)

        assert injected == [], 'injected into a thread the handle no longer claims'
    finally:
        release.set()
        victim.join(timeout=5)


def test_terminator_injects_while_the_worker_still_claims_the_ident(monkeypatch):
    """The interlock does not disarm the normal path: a running worker is hit."""
    injected: list[tuple[int, type]] = []
    monkeypatch.setattr(
        sandbox,
        '_async_raise',
        lambda ident, exc: injected.append((ident, exc)) or True,
    )

    release = threading.Event()
    worker = threading.Thread(target=release.wait, name='sandbox-exec', daemon=True)
    worker.start()
    try:
        handle = sandbox._WorkerHandle()
        handle.ident = worker.ident
        handle.running = True
        handle.started.set()

        sandbox._terminate_thread(worker, handle)

        assert injected, 'a running worker was never interrupted'
        assert all(ident == worker.ident for ident, _ in injected)
        assert all(exc is sandbox._SandboxTimeout for _, exc in injected)
    finally:
        release.set()
        worker.join(timeout=5)


def test_clear_running_survives_an_injection_landing_inside_it():
    """``running`` is always cleared, even if the timeout arrives mid-clear.

    A stale ``running=True`` would re-open the reuse window for the next
    injection attempt, so the clear retries rather than propagating.
    """
    handle = sandbox._WorkerHandle()
    handle.running = True

    real_lock = handle.lock
    calls = {'n': 0}

    class _LockRaisingOnce:
        def __enter__(self):
            calls['n'] += 1
            if calls['n'] == 1:
                raise sandbox._SandboxTimeout()
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

    handle.lock = _LockRaisingOnce()

    sandbox._clear_running(handle)

    assert handle.running is False
    assert calls['n'] == 2, 'the interrupted clear was not retried'


def test_immediate_deadline_still_terminates_the_worker():
    """A zero-length deadline interrupts the script rather than orphaning it.

    ``join(0)`` returns before the worker has necessarily run its first
    statement, so the terminator sees the handle mid-startup. Treating that as
    "already finished" would skip injection entirely and leave the script
    running for the life of the process.
    """
    baseline = _live_sandbox_threads()

    out = execute_sandboxed('x = 0\nwhile True:\n    x += 1', timeout=0)

    assert out['timed_out'] is True
    assert out['exit_code'] == -1
    assert 'could not be interrupted' not in out['stderr']

    _assert_new_workers_terminated(baseline, 'script outlived a zero-length deadline')


def test_worker_that_has_not_published_yet_is_still_interrupted(monkeypatch):
    """The startup window is closed, not merely narrow.

    Widens the real gap between ``thread.start()`` and the worker announcing
    its ident, which is what makes the race observable on a loaded machine.
    Only the worker's publication is delayed — the termination logic is
    untouched.
    """
    baseline = _live_sandbox_threads()
    real_get_ident = threading.get_ident
    main_ident = real_get_ident()

    def _slow_get_ident():
        ident = real_get_ident()
        if ident != main_ident:
            time.sleep(0.2)
        return ident

    monkeypatch.setattr(sandbox.threading, 'get_ident', _slow_get_ident)

    out = execute_sandboxed('x = 0\nwhile True:\n    x += 1', timeout=0)

    assert out['timed_out'] is True
    assert 'could not be interrupted' not in out['stderr'], (
        'worker was reported uninterruptible when it had simply not started yet'
    )

    _assert_new_workers_terminated(baseline, 'unpublished worker was never interrupted')


def test_terminator_waits_for_a_worker_that_has_not_started(monkeypatch):
    """``running == False`` before startup must not be read as "finished"."""
    injected: list[int] = []
    monkeypatch.setattr(
        sandbox,
        '_async_raise',
        lambda ident, exc: injected.append(ident) or True,
    )

    release = threading.Event()
    worker = threading.Thread(target=release.wait, name='sandbox-exec', daemon=True)
    worker.start()
    try:
        handle = sandbox._WorkerHandle()
        handle.ident = worker.ident
        handle.running = False  # not published yet — `started` is still clear

        # Publish from another thread while the terminator is waiting.
        def _publish():
            time.sleep(0.2)
            with handle.lock:
                handle.running = True
            handle.started.set()

        publisher = threading.Thread(target=_publish, daemon=True)
        publisher.start()

        sandbox._terminate_thread(worker, handle)
        publisher.join(timeout=5)

        assert injected, 'terminator gave up on a worker that had not announced itself yet'
    finally:
        release.set()
        worker.join(timeout=5)
