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
Tool grouping for the GitHub node.

This node implements 37 tools plus the generic ``request`` escape hatch. That is a
workable number for an LLM, so unlike tool_pipedrive (255 tools) and tool_gohighlevel
(101) the default here is the full surface: ``DEFAULT_GROUPS`` is ``ALL_GROUPS``.
Gating exists so an operator can deliberately narrow the agent ("this one only triages
issues"), not because the whole set is unusable. Narrowing the default would also
silently break every pipeline already running against this shipped node, with no error
naming the cause: the tool would simply stop existing.

Tools that change state are stamped too, and are dropped from the published set when
the node is in read-only mode. Both filters run in ``IInstance._collect_tool_methods()``,
so a tool that is not published is invisible to ``tool.query`` and rejected by
``tool.invoke`` alike.

Unlike the two sibling nodes the tools are NOT split into a ``tools/`` mixin package.
Three reasons, so nobody re-opens it: 37 tools fit one file; there is no shared request
base to factor out (``github_client.call`` already is one, against pipedrive's 850-line
``tools/_base.py``); and ``nodes/test/tool_github/test_search_relax.py`` reaches
``IInstance._relax_query`` and monkeypatches ``IInstance.call``, both of which a split
would break, rewriting 13 recently written tests for no behavioural gain.

This module is the single source of truth for the group names and for the default set.
``services.json`` does not repeat the default: its ``toolGroups`` default is empty,
which ``normalize_groups`` resolves to ``DEFAULT_GROUPS`` here.
"""

from __future__ import annotations

from typing import Callable

from rocketlib import tool_function

#: Every group this node implements.
#:
#: Three groupings are deliberately coarser than the section banners in ``IInstance.py``:
#:
#: - ``reviews`` folds into ``pull_requests``. A review exists only on a pull request, so
#:   an operator who enabled PRs without reviews would get an agent that can open a PR and
#:   cannot read the feedback on it.
#: - ``repo``, ``commits`` and the two repo-listing tools fold into ``repos``. All five
#:   answer "what repositories exist and what has happened in them", and all five are
#:   read-only. Three separate one- and two-tool groups would be noise.
#: - ``org_members`` stays a single-tool group anyway. ``user_invite`` is the only tool
#:   whose blast radius is org membership rather than a repository, and it needs
#:   ``admin:org`` on the token, so it is exactly the tool an operator wants to switch off
#:   independently. ``org_list_repos`` and ``user_get_repos`` deliberately do not live
#:   here: they are read-only discovery and belong with ``repos``.
ALL_GROUPS = frozenset(
    {
        'files',
        'issues',
        'org_members',
        'pull_requests',
        'releases',
        'repos',
        'search',
        'workflows',
    }
)

#: The full surface. See the module docstring for why this is not a subset.
DEFAULT_GROUPS = ALL_GROUPS

#: The generic escape-hatch tool, gated by ``github.allowRawRequest`` instead of by a
#: tool group. It carries no group stamp at all.
RAW_REQUEST_TOOL = 'request'


def group_names(raw) -> list[str]:
    """The non-empty names in a configured ``toolGroups`` value, as the operator typed them.

    Accepts a list or a comma-separated string; anything else yields no names. This is the
    shared front end of :func:`normalize_groups` and :func:`unknown_groups` so the runtime
    selection and the editor warning can never disagree about what was configured.
    """
    if isinstance(raw, str):
        raw = raw.split(',')
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return []
    return [str(g).strip() for g in raw if str(g).strip()]


def normalize_groups(raw) -> frozenset:
    """Turn the configured ``toolGroups`` value into a set of known group names.

    Matching is case-insensitive, unknown names are ignored (they are surfaced as a warning
    by ``IGlobal.validateConfig``), and ``all`` means every implemented group. An empty or
    missing value falls back to the defaults, which here is also every implemented group.
    """
    names = {name.lower() for name in group_names(raw)}
    if not names:
        return DEFAULT_GROUPS
    if 'all' in names or '*' in names:
        return ALL_GROUPS
    selected = names & ALL_GROUPS
    return frozenset(selected) if selected else DEFAULT_GROUPS


def unknown_groups(raw) -> list[str]:
    """Configured group names this node does not implement.

    Matches exactly what :func:`normalize_groups` accepts, so a name that works at runtime is
    never reported as unknown in the editor, and a typo in the comma-separated string form is
    caught rather than skipped. Names come back as the operator typed them.
    """
    known = ALL_GROUPS | {'all', '*'}
    return sorted({name for name in group_names(raw) if name.lower() not in known})


def tool_counts_by_group() -> dict[str, int]:
    """Map each group to the number of tools it publishes.

    Exists so a test can pin the counts the README tables and the ``services.json``
    description quote, which otherwise drift the moment a tool is added or removed.

    ``IInstance`` is imported lazily because it imports this module, so a top-level import
    would be circular.
    """
    from .IInstance import IInstance

    counts: dict[str, int] = {}
    for name in dir(IInstance):
        group = getattr(getattr(IInstance, name, None), '__github_group__', None)
        if group:
            counts[group] = counts.get(group, 0) + 1
    return counts


def github_tool(*, group: str, writes: bool = False, input_schema=None, description=None) -> Callable:
    """``@tool_function`` plus the resource group the tool belongs to.

    The group is stamped on the function so ``IInstance._collect_tool_methods()`` can filter
    the published tool list without a separate registry to keep in sync. An unknown group
    name raises here, at import time, so a typo is a module-load failure rather than a tool
    that silently never reaches the agent.

    ``writes=True`` marks a tool that creates, updates or deletes something. It is stamped as
    ``__github_writes__`` and read by the same filter, which drops write tools from the
    published set when ``github.readOnly`` is on. That follows tool_gohighlevel rather than
    tool_pipedrive, which publishes its write tools in read-only mode and refuses them at
    ``tool.invoke``: an agent cannot tell in advance that a published tool is blocked, so it
    spends a turn finding out, and 17 tools it can only ever fail on are 17 tools' worth of
    wasted context. Hiding beats refusing. ``IInstance._require_write`` still guards invoke,
    both as defence in depth and because the raw ``request`` tool carries no stamp at all.
    """
    if group not in ALL_GROUPS:
        raise ValueError(f'github_tool: unknown group "{group}"')

    def decorator(fn: Callable) -> Callable:
        fn = tool_function(input_schema=input_schema, description=description)(fn)
        fn.__github_group__ = group
        fn.__github_writes__ = bool(writes)
        return fn

    return decorator
