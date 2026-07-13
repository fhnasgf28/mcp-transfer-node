# Standalone PMT MVP

## Purpose

Standalone PMT adds a central task-orchestration layer to MCP Transfer Node. One PMT website/API can coordinate multiple OpenClaw installations without allowing two agents to silently own the same task.

The MVP is deliberately small:

- authenticated Web dashboard, responsive task detail, Agent Control Center, Sheet Sync Center, Approval Center, editing, acceptance checklist, evidence, and activity timeline
- versioned agent REST API
- remote stdio MCP adapter for each OpenClaw server
- SQLite persistence with WAL mode
- atomic task claim, fenced run token, bounded lease, heartbeat, idempotency, expiry reconciliation, and audit events
- durable interval schedules with worker leases
- a bounded Google Sheet `To-Do` importer
- bounded, deterministic, read-only Google Docs multi-tab task context snapshots
- immutable approval requests, named human decisions, fenced execution attempts, idempotency, expiry recovery, and approval audit events

It does **not** push branches, create merge requests, retry pipelines, send chat messages, deploy, or write task status back to Google Sheet. Sprint 2B models and gates those actions, but deliberately ships no mutating connector.

## Architecture

```text
                         HTTPS
 OpenClaw server A ── MCP adapter ──┐
                                    │
 OpenClaw server B ── MCP adapter ──┼── PMT FastAPI ── SQLite/WAL
                                    │       │
 Human browser ─────────────────────┘       ├── Task/Approval dashboards
                                            ├── Approval execution queue
                                            └── Schedule worker
                                                   │
                                                   └── Google Sheet CSV (read only)
```

The REST API is the cross-server contract. The MCP process is a thin authenticated client and does not need shared filesystem or database access.

## Task lifecycle

```text
todo -> claimed -> in_progress -> ready_for_review
                    |      |
                    |      +-> blocked -> in_progress
                    +-> todo (release)
```

`done` and `cancelled` are supported in storage/API, but deployments should reserve them for human approval or a policy-controlled automation.

### Claim guarantees

`POST /api/v1/pmt/tasks/{key}/claim` runs in a SQLite `BEGIN IMMEDIATE` transaction.

- one active owner per task
- claim lease of 60–7,200 seconds
- idempotency key makes retries safe
- another agent receives `409 CLAIM_CONFLICT`
- an expired lease may be reclaimed
- the claim response includes `current_run_id`, which is required for heartbeat and transition calls
- a stale run token cannot mutate a task after it has been reclaimed
- heartbeat extends only the active fenced run

For the current HMX workflow, use a 30–60 minute lease and heartbeat during long-running verification.

## Authentication model

The MVP reuses `config/peers.json` identities. Each OpenClaw server must have a unique peer entry and token.

Example central `peers.json`:

```json
{
  "allowedPeers": [
    {
      "name": "openclaw-server-a",
      "tokenHash": "<sha256-token-hash>",
      "enabled": true,
      "scopes": [
        "pmt.context.read",
        "pmt.report.read",
        "pmt.report.generate",
        "pmt.report.revise"
      ]
    },
    {
      "name": "openclaw-server-b",
      "tokenHash": "<sha256-token-hash>",
      "enabled": true
    }
  ]
}
```

Rules:

- use a different random token for every agent
- keep raw tokens only in each agent's local secret environment
- serve the PMT API over HTTPS; plain HTTP is accepted only for localhost by the MCP adapter
- never commit raw tokens, cookies, session secrets, or Sheet credentials
- rotate a compromised peer token independently
- put the central service behind a firewall, VPN, Cloudflare Access, or an IP allow-list

The REST API prevents identity spoofing: request body `agent_id` must match `X-PMT-Agent` and the bearer-token peer name. Approval execution additionally requires both a registered active-agent capability and a configured peer scope: `approval.execute` or `approval.execute:<action_type>`. An agent cannot self-grant this authority through registration.

Approval execution, Google Docs context, and internal-status report scopes are enforced now. Broader endpoint-level RBAC and short-lived service tokens remain planned hardening items, so peer credentials should still be granted only to trusted OpenClaw instances.

Google Docs context is separately fail-closed. `pmt.context.read` permits snapshot reads; `pmt.context.refresh` permits attach/refresh/remove only when the authenticated agent also owns the active fenced task run. Existing peers without these explicit scopes gain no context authority.

Internal-status reporting is also fail-closed. Report authors normally receive `pmt.report.read`, `pmt.report.generate`, and `pmt.report.revise`. Put `pmt.report.approve` on a separate reviewer peer and `pmt.report.send` on a delivery-recorder peer. Approve/send are intentionally absent from the ordinary author profile and remain separate from `approval.execute*` scopes.

## Run the central service

The existing web service now includes PMT routes:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
# Fill local values; do not commit .env.
set -a; . ./.env; set +a
mcp-transfer-serve
```

Open:

- Task dashboard: `http://127.0.0.1:8787/pmt`
- Agent Control Center: `http://127.0.0.1:8787/pmt/agents`
- Read-only Sheet Sync Center: `http://127.0.0.1:8787/pmt/sync`
- Approval Center: `http://127.0.0.1:8787/pmt/approvals`
- REST API prefix: `/api/v1/pmt`
- Existing transfer functions remain available.

Runtime PMT database:

```text
$MCP_TRANSFER_BASE_DIR/pmt/pmt.sqlite3
```

Use `mcp-pmt-backup` while the service is running. It uses SQLite's online backup API, runs `PRAGMA integrity_check`, writes a SHA-256 checksum, atomically publishes the backup, and rotates bounded retention. Do not copy only the main database file while WAL mode is active.

### Persistent user service

Hardened user-service templates are available under `deploy/systemd/`:

- `standalone-pmt.service`
- `standalone-pmt-worker.service` and `.timer`
- `standalone-pmt-backup.service` and `.timer`

They bind the existing app configuration, use `UMask=0077`, restrict writable paths to `%h/.local/share/standalone-pmt` and the default `%h/mcp-transfer`, poll durable jobs centrally, and create a verified daily backup. If `MCP_TRANSFER_BASE_DIR` points elsewhere, add that exact directory to `ReadWritePaths` before installation. Inspect existing user services first and install these as separate units; do not replace an existing Transfer Node service.

## Configure each OpenClaw MCP client

Install this repository on each OpenClaw server, then configure only local secret references:

```dotenv
MCP_PMT_API_URL=https://pmt.example.com
MCP_PMT_API_TOKEN=<unique-token-for-this-agent>
MCP_PMT_AGENT_ID=openclaw-server-a
```

Generic stdio MCP entry:

```json
{
  "mcpServers": {
    "standalone-pmt": {
      "command": "/opt/mcp-transfer-node/.venv/bin/mcp-pmt-mcp",
      "env": {
        "MCP_PMT_API_URL": "https://pmt.example.com",
        "MCP_PMT_API_TOKEN": "${MCP_PMT_API_TOKEN}",
        "MCP_PMT_AGENT_ID": "openclaw-server-a"
      }
    }
  }
}
```

Prefer the platform's secret store or environment-file support instead of embedding the raw token in JSON.

### MCP tools

Read/context:

- `pmt_get_available_tasks`
- `pmt_get_my_tasks`
- `pmt_get_task`
- `pmt_get_task_context`
- `pmt_list_task_context`
- `pmt_get_context_document`
- `pmt_get_agents`
- `pmt_agent_heartbeat`
- `pmt_get_schedules`
- `pmt_get_schedule_runs`
- `pmt_get_internal_status_draft`
- `pmt_list_internal_status_drafts`

Task writes:

- `pmt_create_task`
- `pmt_update_task`
- `pmt_add_acceptance_criterion`
- `pmt_toggle_acceptance_criterion`
- `pmt_add_evidence`
- `pmt_register_agent`
- `pmt_claim_task`
- `pmt_task_heartbeat`
- `pmt_start_task`
- `pmt_update_progress`
- `pmt_report_blocker`
- `pmt_submit_for_review`
- `pmt_release_task`
- `pmt_attach_google_doc_context`
- `pmt_refresh_google_doc_context`
- `pmt_remove_google_doc_context`

Internal-status authoring (`pmt.report.generate` / `pmt.report.revise`):

- `pmt_generate_internal_status_draft`
- `pmt_revise_internal_status_draft`

Privileged internal-status lifecycle:

- `pmt_approve_internal_status_draft` (`pmt.report.approve`)
- `pmt_mark_internal_status_sent` (`pmt.report.send`; record only, never sends chat)

Schedule writes:

- `pmt_create_schedule`
- `pmt_claim_due_schedule`
- `pmt_finish_schedule`

Approval workflow:

- `pmt_request_approval`
- `pmt_get_approvals`
- `pmt_get_approval`
- `pmt_claim_approved_action`
- `pmt_approval_heartbeat`
- `pmt_finish_approved_action`

Task-detail mutations require both the active `run_id` fencing token and the caller-observed `expected_version`. The MCP context pack includes durable approval records instead of informational booleans.

## REST API examples

Headers for every PMT API request:

```text
Authorization: Bearer <agent-token>
X-PMT-Agent: openclaw-server-a
```

Create task:

```bash
curl --fail-with-body https://pmt.example.com/api/v1/pmt/tasks \
  -H "Authorization: Bearer $MCP_PMT_API_TOKEN" \
  -H "X-PMT-Agent: $MCP_PMT_AGENT_ID" \
  -H 'Content-Type: application/json' \
  -d '{
    "title":"Fix Employee access right",
    "project":"HMX",
    "module":"core_hr",
    "menu":"Employee",
    "assignee":"Farhan",
    "priority":"high",
    "target_branch":"Human-Resources",
    "required_checks":["access-matrix","fresh-db-test","prepush-quality"]
  }'
```

Claim task:

```bash
curl --fail-with-body https://pmt.example.com/api/v1/pmt/tasks/PMT-0001/claim \
  -H "Authorization: Bearer $MCP_PMT_API_TOKEN" \
  -H "X-PMT-Agent: $MCP_PMT_AGENT_ID" \
  -H 'Content-Type: application/json' \
  -d '{
    "agent_id":"openclaw-server-a",
    "idempotency_key":"PMT-0001-run-20260712T153000",
    "lease_seconds":1800
  }'
```

Heartbeat:

```bash
curl --fail-with-body https://pmt.example.com/api/v1/pmt/tasks/PMT-0001/heartbeat \
  -H "Authorization: Bearer $MCP_PMT_API_TOKEN" \
  -H "X-PMT-Agent: $MCP_PMT_AGENT_ID" \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"openclaw-server-a","run_id":"<current_run_id-from-claim>","lease_seconds":1800}'
```

## Approval Center

External actions use a queue that is separate from schedules:

```text
pending -> approved -> executing -> succeeded
   |          |            |
   +-> rejected/cancelled   +-> failed -> executing (new attempt)
              approved/executing -> expired
```

Safety invariants:

- the request stores a canonical immutable payload and `payload_sha256`
- typed payload schemas reject unexpected fields, secret-like keys, non-finite JSON, excessive nesting, and oversized data
- task-linked agent requests require the active task owner and its fenced `task_run_id`
- request creation is idempotent; reusing the key with different content is rejected
- the requester cannot approve their own request
- browser decisions require a named session principal, CSRF token, caller-observed approval version, and exact typed approval key
- approval has a bounded validity period after the human decision
- execution requires an enabled scoped peer plus a registered active agent capability
- execution claim creates a distinct run ID, lease, heartbeat, deterministic provider key, and audit event
- finish is fenced and safely replayable only with the same final status and canonical result
- failed attempts may retry with a new execution idempotency key while approval is valid
- timed-out attempts are closed and cannot reuse their old idempotency key
- approval, execution-run, and task-linked events are retained in SQLite

Supported request action types are `sheet_writeback`, `git_push`, `gitlab_merge_request`, `gitlab_pipeline_retry`, `chat_message`, and `deployment`. They are authorization envelopes, not connector implementations. The central worker only reconciles leases; it does not execute an external action.

Example agent request:

```json
{
  "action_type": "git_push",
  "title": "Push reviewed HMX branch",
  "reason": "Fresh-DB tests and pre-push quality passed",
  "idempotency_key": "PMT-0001-git-push-abc1234",
  "task_ref": "PMT-0001",
  "task_run_id": "<active-task-run-id>",
  "payload": {
    "repository": "hmx-002",
    "remote": "origin",
    "source_branch": "feat/example",
    "target_branch": "Human-Resources",
    "commit_sha": "abc1234"
  }
}
```

The human reviews the exact payload at `/pmt/approvals`, types the displayed `APR-xxxx` key, and approves or rejects it. Approval does not itself perform the action.

### Threat model and explicit non-goals

The design addresses duplicate agents, stale task owners, concurrent human decisions, stale executors, request replay, accidental payload changes, CSRF on approval decisions, self-approved requests, self-granted execution capability, secret material in approval JSON, and schedule-based bypass of the approval queue.

The current release does not claim to protect against a compromised PMT host/database administrator, a stolen human password/session, a stolen already-scoped peer token, or a malicious external provider. It does not provide OIDC, multi-role project authorization, cryptographic append-only audit storage, credential brokerage, connector-side rollback, or exactly-once guarantees from external providers. Executors must use `provider_key` as the provider-side idempotency key when a future connector supports one.

## Google Docs task context

An authenticated administrator or an actively owning agent can attach a canonical URL of the form `https://docs.google.com/document/d/<id>/edit?tab=<tab-id>`. PMT fetches `GET https://docs.googleapis.com/v1/documents/<id>?includeTabsContent=true` with the sole OAuth scope `https://www.googleapis.com/auth/documents.readonly`. It never uses `google-api-python-client`, write scopes, arbitrary hosts, redirects, HTML export scraping, or implicit background refresh.

Server-owner configuration:

```dotenv
MCP_PMT_GOOGLE_DOCS_SERVICE_ACCOUNT_FILE=/absolute/owner-only/service-account.json
MCP_PMT_GOOGLE_DOCS_TIMEOUT_SECONDS=30
```

The credential must be an owner-owned regular file with no group/world permission bits. PMT opens it with `O_NOFOLLOW`, verifies the opened descriptor, rejects group/world-writable or symlinked parent directories, caps credential size, and pins `token_uri` to `https://oauth2.googleapis.com/token`. Its path and contents are never returned by API/UI or written to events. If the setting/file is absent or unsafe, Google Docs attach/refresh fails closed. The Google Cloud project must have `docs.googleapis.com` enabled and the service account must be able to read the document. A `403 SERVICE_DISABLED` means the Google Docs API still needs enabling (or propagation time); it is not evidence that PMT needs broader OAuth permissions.

Snapshots preserve deterministic depth-first tab/subtab hierarchy, selected tab, headings, paragraphs, bullets, tables, links, supported inline semantics, headers, footers, footnotes, and non-body tab resources used by semantic hashing. Unknown paragraph/structural elements fail closed instead of silently disappearing. Limits are 100 tabs, 100,000 extracted characters per tab, 500,000 total characters, a 5 MiB API response, bounded nesting, and one 3–60 second end-to-end deadline covering OAuth plus the streamed Docs response. Duplicate tab IDs, inconsistent parents, malformed payloads, non-JSON responses, redirects, userinfo, custom ports, and non-canonical hosts are rejected.

`task_context_documents.context_version` is independent of `tasks.version`. Network I/O occurs outside SQLite. Authorization, active owner, run fencing, and observed task version are checked before fetch and again in the final transaction. Attach preflight avoids duplicate/cap-exceeded network fetches. Changed snapshots atomically replace all tab rows and require the observed context version. An identical-hash retry updates provider revision metadata, `fetched_at`, and `last_checked_at` without incrementing context/task versions, including when its context-version observation became stale. Attach/remove/refresh events contain metadata and hashes only, never document text.

The list/aggregate context API is metadata-only. Single-document reads return one selected or explicit tab page with `offset` and `limit` (maximum 20,000 characters), truncation metadata, and the same untrusted-content boundary. Task Detail renders at most 20,000 context characters per document and 5,000 per tab; complete stored snapshots remain available through explicit tab pagination. Existing peers without `pmt.context.read` retain the legacy `pmt_get_task_context` fields and receive an explicit `externalContextUnavailable` marker instead of losing the whole tool.

For Docker, use the opt-in overlay and prepare a dedicated read-only file owned by the container UID/GID:

```bash
install -o 10001 -g 10001 -m 0600 /safe/source/service-account.json /safe/pmt/google-service-account.json
export MCP_PMT_GOOGLE_DOCS_CREDENTIAL_HOST_FILE=/safe/pmt/google-service-account.json
docker compose -f docker-compose.yml -f deploy/docker-compose.gdocs.yml --profile worker up -d
```

The credential bind mount is read-only at `/run/pmt-secrets/google-service-account.json`. Do not bake credentials into the image or repository.

### Untrusted-content boundary

Every REST/MCP context pack includes a machine-readable `untrusted_external_content` boundary and an explicit text warning. Google Docs text is data/evidence only. It cannot override system/developer/project policy, authorize a tool, approve an external mutation, request command execution, disclose credentials, or bypass the Approval Center. Jinja autoescaping is retained in Task Detail and document text is rendered as plain pre-wrapped text.

Explicit non-goals: editing Google Docs, write scopes, comments/Drive traversal, following links found in documents, executing embedded instructions, automatic scheduler refresh, storing OAuth tokens, or using document content as authorization. API enablement/access remains an operator prerequisite, not something PMT changes automatically.

## Google Sheet schedule

Executable job types are `google_sheet_sync` and `lease_recovery`. Sheet sync accepts a public Google Sheet CSV export URL and imports matching rows as idempotent PMT tasks. Every worker invocation reconciles expired task and approval-execution leases before claiming a schedule, so recovery does not depend on an optional schedule record; the explicit `lease_recovery` type remains available for observability/backward compatibility.

Default filters follow the current HMX workflow:

- developer/assignee: `Farhan`
- Dev Status: `To-Do`

Create the schedule through the API/MCP:

```json
{
  "name": "Import Farhan To-Do tasks",
  "job_type": "google_sheet_sync",
  "interval_seconds": 300,
  "payload": {
    "csv_url": "https://docs.google.com/spreadsheets/d/<id>/export?format=csv&gid=<gid>",
    "assignee": "Farhan",
    "dev_status": "To-Do",
    "project": "HMX",
    "target_branch": "Human-Resources"
  }
}
```

The importer:

- only accepts HTTPS URLs on `docs.google.com`
- rejects redirects, oversized responses, and unexpected content types
- auto-detects a header row within the first 30 CSV rows
- imports only matching task rows
- stores spreadsheet ID + gid + row as a source-aware idempotent identity
- does not write back to the Sheet
- takes one durable, process-shared Sheet sync lease before any fetch; Drive,
  scheduled, and manual entry points cannot overlap fetch/parse/import work
- uses a 60-second whole-operation deadline and a 90-second fenced lease, and
  rechecks the run fence before each task mutation

Run one due schedule:

```bash
MCP_PMT_AGENT_ID=pmt-scheduler mcp-pmt-worker
```

Use one system cron or timer on the **central PMT server** to invoke the worker once per minute. Before installing a system scheduler, inspect and merge with the existing cron/timer configuration. Do not run duplicate central workers unless they use unique IDs; database leases still prevent duplicate ownership.

Docker users can instead enable the `worker` profile. `pmt-worker` uses the same
image, environment, named data volume, and optional read-only credential mount,
exposes no port, and invokes one recovery pass every 60 seconds. That cadence
only checks local due state; it does not poll Sheet content unless a schedule or
durable Drive event is due.

## HMX recommended agent flow

1. `pmt_get_available_tasks`
2. `pmt_get_task_context`
3. `pmt_claim_task` with a unique idempotency key; retain its `current_run_id`
4. HMX code search and repository inspection
5. `pmt_start_task` with the fenced `run_id`
6. heartbeat with the same `run_id` during long tests
7. update progress/blocker
8. run module, pre-push, access, pipeline, and UI evidence checks as required
9. `pmt_submit_for_review`
10. create a typed approval request with `pmt_request_approval`
11. wait for a different named human to approve the immutable payload in Approval Center
12. only a separately scoped executor may claim the approved request; no external connector is enabled in this release

## Known MVP limitations

- SQLite is appropriate for one central PMT process and moderate agent traffic. Move to PostgreSQL before horizontal API scaling.
- Web login uses one configured named admin principal and password; use OIDC, multiple human identities, and RBAC before multi-user/public deployment.
- Peer scope enforcement protects approval execution, Google Docs context, and internal-status reporting; broader task endpoint scopes remain future work.
- Schedules are interval-based, not full cron expressions.
- Sheet identity includes spreadsheet ID, gid, and row number. A stable source UUID column is still recommended before rows are frequently reordered.
- No attachment upload, GitLab write, Sheet write-back, notification send, pipeline retry, or deployment action is implemented.
- Stable Sheet source-row UUIDs are required before enabling controlled write-back; row-number identity is insufficient.
- Task dependencies, evidence verdict automation, and sprint analytics remain follow-up modules.

## Next hardening milestones

1. PostgreSQL migrations and `SELECT ... FOR UPDATE SKIP LOCKED`
2. OIDC login, project RBAC, and broader endpoint-level per-agent scopes
3. stable Sheet source IDs and conflict-aware two-way sync
4. dry-run connector previews and credential brokerage outside approval payloads
5. controlled Sheet/GitLab/chat executors with provider idempotency and explicit enable switches
6. structured evidence and required-check verdicts
7. GitLab read-only pipeline/MR connector
8. audit export, metrics, backups, retention, webhook signatures, and rate limits

## Event-driven Google Drive Shadow Sync

The Bug Tracker connector can use Google Drive API v3 `files.watch` rather than
polling the Sheet. It is disabled by default and does not enable existing Sheet
poll schedules. The existing manual CSV Shadow Sync remains read-only and
idempotent.

Configuration is shown in `.env.example`. Set the exact spreadsheet ID and its
bounded `docs.google.com/.../export?format=csv` URL, an owner-only service-account
credential, and a random webhook master secret of at least 32 bytes. The public
URL must be an HTTPS origin without userinfo, port, path, query, or fragment. The
callback is fixed to:

`/api/v1/pmt/drive-notifications/bug-tracker`

An explicit callback override is accepted only when it is that exact path on the
same HTTPS origin. The service account OAuth token uses the pinned Google token
endpoint and the least-privilege
`https://www.googleapis.com/auth/drive.metadata.readonly` scope. Drive-triggered
private CSV export uses a separate
`https://www.googleapis.com/auth/spreadsheets.readonly` bearer token, sent only
to the exact validated `https://docs.google.com` export URL. Google normally
returns one bounded `*-sheets.googleusercontent.com` export redirect; PMT
validates that URL against the requested spreadsheet and tab, then fetches it in
a separate request without forwarding Authorization. Additional or non-Google
redirects are rejected. Existing public/manual sync remains unauthenticated and
read-only.

After enabling configuration, no channel is created automatically. An
authenticated administrator must explicitly Register from `/pmt/sync`; this
persists the desired Running state. Stop first persists desired Stopped state,
then disables local channels, so workers cannot recreate or renew them while the
feature environment remains enabled. A later manual Register re-enables renewal.
All mutations require CSRF.
Watch channels cannot renew in place, so renewal creates an overlapping channel,
activates it, then queues/stops the old one. Remote stop failures are persisted
with bounded backoff and retried by worker maintenance. If watch creation returns
enough channel/resource information but local validation or binding fails, PMT
attempts immediate cleanup and persists retry state on failure. If Google never
returns a usable resource ID, its requested provider expiration is the only
remote cleanup fallback. The worker checks local SQLite
due events and channel expiration during its normal recovery invocation. It does
not poll Google Sheet content unless a durable notification event is due.

Google callbacks are unauthenticated at the HTTP layer, but are authenticated by
strict `X-Goog-*` channel metadata and a deterministic per-channel HMAC token.
Only the token SHA-256 hash is persisted. `sync` callbacks are durably
acknowledged without fetching CSV; `update` callbacks for content/properties are
persisted, deduplicated, ordered, and coalesced for five seconds before one
Shadow Sync. A late event received while the debounce runner is active is handled
by that runner's durable follow-up loop. Failed work uses durable bounded backoff
and can be recovered by a later worker invocation after restart. Worker
maintenance prunes terminal notification history and old inactive channel rows
after 30 days while preserving active/retriable state and a bounded recent
channel history.

Operational notes:

- The callback must be publicly reachable over valid HTTPS before registration.
- Google Drive API must be enabled and the service account must be able to read
  the configured file metadata.
- Keep the webhook secret stable across service restarts; changing it invalidates
  callbacks for existing channels, which should then be replaced manually.
- Status and run results deliberately omit channel tokens, credentials, OAuth
  data, resource IDs, and provider response bodies.
