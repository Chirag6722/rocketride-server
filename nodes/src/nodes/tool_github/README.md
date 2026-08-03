# tool_github

A RocketRide tool node that exposes GitHub repository operations to an AI agent.

## What it does

Gives an agent full access to the GitHub REST API: files, issues, pull requests,
reviews, releases, workflows, organizations, users, search, and commit history. Useful
for agents that manage codebases, triage issues, automate releases, or operate CI/CD
pipelines.

Uses the **requests** library to call the GitHub REST API v3 (`https://api.github.com`,
API version `2022-11-28`) with Bearer-token auth and a 30-second request timeout. API
responses are stripped of noisy fields (`node_id`, `_links`, gravatar data, etc.) so the
agent gets compact, useful output.

A personal access token is **required**: the pipeline fails to start without one. Write
operations are **allowed by default**; enable **read-only mode** to hide every mutating
tool when the agent should only inspect.

Beyond the typed tools, a generic `request` tool reaches any other GitHub REST endpoint,
so an agent is not limited to the operations wired up here.

---

## Configuration


| Field | Type | Description |
|---|---|---|
| `token` | string | Default empty. GitHub PAT with repo, issues, pull_requests, and workflows scopes. Use a fine-grained token scoped to only the repos you need. |
| `defaultRepo` | string | Default empty. Default repo in owner/repo format (e.g. acme/myapp). Tool calls that omit the repo parameter will use this value. |
| `readOnly` | boolean | Default false. When enabled, every create, edit and delete tool is hidden from the agent rather than merely refused, and the generic request tool only accepts GET. |
| `toolGroups` | array | Default empty, which publishes all 37 tools. Name groups to narrow the agent. See [Tool groups](#tool-groups). |
| `allowRawRequest` | boolean | Default true. Publishes the generic `request` tool, which reaches any GitHub REST endpoint. Not restricted by `toolGroups`. |


### Tool groups

`toolGroups` selects which groups reach the agent. **Leave it empty to publish all 37
tools**, which is the default and what this node has always done, so upgrading changes
nothing until the field is set.

| Group | Tools | Contents |
|---|---|---|
| `files` | 5 | File contents: read, list, create, edit, delete |
| `issues` | 6 | Issues, comments, labels, lock |
| `pull_requests` | 7 | Pull requests and their reviews |
| `repos` | 5 | Repository metadata, commits, repository listings (all read-only) |
| `releases` | 5 | Releases |
| `workflows` | 6 | GitHub Actions workflows |
| `search` | 2 | Code and issue search |
| `org_members` | 1 | Organization invitations |

Narrow it to give an agent one job. An issue-triage agent needs 13 tools:

```json
{ "toolGroups": ["issues", "search", "repos"] }
```

`all` is an explicit way to say everything. Names are matched case-insensitively, and a
name this node does not implement is reported as a warning in the editor and otherwise
ignored. The `request` tool is gated by `allowRawRequest` instead, not by this field.

### Rate limits and retries

The client reads GitHub's own headers rather than guessing. A `429`, and a `403` that is
demonstrably a rate limit, are retried up to 3 attempts, waiting for whatever
`retry-after` or `x-ratelimit-reset` asks for.

A `403` that names a permission, SSO or scope problem is **never** retried: it is
permanent, and retrying would burn budget while hiding the real error.

No single wait exceeds 60 seconds and no call sleeps more than 90 seconds in total. The
core rate limit resets hourly, so when the required wait is longer than the cap the call
fails immediately with the reset time in its message rather than holding a pipeline thread
for the best part of an hour. Failures on `5xx` and on network errors are retried for GET
only, since a write may already have applied.

### Repository resolution

Most tools accept an optional `repo` parameter (`owner/repo`). If omitted, the configured
`defaultRepo` is used; if neither is set, the call fails with an error asking for a repo.

> **Note:** `search_code` and `search_issues` also fall back to `defaultRepo`, when a
> default repo is configured, searches are scoped to it unless the call passes its own
> `repo`. To search across all accessible repositories, leave `defaultRepo` blank.

---

## Available tools

37 typed tools across 8 groups, plus the generic `request` escape hatch. Which groups are
published is controlled by `toolGroups`; the **Writes** column marks the tools that are
hidden when `readOnly` is on. List tools accept `per_page` (1-100, default 30) and `page`
(default 1).

### `files` (5 tools)

| Tool | Writes | Description |
|---|---|---|
| `file_get` | | Get a file's decoded content and metadata. Returns `found: false` on a 404 instead of failing. |
| `file_list` | | List files and directories at a path. |
| `file_create` | yes | Create a new file. |
| `file_edit` | yes | Update an existing file. Requires the current blob SHA from `file_get`. |
| `file_delete` | yes | Delete a file. Requires the current blob SHA from `file_get`. |

`file_get` raises when the path is a directory (use `file_list`), and `file_list` raises
when the path is a file (use `file_get`). Reads accept an optional `ref` (branch, tag or
commit SHA); writes accept an optional `branch` and a commit `message`.

### `issues` (6 tools)

| Tool | Writes | Description |
|---|---|---|
| `issue_get` | | Get a single issue by number. |
| `issue_list` | | List issues in a repository. |
| `issue_create` | yes | Create an issue. |
| `issue_comment` | yes | Post a comment on an issue. |
| `issue_edit` | yes | Edit title, body, state, labels or assignees. |
| `issue_lock` | yes | Lock an issue, optionally with a reason. |

GitHub returns pull requests from its issues endpoints; both `issue_get` and `issue_list`
filter them out, so use the `pull_requests` group for those.

### `pull_requests` (7 tools)

| Tool | Writes | Description |
|---|---|---|
| `pr_get` | | Get a single pull request by number. |
| `pr_list` | | List pull requests. |
| `pr_create` | yes | Open a pull request. |
| `review_get` | | Get a single review. |
| `review_list` | | List the reviews on a pull request. |
| `review_create` | yes | Submit an APPROVE, REQUEST_CHANGES or COMMENT review. |
| `review_update` | yes | Update the body of a pending review. |

Reviews live here rather than in a group of their own: a review exists only on a pull
request, so an agent given PRs without reviews could open one and never read the feedback.

### `repos` (5 tools)

| Tool | Writes | Description |
|---|---|---|
| `repo_get` | | Get repository metadata. |
| `commit_list` | | List commits, optionally filtered to a file path. |
| `commit_get` | | Get a commit with diff stats and per-file patches. |
| `org_list_repos` | | List an organization's repositories. |
| `user_get_repos` | | List a user's repositories, or the authenticated user's when no username is given. |

All read-only: this group answers "what repositories exist and what has happened in them".

### `releases` (5 tools)

| Tool | Writes | Description |
|---|---|---|
| `release_list` | | List releases. |
| `release_get` | | Get a release by id. |
| `release_create` | yes | Create a release. |
| `release_update` | yes | Update a release. |
| `release_delete` | yes | Delete a release. |

### `workflows` (6 tools)

| Tool | Writes | Description |
|---|---|---|
| `workflow_list` | | List GitHub Actions workflows. |
| `workflow_get` | | Get a workflow by id or filename. |
| `workflow_get_usage` | | Billable minutes for a workflow. |
| `workflow_dispatch` | yes | Trigger a `workflow_dispatch` event. |
| `workflow_enable` | yes | Enable a workflow. |
| `workflow_disable` | yes | Disable a workflow. |

### `search` (2 tools)

| Tool | Writes | Description |
|---|---|---|
| `search_code` | | Search code. Supports GitHub code-search syntax, e.g. `transport extension:py`. |
| `search_issues` | | Search issues and pull requests, with an optional `state` filter. |

Both fall back to `defaultRepo` when one is configured. A query that returns nothing is
retried once as an OR of its keywords, so a multi-word question still finds something.

### `org_members` (1 tool)

| Tool | Writes | Description |
|---|---|---|
| `user_invite` | yes | Invite a user to an organization by email. |

A single-tool group on purpose. This is the only tool whose blast radius is organization
membership rather than a repository, and it needs `admin:org` on the token, so it is
exactly the one an operator may want to switch off on its own.

### `request` (1 tool, gated by `allowRawRequest`)

| Tool | Writes | Description |
|---|---|---|
| `request` | for any non-GET method | Call any GitHub REST endpoint by method and path. |

The escape hatch for everything the typed tools do not model: branches, tags, labels,
milestones, gists, projects, teams, deployments, check runs, packages, branch protection
and the rest. It takes `method` and `path` plus optional `params` and `body`, and returns
the raw response body rather than a cleaned projection.

It is **not** filtered by `toolGroups`, so it reaches anything the token is scoped for. It
is still subject to `readOnly`, where it accepts GET only. Paths must start with `/` and
must not carry a query string; pass query parameters in `params`.

```json
{ "method": "GET", "path": "/repos/acme/app/branches/main/protection" }
```

---

## Read-only mode

When `readOnly` is `true`, the 17 mutating tools are **hidden from the agent** rather than
merely refused. They do not appear in the published tool list at all, so the agent never
sees a tool it can only fail on, and calling one anyway is still rejected.

This is a change from earlier behaviour, where every tool stayed visible and a write was
refused only once it had been attempted. An agent cannot tell in advance that a published
tool is blocked, so it spent a turn finding out, and 17 unusable tools is 17 tools' worth
of wasted context.

The `request` tool stays published in read-only mode, because it is still a working read
tool there. It accepts GET and refuses every other method.

Which tools count as writes is marked in the **Writes** column of each group table above.

Note the default is `false`: a freshly added node can write. Turn read-only mode on
explicitly for inspect-only agents.

---

## Authentication

Set `token` to a GitHub Personal Access Token. Classic tokens need the `repo`, `issues`,
`pull_requests`, and `workflows` scopes; a fine-grained token scoped to only the
repositories the agent needs is the safer choice. The token is sent as a
`Authorization: Bearer` header on every request; there is no unauthenticated mode.

API errors are surfaced to the agent as readable messages including the HTTP status and
GitHub's error details.

Upstream reference: [GitHub REST API documentation](https://docs.github.com/en/rest).

---

## Running the tests

```bash
# Stubbed suite: no credentials, no network
pytest nodes/test/tool_github/ -v

# Integration tests against a real repository (skipped unless both vars are set)
export GITHUB_TOKEN=<your token>
export GITHUB_TEST_REPO=owner/repo
pytest nodes/test/tool_github/test_tools.py -v
```

---

<!-- ROCKETRIDE:GENERATED:PARAMS START -->
<!-- Generated by nodes:docs-generate. Do not edit by hand. -->

## Schema

| Field | Type | Description | Default |
|---|---|---|---|
| `github.allowRawRequest` | `boolean` | **Allow raw API requests**<br/>Publishes the generic <b>request</b> tool, which can call any GitHub REST endpoint by method and path: branches, tags, labels, milestones, gists, projects, teams, deployments, check runs, branch protection and everything else the typed tools do not model. It uses the same authentication, rate-limit handling and read-only enforcement as the typed tools, but it is <b>not</b> restricted by Tool groups, so it reaches anything the configured token is scoped for. Disable to restrict the agent to the typed tools only. | `true` |
| `github.defaultRepo` | `string` | **Default Repository**<br/>Default repo in owner/repo format (e.g. acme/myapp). Tool calls that omit the repo parameter will use this value. | `""` |
| `github.readOnly` | `boolean` | **Read-only mode**<br/>When enabled, every create, edit and delete tool is <b>hidden from the agent</b> rather than merely refused, and the generic request tool only accepts GET. An agent cannot tell in advance that a published tool is blocked, so hiding saves it a wasted turn. Safe for agents that should only inspect repositories. | `false` |
| `github.token` | `string` | **Personal Access Token**<br/>GitHub PAT with repo, issues, pull_requests, and workflows scopes. Use a fine-grained token scoped to only the repos you need. | `""` |
| `github.toolGroups` | `array` | **Tool groups**<br/>Which groups of GitHub tools this node publishes to the agent. <b>Leave this empty to publish all 37 tools</b>, which is the default and what this node has always done. Name groups to narrow the agent to one job: for example <code>issues, search, repos</code> gives a 13-tool issue-triage agent. Available groups: files (5), issues (6), org_members (1), pull_requests (7), releases (5), repos (5), search (2), workflows (6). Use <b>all</b> as an explicit way to say everything. Names are matched case-insensitively, and a name this node does not implement is reported as a warning and otherwise ignored. | `[]` |

## Dependencies

- `requests` `>=2.34.2`
- `tenacity`

## Source

[<svg viewBox="0 0 16 16" width="15" height="15" fill="currentColor" aria-hidden="true" style="vertical-align:-0.15em;margin-right:0.35em"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg> View source](https://github.com/rocketride-org/rocketride-server/tree/develop/nodes/src/nodes/tool_github)
<!-- ROCKETRIDE:GENERATED:PARAMS END -->
