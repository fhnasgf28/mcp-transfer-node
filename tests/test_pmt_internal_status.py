from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from mcp_transfer_node import pmt_mcp_server, pmt_worker
from mcp_transfer_node.app import create_app
from mcp_transfer_node.auth import hash_token
from mcp_transfer_node.pmt_reports import parse_report_date, render_report
from mcp_transfer_node.pmt_store import PmtStore, TaskInput


def _set_event_time(store: PmtStore, task_id: str, event_type: str, created_at: str) -> None:
    with store._connect() as db:
        db.execute(
            "UPDATE task_events SET created_at=? WHERE task_id=? AND event_type=?",
            (created_at, task_id, event_type),
        )
        db.commit()


def _create(store: PmtStore, title: str, *, status: str = "todo") -> dict:
    task = store.create_task(TaskInput(title=title, assignee="Farhan"))
    if status != "todo":
        task = store.admin_transition_task(task["task_key"], status, "admin")
    return task


def test_report_date_timezone_todo_done_sections_and_mr_evening_only(settings):
    store = PmtStore(settings.pmt_db_path)
    store.initialize()
    new_task = _create(store, "New task")
    old_task = _create(store, "Old task")
    done_yesterday = _create(store, "Done yesterday", status="done")
    done_today = _create(store, "Done today", status="done")
    active = _create(store, "Active", status="inbox")
    # Admin cannot put an unclaimed task in progress, so use durable status fixtures directly.
    blocked = _create(store, "Blocked", status="blocked")
    with store._connect() as db:
        db.execute("UPDATE tasks SET status='in_progress' WHERE id=?", (active["id"],))
        db.execute(
            "UPDATE tasks SET created_at=? WHERE id=?",
            ("2026-07-01T00:00:00+00:00", old_task["id"]),
        )
        db.execute(
            "UPDATE task_events SET created_at=? WHERE task_id=? AND event_type='task.created'",
            ("2026-07-01T00:00:00+00:00", old_task["id"]),
        )
        db.execute("UPDATE task_evidence SET created_at=? WHERE 0", ("2026-07-13T03:00:00+00:00",))
        db.commit()
    # Jakarta day boundaries: 17:30 UTC is already the next local day.
    _set_event_time(store, new_task["id"], "task.created", "2026-07-12T17:30:00+00:00")
    _set_event_time(store, done_yesterday["id"], "task.done", "2026-07-12T03:00:00+00:00")
    _set_event_time(store, done_today["id"], "task.done", "2026-07-13T03:00:00+00:00")
    store.add_evidence(
        done_today["task_key"],
        evidence_type="merge_request",
        label="MR for done task",
        url="https://gitlab.example/mr/1",
        note="",
        actor="admin",
    )
    with store._connect() as db:
        db.execute(
            "UPDATE task_evidence SET created_at=? WHERE task_id=?",
            ("2026-07-13T04:00:00+00:00", done_today["id"]),
        )
        db.commit()

    morning = store.generate_internal_status_report(
        owner="Farhan",
        report_date="2026-07-13",
        period="morning",
        actor="test",
    )
    assert [item["task_key"] for item in morning["sections"]["plan"]] == [new_task["task_key"]]
    assert old_task["task_key"] not in morning["rendered_text"]
    assert done_yesterday["task_key"] in morning["rendered_text"]
    assert done_today["task_key"] not in [item["task_key"] for item in morning["sections"]["done"]]
    assert active["task_key"] in morning["rendered_text"]
    assert blocked["task_key"] in morning["rendered_text"]
    assert "Create Merge Request" not in morning["rendered_text"]

    evening = store.generate_internal_status_report(
        owner="Farhan",
        report_date="2026-07-13",
        period="evening",
        actor="test",
    )
    assert done_today["task_key"] in evening["rendered_text"]
    assert "Create Merge Request" in evening["rendered_text"]
    assert "https://gitlab.example/mr/1" in evening["rendered_text"]
    assert evening["sections"]["done"][0]["source_event_id"]
    assert evening["sections"]["merge_requests"][0]["source_evidence_id"]


def test_report_versioning_overrides_immutability_and_idempotent_sent(settings):
    store = PmtStore(settings.pmt_db_path)
    store.initialize()
    old_task = _create(store, "Carry over")
    with store._connect() as db:
        db.execute(
            "UPDATE tasks SET created_at=? WHERE id=?",
            ("2026-07-01T00:00:00+00:00", old_task["id"]),
        )
        db.execute(
            "UPDATE task_events SET created_at=? WHERE task_id=?",
            ("2026-07-01T00:00:00+00:00", old_task["id"]),
        )
        db.commit()
    first = store.generate_internal_status_report(
        owner="Farhan", report_date="2026-07-13", period="morning", actor="test"
    )
    assert (
        store.generate_internal_status_report(
            owner="Farhan", report_date="2026-07-13", period="morning", actor="test"
        )["id"]
        == first["id"]
    )
    revised = store.revise_internal_status_report(
        owner="Farhan",
        report_date="2026-07-13",
        period="morning",
        expected_version=1,
        overrides={
            "include": [
                {"section": "plan", "task_ref": old_task["task_key"], "note": "Carry today"}
            ],
            "exclude": [],
        },
        actor="editor",
    )
    assert revised["report_version"] == 2
    assert revised["sections"]["plan"][0]["carry_over"] is True
    assert (
        store.get_internal_status_report("Farhan", "2026-07-13", "morning", 1)["id"] == first["id"]
    )
    approved = store.transition_internal_status_report(
        owner="Farhan",
        report_date="2026-07-13",
        period="morning",
        expected_version=2,
        target_state="approved",
        actor="approver",
    )
    with pytest.raises(PermissionError):
        store.revise_internal_status_report(
            owner="Farhan",
            report_date="2026-07-13",
            period="morning",
            expected_version=2,
            overrides={"include": [], "exclude": []},
            actor="editor",
        )
    sent = store.transition_internal_status_report(
        owner="Farhan",
        report_date="2026-07-13",
        period="morning",
        expected_version=2,
        target_state="sent",
        actor="sender",
    )
    assert sent["state"] == "sent"
    assert (
        store.transition_internal_status_report(
            owner="Farhan",
            report_date="2026-07-13",
            period="morning",
            expected_version=2,
            target_state="sent",
            actor="sender",
        )["sent_at"]
        == sent["sent_at"]
    )
    assert approved["rendered_text"] == sent["rendered_text"]


def test_regeneration_requires_expected_version_and_preserves_approved_history(settings):
    store = PmtStore(settings.pmt_db_path)
    store.initialize()
    first = store.generate_internal_status_report(
        owner="Farhan", report_date="2026-07-13", period="morning", actor="generator"
    )
    with pytest.raises(PermissionError, match="expected_version"):
        store.generate_internal_status_report(
            owner="Farhan",
            report_date="2026-07-13",
            period="morning",
            actor="generator",
            regenerate=True,
        )
    with pytest.raises(PermissionError):
        store.generate_internal_status_report(
            owner="Farhan",
            report_date="2026-07-13",
            period="morning",
            actor="generator",
            regenerate=True,
            expected_version=99,
        )
    idempotent = store.generate_internal_status_report(
        owner="Farhan",
        report_date="2026-07-13",
        period="morning",
        actor="generator",
        expected_version=99,
    )
    assert idempotent["id"] == first["id"]
    approved = store.transition_internal_status_report(
        owner="Farhan",
        report_date="2026-07-13",
        period="morning",
        expected_version=1,
        target_state="approved",
        actor="approver",
    )
    regenerated = store.generate_internal_status_report(
        owner="Farhan",
        report_date="2026-07-13",
        period="morning",
        actor="regenerator",
        regenerate=True,
        expected_version=1,
    )
    assert regenerated["report_version"] == 2
    assert regenerated["state"] == "draft"
    assert regenerated["generated_by"] == "regenerator"
    assert (
        store.get_internal_status_report("Farhan", "2026-07-13", "morning", 1)["id"] == first["id"]
    )
    assert (
        store.get_internal_status_report("Farhan", "2026-07-13", "morning", 1)["state"]
        == approved["state"]
    )


def test_concurrent_report_regeneration_allows_only_one_expected_version_writer(settings):
    store = PmtStore(settings.pmt_db_path)
    store.initialize()
    store.generate_internal_status_report(
        owner="Farhan", report_date="2026-07-13", period="morning", actor="generator"
    )
    barrier = threading.Barrier(2)
    results: list[str] = []

    def regenerate(actor: str) -> None:
        barrier.wait()
        try:
            store.generate_internal_status_report(
                owner="Farhan",
                report_date="2026-07-13",
                period="morning",
                actor=actor,
                regenerate=True,
                expected_version=1,
            )
        except PermissionError:
            results.append("conflict")
        else:
            results.append("ok")

    threads = [
        threading.Thread(target=regenerate, args=(f"generator-{index}",)) for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["conflict", "ok"]
    assert (
        store.get_internal_status_report("Farhan", "2026-07-13", "morning")["report_version"] == 2
    )


def test_report_migration_metadata_audit_and_compact_listing_are_idempotent(settings):
    settings.pmt_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.pmt_db_path) as db:
        db.execute(
            """CREATE TABLE internal_status_reports (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, report_date TEXT NOT NULL,
                period TEXT NOT NULL, report_version INTEGER NOT NULL, state TEXT NOT NULL,
                timezone TEXT NOT NULL, generated_at TEXT NOT NULL, rendered_text TEXT NOT NULL,
                sections TEXT NOT NULL, overrides TEXT NOT NULL, approved_by TEXT,
                approved_at TEXT, sent_by TEXT, sent_at TEXT, created_at TEXT NOT NULL,
                UNIQUE(owner, report_date, period, report_version)
            )"""
        )
        db.execute(
            """INSERT INTO internal_status_reports
                VALUES ('legacy', 'Farhan', '2026-07-13', 'morning', 1, 'draft',
                        'Asia/Jakarta', '2026-07-13T00:00:00+00:00', 'legacy', '{}', '{}',
                        NULL, NULL, NULL, NULL, '2026-07-13T00:00:00+00:00')"""
        )
    store = PmtStore(settings.pmt_db_path)
    store.initialize()
    store.initialize()
    with store._connect() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(internal_status_reports)")}
        assert "generated_by" in columns
    legacy = store.get_internal_status_report("Farhan", "2026-07-13", "morning")
    assert legacy["generated_by"] == ""
    store.generate_internal_status_report(
        owner="Farhan", report_date="2026-07-14", period="morning", actor="a" * 300
    )
    compact = store.list_internal_status_reports(owner=" farHAN ", limit=999)[0]
    assert compact["generated_by"] == "a" * 120
    assert "rendered_text" not in compact
    assert "sections" not in compact
    assert store.list_internal_status_reports(owner="   ") == []
    store.transition_internal_status_report(
        owner="Farhan",
        report_date="2026-07-14",
        period="morning",
        expected_version=1,
        target_state="approved",
        actor="approver",
    )
    approved = store.get_internal_status_report("Farhan", "2026-07-14", "morning")
    assert approved["generated_by"] == "a" * 120


def test_report_date_input_is_strict_iso_but_heading_is_us_short_date():
    with pytest.raises(ValueError):
        parse_report_date("7/13/2026", "Asia/Jakarta")
    parsed = parse_report_date("2026-07-13", "Asia/Jakarta")
    assert "Internal Status - Pagi (7/13/2026)" in render_report("Farhan", parsed, "morning", {})
    assert "Internal Status - Sore (7/13/2026)" in render_report("Farhan", parsed, "evening", {})


def test_web_revision_preserves_prior_overrides_deterministically(settings):
    store = PmtStore(settings.pmt_db_path)
    store.initialize()
    task = _create(store, "Carry over")
    first = store.generate_internal_status_report(
        owner="Farhan", report_date="2026-07-13", period="morning", actor="generator"
    )
    second = store.revise_internal_status_report(
        owner="Farhan",
        report_date="2026-07-13",
        period="morning",
        expected_version=first["report_version"],
        overrides={
            "include": [{"section": "plan", "task_ref": task["task_key"], "note": "first"}],
            "exclude": [],
        },
        actor="editor",
    )
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        client.post("/login", data={"username": "admin", "password": "admin-password"})
        page = client.get(
            "/pmt/internal-status",
            params={
                "owner": "Farhan",
                "report_date": "2026-07-13",
                "period": "morning",
                "report_version": second["report_version"],
            },
        )
        import re

        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = client.post(
            "/pmt/internal-status/revise",
            data={
                "csrf_token": csrf,
                "owner": "Farhan",
                "report_date": "2026-07-13",
                "period": "morning",
                "expected_version": second["report_version"],
            },
        )
        assert response.status_code == 200
    third = store.get_internal_status_report("Farhan", "2026-07-13", "morning")
    assert third["overrides"] == second["overrides"]
    assert third["sections"]["plan"][0]["note"] == "first"


def test_mcp_report_paths_encode_every_path_segment(monkeypatch):
    calls = []

    def fake(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"ok": True}

    monkeypatch.setattr(pmt_mcp_server, "_request", fake)
    pmt_mcp_server.pmt_get_internal_status_draft("A/B C", "2026/07/13", "morning", 2)
    pmt_mcp_server.pmt_revise_internal_status_draft("A/B C", "2026/07/13", "morning", 2)
    pmt_mcp_server.pmt_approve_internal_status_draft("A/B C", "2026/07/13", "morning", 2)
    pmt_mcp_server.pmt_mark_internal_status_sent("A/B C", "2026/07/13", "morning", 2)
    for _, path, _ in calls:
        assert "/A%2FB%20C/2026%2F07%2F13/morning" in path


def test_web_selects_exact_historical_report_version(settings):
    store = PmtStore(settings.pmt_db_path)
    store.initialize()
    first = store.generate_internal_status_report(
        owner="Farhan", report_date="2026-07-13", period="morning", actor="first"
    )
    task = _create(store, "Only in latest")
    second = store.generate_internal_status_report(
        owner="Farhan",
        report_date="2026-07-13",
        period="morning",
        actor="second",
        regenerate=True,
        expected_version=first["report_version"],
        overrides={
            "include": [{"section": "plan", "task_ref": task["task_key"]}],
            "exclude": [],
        },
    )
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        client.post("/login", data={"username": "admin", "password": "admin-password"})
        historical = client.get(
            "/pmt/internal-status",
            params={
                "owner": "Farhan",
                "report_date": "2026-07-13",
                "period": "morning",
                "report_version": first["report_version"],
            },
        )
    assert "snapshot v1" in historical.text
    assert "Only in latest" not in historical.text
    assert f"report_version={second['report_version']}" in historical.text


def test_report_expected_version_conflict_and_override_bounds(settings):
    store = PmtStore(settings.pmt_db_path)
    store.initialize()
    _create(store, "Task")
    store.generate_internal_status_report(
        owner="Farhan", report_date="2026-07-13", period="morning", actor="test"
    )
    with pytest.raises(PermissionError):
        store.revise_internal_status_report(
            owner="Farhan",
            report_date="2026-07-13",
            period="morning",
            expected_version=99,
            overrides={"include": [], "exclude": []},
            actor="test",
        )
    with pytest.raises(ValueError, match="too many"):
        store.revise_internal_status_report(
            owner="Farhan",
            report_date="2026-07-13",
            period="morning",
            expected_version=1,
            overrides={
                "include": [{"section": "plan", "task_ref": "PMT-0001"} for _ in range(51)],
                "exclude": [],
            },
            actor="test",
        )


def test_concurrent_report_revision_allows_only_one_expected_version_writer(settings):
    store = PmtStore(settings.pmt_db_path)
    store.initialize()
    _create(store, "Task")
    store.generate_internal_status_report(
        owner="Farhan", report_date="2026-07-13", period="morning", actor="test"
    )
    barrier = threading.Barrier(2)
    results: list[str] = []

    def revise(actor: str) -> None:
        barrier.wait()
        try:
            store.revise_internal_status_report(
                owner="Farhan",
                report_date="2026-07-13",
                period="morning",
                expected_version=1,
                overrides={"include": [], "exclude": []},
                actor=actor,
            )
        except PermissionError:
            results.append("conflict")
        else:
            results.append("ok")

    threads = [threading.Thread(target=revise, args=(f"editor-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["conflict", "ok"]
    assert (
        store.get_internal_status_report("Farhan", "2026-07-13", "morning")["report_version"] == 2
    )


def _report_client(settings, scopes: list[str]) -> TestClient:
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    (settings.config_dir / "peers.json").write_text(
        json.dumps(
            {
                "allowedPeers": [
                    {
                        "name": "report-agent",
                        "tokenHash": hash_token("report-token"),
                        "enabled": True,
                        "scopes": scopes,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return TestClient(create_app(settings), base_url="https://testserver")


def test_report_api_scope_separation_and_lifecycle(settings):
    headers = {"Authorization": "Bearer report-token", "X-PMT-Agent": "report-agent"}
    with _report_client(settings, ["pmt.report.read"]) as client:
        denied = client.post(
            "/api/v1/pmt/internal-status/reports/generate",
            headers=headers,
            json={"owner": "Farhan", "period": "morning", "report_date": "2026-07-13"},
        )
        assert denied.status_code == 403
        schedule_denied = client.post(
            "/api/v1/pmt/schedules",
            headers=headers,
            json={
                "name": "Morning",
                "job_type": "internal_status_generate",
                "interval_seconds": 3600,
                "payload": {"owner": "Farhan", "period": "morning"},
            },
        )
        assert schedule_denied.status_code == 403
    scopes = [
        "pmt.report.read",
        "pmt.report.generate",
        "pmt.report.revise",
        "pmt.report.approve",
        "pmt.report.send",
    ]
    with _report_client(settings, scopes) as client:
        generated = client.post(
            "/api/v1/pmt/internal-status/reports/generate",
            headers=headers,
            json={"owner": "Farhan", "period": "morning", "report_date": "2026-07-13"},
        )
        assert generated.status_code == 200
        report = generated.json()["data"]["report"]
        assert (
            client.get("/api/v1/pmt/internal-status/reports?owner=Farhan", headers=headers)
            .json()["data"]["reports"][0]
            .get("rendered_text")
            is None
        )
        approved = client.post(
            "/api/v1/pmt/internal-status/reports/Farhan/2026-07-13/morning/approve",
            headers=headers,
            json={"expected_version": report["report_version"]},
        )
        assert approved.json()["data"]["report"]["state"] == "approved"
        sent = client.post(
            "/api/v1/pmt/internal-status/reports/Farhan/2026-07-13/morning/mark-sent",
            headers=headers,
            json={"expected_version": report["report_version"]},
        )
        assert sent.json()["data"]["report"]["state"] == "sent"


def test_mcp_report_tools_use_rest_adapter(monkeypatch):
    calls = []

    def fake(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"ok": True}

    monkeypatch.setattr(pmt_mcp_server, "_request", fake)
    pmt_mcp_server.pmt_generate_internal_status_draft("Farhan", "evening", "2026-07-13")
    pmt_mcp_server.pmt_mark_internal_status_sent("Farhan", "2026-07-13", "evening", 3)
    assert calls[0][1] == "/internal-status/reports/generate"
    assert calls[1][1].endswith("/mark-sent")
    assert calls[1][2]["json_body"]["expected_version"] == 3


def test_worker_internal_status_schedule_is_durable_and_does_not_send(settings, monkeypatch):
    store = PmtStore(settings.pmt_db_path)
    store.initialize()
    schedule = store.create_schedule(
        "Morning report",
        "internal_status_generate",
        60,
        {"owner": "Farhan", "period": "morning", "report_date": "2026-07-13"},
        "admin",
    )
    with store._connect() as db:
        db.execute(
            "UPDATE schedules SET next_run_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), schedule["id"]),
        )
        db.commit()
    monkeypatch.setattr(pmt_worker, "load_settings", lambda: settings)
    result = asyncio.run(pmt_worker.run_once("report-worker"))
    assert result["status"] == "succeeded"
    assert result["result"]["state"] == "draft"
    assert store.get_internal_status_report("Farhan", "2026-07-13", "morning") is not None


def test_internal_status_web_requires_auth_csrf_escapes_and_fences_version(settings):
    store = PmtStore(settings.pmt_db_path)
    store.initialize()
    task = store.create_task(TaskInput(title="<script>alert(1)</script>", assignee="Farhan"))
    _set_event_time(store, task["id"], "task.created", "2026-07-13T02:00:00+00:00")
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        assert client.get("/pmt/internal-status", follow_redirects=False).status_code == 303
        client.post("/login", data={"username": "admin", "password": "admin-password"})
        page = client.get("/pmt/internal-status")
        import re

        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        assert (
            client.post(
                "/pmt/internal-status/generate",
                data={
                    "csrf_token": "wrong",
                    "owner": "Farhan",
                    "report_date": "2026-07-13",
                    "period": "morning",
                },
            ).status_code
            == 403
        )
        generated = client.post(
            "/pmt/internal-status/generate",
            data={
                "csrf_token": csrf,
                "owner": "Farhan",
                "report_date": "2026-07-13",
                "period": "morning",
                "timezone_name": "Asia/Jakarta",
            },
            follow_redirects=True,
        )
        assert generated.status_code == 200
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in generated.text
        assert "<script>alert(1)</script>" not in generated.text
        stale = client.post(
            "/pmt/internal-status/approve",
            data={
                "csrf_token": csrf,
                "owner": "Farhan",
                "report_date": "2026-07-13",
                "period": "morning",
                "expected_version": 99,
            },
            follow_redirects=True,
        )
        assert "report changed since it was loaded" in stale.text
