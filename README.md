# MCP Transfer Node + Standalone PMT

FastAPI receiver + Web UI + MCP tools for direct server-to-server transfer via Cloudflare Tunnel. The repository also contains a standalone PMT for coordinating HMX tasks across multiple OpenClaw servers through a central authenticated API, transactional task leases, a Web dashboard, remote MCP tools, durable schedules, read-only Google Sheet task import, read-only multi-tab Google Docs context snapshots, and an auditable Approval Center.

Runtime data lives in `/home/fhnasgf/mcp-transfer/`; transfer inbox is `/home/fhnasgf/mcp-transfer/inbox/`, while PMT state is stored in `/home/fhnasgf/mcp-transfer/pmt/pmt.sqlite3`. All transferred file types are accepted as binary up to 50 MB.

## Install

```bash
cd /home/fhnasgf/mcp-transfer-node
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
```

## Runtime config

Copy `examples/peers.json` and `examples/destinations.json` into `/home/fhnasgf/mcp-transfer/config/`. Set env vars shown in `.env.example`. `MCP_TRANSFER_HOME_ALLOWLIST_PREFIX` defaults to `/home/fhnasgf`; override it only for a different deployment home, and keep `MCP_TRANSFER_BASE_DIR` under that prefix. Expose `http://127.0.0.1:8787` through Cloudflare Tunnel to a subdomain such as `server-a.clipperyt.online`.

## Run

```bash
mcp-transfer-serve
```

MCP transfer command: `/home/fhnasgf/mcp-transfer-node/.venv/bin/mcp-transfer-mcp`.

## Standalone PMT MVP

After login, open `/pmt` for tasks, `/pmt/agents` for heartbeat/capability/lease visibility, `/pmt/sync` for read-only Google Sheet scheduler observability, `/pmt/internal-status` for versioned PMT-backed report drafts, and `/pmt/approvals` for human approval decisions and execution-attempt history. Agent API routes live under `/api/v1/pmt` and use the peer bearer-token registry with `X-PMT-Agent` identity binding.

Install the remote PMT MCP adapter on each OpenClaw server:

```bash
export MCP_PMT_API_URL=https://pmt.example.com
export MCP_PMT_API_TOKEN='<local-secret>'
export MCP_PMT_AGENT_ID=openclaw-server-a
mcp-pmt-mcp
```

The central schedule worker executes one due job per invocation:

```bash
MCP_PMT_AGENT_ID=pmt-scheduler mcp-pmt-worker
```

Executable schedule types are `google_sheet_sync`, `lease_recovery`, and draft-only `internal_status_generate`. Google Sheet sync rejects redirects and non-Google targets, uses source-aware idempotency, defaults to Farhan tasks with `Dev Status = To-Do`, and never writes back.

External mutations are represented by immutable approval requests for Sheet write-back, Git push/MR, pipeline retry, chat message, or deployment. A named human must approve an agent request before a separately fenced executor can claim it. This release provides the queue, UI, API, MCP contract, leases, idempotency, and audit trail only: it intentionally contains **no connector that performs those external mutations**. Approval-executor peers must also have `approval.execute` or an action-specific scope in `peers.json`.

Google Docs context is disabled unless the owner configures an absolute, owner-only service-account file. It uses only `documents.readonly`, the fixed `docs.googleapis.com` endpoint, and explicit `pmt.context.read` / `pmt.context.refresh` peer scopes. Document content is always marked untrusted evidence and cannot authorize tools or commands. See the full guide for API enablement and threat-model details.

Create a verified online SQLite backup:

```bash
mcp-pmt-backup --retention 14
```

Hardened user service, worker timer, and backup timer templates are under `deploy/systemd/`.

Full architecture, API examples, multi-server setup, security rules, scheduler guidance, HMX workflow, limitations, and roadmap:

- [`docs/standalone-pmt.md`](docs/standalone-pmt.md)
- [`docs/internal-status-migration.md`](docs/internal-status-migration.md)
