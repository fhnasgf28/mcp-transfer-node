# PMT-managed Internal Status migration

## Ownership boundary

PMT is the durable primary source for internal-status reports about PMT tasks. It derives report items from PMT task lifecycle events and evidence, then stores immutable/versioned daily snapshots. The PMT MCP and REST API expose those snapshots to agents and the authenticated PMT web UI. No report operation sends a message or calls HashMicro Chat.

The existing `hmx-internal-status` MCP remains unchanged. Google Sheet marker columns remain a fallback for legacy tasks that have not migrated into PMT. This implementation neither reads nor writes the live Sheet.

## Hybrid migration and deduplication

1. Ingest a legacy task into PMT with its stable provider name in `tasks.source` and stable Sheet/provider identifier in `tasks.external_id`.
2. Use the PMT task ID/key as the durable internal link. The existing unique `(source, external_id)` index prevents duplicate PMT tasks.
3. Prefer the PMT report item whenever the same external source ID/task link appears in both PMT and a legacy Sheet draft.
4. Include Sheet-only items through the legacy `hmx-internal-status` fallback until they have a PMT task link. Do not copy Sheet marker history into report snapshots without a controlled migration.
5. Keep delivery separate: copy the approved PMT `rendered_text` to the configured delivery adapter, then call PMT `mark-sent` only after delivery is confirmed. `mark-sent` records state; it does not send.

## Report semantics

- Default timezone: `Asia/Jakarta`; REST/MCP accepts an IANA timezone and explicit `YYYY-MM-DD` report date.
- Morning: `Done kemarin`, `Plan hari ini`, `On progress`, `Blocker`. Morning never includes merge requests.
- Evening: `Done hari ini`, `On progress`, `Blocker`, and optional `Create Merge Request` from report-date PMT evidence/task events.
- Done items come from durable `task.done` event timestamps within the local report-date window.
- Plan includes only To-Do tasks created or newly assigned to the owner on the report date. Older To-Do items require an explicit task-linked `plan` inclusion override and are marked `carry_over` in structured data.
- Reports are bounded to 25 items per section, 50 override entries, 500 characters per display note, and 20,000 rendered characters.

## Snapshot lifecycle

A snapshot is uniquely identified by `(owner, report_date, period, report_version)`.

- Generate without `regenerate` is idempotent and returns the latest version.
- Explicit regeneration creates a new draft version and preserves prior approved/sent snapshots.
- Revision requires `expected_version`, accepts bounded task-linked include/exclude overrides, and creates a new version. It cannot revise an approved/sent latest snapshot.
- Approval requires the latest draft version.
- Mark-sent requires the latest approved version and is idempotent.
- Approval/sent state transitions do not modify `rendered_text`, sections, overrides, or report version.

## Least-privilege scopes

Report permissions are separate from ordinary task read/write and approval-execution scopes:

- `pmt.report.read`: get/list snapshots
- `pmt.report.generate`: generate/regenerate drafts
- `pmt.report.revise`: create a revised draft version
- `pmt.report.approve`: approve a draft
- `pmt.report.send`: record a confirmed sent state

Do not grant approve/send scopes to ordinary task-read agents. Schedule creation and worker execution continue to use the existing PMT schedule/auth boundary; no production schedule is created by this change.

## Optional schedule type

`internal_status_generate` is an available durable schedule job type. Payload:

```json
{
  "owner": "Farhan",
  "period": "morning",
  "timezone": "Asia/Jakarta"
}
```

An optional fixed `report_date` may be supplied for controlled replay/testing. Without it, the worker resolves the local date at execution time. The job only generates/gets a draft snapshot and never approves, sends, or mutates external providers.
