# OneDrive tool node (`tool_onedrive`)

Exposes the Microsoft Graph drive API as agent tools: list and search files
and folders, read metadata, download and upload content, create folders,
copy/move/rename items, trash/restore, and manage sharing (links and
permission grants). Operates on the acting user's OneDrive.

## What it does

An agent-invocable tool node — no data flows through lanes. Each operation is
a `@tool_function` an agent calls on demand. Operational targets (item path
or id, folder, permission id) are always tool-call parameters, never node
config. Outputs are cleaned shapes (`id`, `name`, `size`, `webUrl`,
`folder`/`file`, `lastModifiedDateTime`, `parentReference.path`), not raw
Graph JSON.

Items are addressed by either a drive-relative path (`'Reports/q3.pdf'`) or a
drive item id — whichever the caller has on hand. A value containing `/` is
treated as a path; anything else is treated as an item id.

## Configuration

| Field | Notes |
|-------|-------|
| `microsoft.authType` | `service` (Entra app, client credentials) or `user` (OAuth). |
| `microsoft.tenantId` / `microsoft.clientId` / `microsoft.clientSecret` | Entra app credentials for `service` auth. |
| `microsoft.userPrincipalName` | Acting user's UPN for `service` auth (app-only calls target `/users/{upn}`). |
| `microsoft.oAuthButton` / `microsoft.userToken` | User OAuth: sign in to populate the access token. |
| `onedrive.access` | `readonly` or `write` (default). Resolved by the shared `ONEDRIVE` spec in `core/microsoft_access.py`; scopes are never hand-entered. |
| `onedrive.allowPublicSharing` | Off by default. Gates anonymous sharing links and org-wide alias invites. |
| `onedrive.allowHardDelete` | Off by default. Gates `onedrive_permanently_delete`; trash/restore are always available. |

### Access tiers → scopes

| Tier | Scope | Capability |
|------|-------|------------|
| `readonly` | `Files.Read` | list, search, read metadata, and download only |
| `write` | `Files.ReadWrite` | full read/write (default) |

### Gate flags

- **`allowPublicSharing`**: off by default. When off, `onedrive_create_sharing_link`
  refuses `scope="anonymous"` links, and `onedrive_invite` refuses invites to
  emails whose local-part reads as a distribution/org-wide alias (e.g.
  `all@contoso.com`). Grants to individually named people are never gated.
- **`allowHardDelete`**: off by default. When off, `onedrive_permanently_delete`
  is refused; `onedrive_trash` / `onedrive_restore` are always available as
  the recoverable alternative.

## Tools

- **Read:** `onedrive_list_items` (a folder's children), `onedrive_search`,
  `onedrive_get_metadata`, `onedrive_download` (inline base64 ≤ 1 MiB, else a
  `downloadUrl`).
- **Write:** `onedrive_upload` (simple PUT ≤ 4 MB, else a chunked resumable
  session), `onedrive_create_folder`, `onedrive_copy`, `onedrive_move`,
  `onedrive_rename`.
- **Trash / restore / delete (write):** `onedrive_trash`, `onedrive_restore`,
  `onedrive_permanently_delete` (gated by `allowHardDelete`).
- **Sharing (write):** `onedrive_create_sharing_link` (gated by
  `allowPublicSharing` for anonymous links), `onedrive_list_permissions`
  (read-only), `onedrive_invite` (gated by `allowPublicSharing` for org-wide
  aliases), `onedrive_delete_permission`.
- **Diagnostics:** `onedrive_check_connection` verifies that granted OAuth
  scopes cover the configured access tier.

## Setup

Authenticate with either an **Entra app** (`microsoft.tenantId` /
`microsoft.clientId` / `microsoft.clientSecret` / `microsoft.userPrincipalName`,
client-credentials flow) or **user OAuth** (click sign-in to populate
`microsoft.userToken`). See `microsoft-oauth.md` for the Entra app / consent
setup shared by every Microsoft 365 tool service. The app must be granted the
Graph `Files.Read` or `Files.ReadWrite` permission (application or delegated,
matching the auth mode) with admin consent.

## Limits

- `onedrive_copy` runs asynchronously on Graph's side; the tool returns once
  the copy is accepted, not once it completes.
- `onedrive_upload` chunks large files at 5 MiB per PUT to the resumable
  upload session, per Graph's recommended chunk size.
- Rate limits are per Entra app / tenant; the node retries `429`/`5xx` with
  exponential backoff.

## Examples

An agent uploads a report, then shares it with a named colleague:

```
onedrive_upload  { "path": "Reports/q3.pdf", "content_base64": "..." }
onedrive_invite  { "item": "Reports/q3.pdf", "emails": ["alex@contoso.com"],
                    "role": "read" }
```

## Upstream docs

- Microsoft Graph OneDrive API: https://learn.microsoft.com/en-us/graph/api/resources/onedrive
- DriveItem: https://learn.microsoft.com/en-us/graph/api/resources/driveitem
- Upload large files: https://learn.microsoft.com/en-us/graph/api/driveitem-createuploadsession
- Sharing: https://learn.microsoft.com/en-us/graph/api/driveitem-createlink

## Troubleshooting

- **Scope / 403 errors:** call `onedrive_check_connection`; if scopes are
  missing, disconnect and reconnect the Microsoft account (user auth) or
  grant/consent the Entra app permission (service auth) at the required tier.
- **`access` is read-only:** write tools raise `MicrosoftAccessError`; raise
  `onedrive.access` to `write`.
- **Sharing/invite refused:** anonymous links and org-wide alias invites need
  `onedrive.allowPublicSharing` enabled; permanent delete needs
  `onedrive.allowHardDelete` enabled.
- **Item not found:** confirm whether the caller meant a path or an item id —
  a value without `/` is always treated as an item id, never a bare
  root-level filename.

<!-- ROCKETRIDE:GENERATED:PARAMS START -->
<!-- ROCKETRIDE:GENERATED:PARAMS END -->
