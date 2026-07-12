from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from mcp_transfer_node.config import load_settings
from mcp_transfer_node.pmt_drive import (
    register_drive_watch,
    retry_drive_channel_cleanups,
    run_due_drive_events,
)
from mcp_transfer_node.pmt_sheet import SheetSyncBusy, sync_google_sheet
from mcp_transfer_node.pmt_store import PmtStore


async def run_once(worker_id: str) -> dict[str, object]:
    settings = load_settings()
    store = PmtStore(settings.pmt_db_path)
    store.initialize()
    maintenance = {
        "tasks": store.reconcile_expired_leases(actor=worker_id),
        "approvals": store.reconcile_expired_approval_leases(actor=worker_id),
        "drive_history": store.prune_drive_history(),
    }
    drive: dict[str, object] = {"status": "disabled"}
    if settings.pmt_drive_watch_enabled:
        maintenance["drive_cleanup"] = await retry_drive_channel_cleanups(store, settings)
        drive = await run_due_drive_events(store, settings, worker_id=f"{worker_id}:drive")
        if (
            store.drive_watch_desired(settings.pmt_drive_spreadsheet_id)
            and store.due_drive_renewal(settings.pmt_drive_spreadsheet_id)
            and store.drive_renewal_retry_due(settings.pmt_drive_spreadsheet_id)
        ):
            try:
                registration = await register_drive_watch(store, settings)
                drive["renewal"] = {
                    "status": "succeeded",
                    "expiration_at": registration["expiration_at"],
                    "replaced": registration["replaced"],
                }
            except Exception as exc:
                drive["renewal"] = {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": "Drive watch renewal failed",
                }
    schedule = store.claim_due_schedule(worker_id)
    if schedule is None:
        return {
            "status": "idle",
            "message": "no due schedule",
            "maintenance": maintenance,
            "drive": drive,
        }

    run_id = str(schedule["run_id"])
    try:
        if schedule["job_type"] == "google_sheet_sync":
            result = await sync_google_sheet(store, schedule["payload"], actor=worker_id)
        elif schedule["job_type"] == "lease_recovery":
            result = maintenance
        elif schedule["job_type"] == "internal_status_generate":
            payload = schedule["payload"]
            report = store.generate_internal_status_report(
                owner=payload["owner"],
                report_date=payload.get("report_date"),
                period=payload["period"],
                timezone_name=payload.get("timezone", "Asia/Jakarta"),
                actor=worker_id,
            )
            result = {
                "report_id": report["id"],
                "owner": report["owner"],
                "report_date": report["report_date"],
                "period": report["period"],
                "report_version": report["report_version"],
                "state": report["state"],
            }
        else:
            result = {"reason": f"unsupported job type: {schedule['job_type']}"}
            store.finish_schedule_run(schedule["id"], run_id, worker_id, "skipped", result)
            return {"status": "skipped", "schedule": schedule["id"], "result": result}
    except SheetSyncBusy:
        safe_result = {"message": "Google Sheet sync is already in progress"}
        store.finish_schedule_run(schedule["id"], run_id, worker_id, "skipped", safe_result)
        return {"status": "skipped", "schedule": schedule["id"], "result": safe_result}
    except Exception as exc:
        safe_result = {"error_type": type(exc).__name__, "message": "Schedule execution failed"}
        store.finish_schedule_run(schedule["id"], run_id, worker_id, "failed", safe_result)
        return {"status": "failed", "schedule": schedule["id"], "result": safe_result}

    store.finish_schedule_run(schedule["id"], run_id, worker_id, "succeeded", result)
    return {"status": "succeeded", "schedule": schedule["id"], "result": result}


def run() -> None:
    parser = argparse.ArgumentParser(description="Run one due standalone PMT schedule")
    parser.add_argument("--worker-id", default=os.environ.get("MCP_PMT_AGENT_ID", ""))
    args = parser.parse_args()
    if not args.worker_id:
        parser.error("--worker-id or MCP_PMT_AGENT_ID is required")
    result = asyncio.run(run_once(args.worker_id))
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] == "failed":
        sys.exit(1)


if __name__ == "__main__":
    run()
