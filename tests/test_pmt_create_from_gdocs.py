from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from mcp_transfer_node.auth import hash_token
from mcp_transfer_node.pmt_context import GoogleDocsContextService
from mcp_transfer_node.pmt_gdocs import parse_google_doc_payload
from mcp_transfer_node.pmt_mcp_server import pmt_create_task_from_google_doc
from mcp_transfer_node.pmt_store import PmtStore, TaskInput, derive_google_doc_task_title

DOC_URL = "https://docs.google.com/document/d/doc123/edit?tab=tab.main&usp=sharing&tab=tab.main"


def _snapshot(
    text: str = "Requirement <script>alert(1)</script>", *, revision: bool = True
) -> dict[str, object]:
    payload = {
        "documentId": "doc123",
        "title": "People <Roadmap>",
        "revisionId": "r1",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab.main", "title": "Main"},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "paragraph": {
                                    "elements": [{"textRun": {"content": text + "\n"}}],
                                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                }
                            }
                        ]
                    }
                },
                "childTabs": [
                    {
                        "tabProperties": {
                            "tabId": "tab.child",
                            "title": "Details",
                            "parentTabId": "tab.main",
                        },
                        "documentTab": {
                            "body": {
                                "content": [
                                    {
                                        "paragraph": {
                                            "elements": [{"textRun": {"content": "Child\n"}}],
                                            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                        }
                                    }
                                ]
                            }
                        },
                        "childTabs": [],
                    }
                ],
            }
        ],
    }
    if not revision:
        payload.pop("revisionId")
    return parse_google_doc_payload(payload, selected_tab_id="tab.main")


def _csrf_from(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _field(response, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}"[^>]*value="([^"]*)"', response.text)
    if match is not None:
        return match.group(1)
    selected = re.search(
        rf'<select[^>]*name="{re.escape(name)}"[^>]*>.*?<option value="([^"]+)"[^>]*selected',
        response.text,
        re.DOTALL,
    )
    assert selected is not None, name
    return selected.group(1)


def _enable_context_scope(settings: object) -> None:
    config_dir = settings.config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "peers.json").write_text(
        '{"allowedPeers":[{"name":"server-a","tokenHash":"'
        + hash_token("valid-token")
        + '","enabled":true,"scopes":["pmt.context.refresh"]}]}',
        encoding="utf-8",
    )


def test_derive_google_doc_task_title_is_bounded_and_safe() -> None:
    snapshot = _snapshot()
    assert derive_google_doc_task_title(snapshot, "") == "People <Roadmap> — Main"
    assert derive_google_doc_task_title(snapshot, "  Override\nTitle  ") == "Override Title"
    assert len(derive_google_doc_task_title(snapshot, "x" * 400)) == 300


def test_atomic_google_doc_create_is_idempotent_and_audited(settings) -> None:
    store = PmtStore(settings.pmt_db_path)
    store.initialize()
    first = store.create_task_from_google_doc(
        TaskInput(title="People <Roadmap> — Main", source="google_docs"),
        source_url=DOC_URL,
        snapshot=_snapshot(),
        actor="Farhan",
        idempotency_key="create-1",
    )
    replay = store.create_task_from_google_doc(
        TaskInput(title="People <Roadmap> — Main", source="google_docs"),
        source_url=DOC_URL,
        snapshot=_snapshot(),
        actor="Farhan",
        idempotency_key="create-1",
    )

    assert first["created"] is True
    assert replay["created"] is False
    assert first["task"]["version"] == 1
    assert first["task"]["description"] == (
        "Task dibuat dari Google Docs context. "
        "Gunakan snapshot terlampir sebagai requirement utama."
    )
    assert first["context"]["context_version"] == 1
    assert "tabs" not in first["context"]
    assert len(store.list_tasks()) == 1
    assert len(store.list_task_context_documents(first["task"]["task_key"])) == 1
    assert {event["event_type"] for event in store.task_events(first["task"]["task_key"])} == {
        "task.created",
        "task.context_attached",
    }

    removed = store.remove_task_context_document(
        first["task"]["task_key"],
        first["context"]["id"],
        actor="Farhan",
        expected_version=first["task"]["version"],
        expected_context_version=first["context"]["context_version"],
    )
    assert removed["id"] == first["context"]["id"]
    assert store.list_task_context_documents(first["task"]["task_key"]) == []
    assert store.get_google_doc_task_creation("create-1", DOC_URL) is None


def test_atomic_google_doc_create_rolls_back_task_when_context_insert_fails(
    settings, monkeypatch
) -> None:
    store = PmtStore(settings.pmt_db_path)
    store.initialize()

    def fail(*_args, **_kwargs):
        raise RuntimeError("snapshot write failed")

    monkeypatch.setattr(store, "_insert_initial_context", fail)
    with pytest.raises(RuntimeError, match="snapshot write failed"):
        store.create_task_from_google_doc(
            TaskInput(title="Will roll back", source="google_docs"),
            source_url=DOC_URL,
            snapshot=_snapshot(),
            actor="Farhan",
            idempotency_key="rollback-1",
        )
    assert store.list_tasks() == []


def test_concurrent_same_google_doc_idempotency_creates_one_task(settings) -> None:
    store = PmtStore(settings.pmt_db_path)
    store.initialize()

    def create() -> dict[str, object]:
        return store.create_task_from_google_doc(
            TaskInput(title="Concurrent", source="google_docs"),
            source_url=DOC_URL,
            snapshot=_snapshot(),
            actor="Farhan",
            idempotency_key="same-concurrent-key",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: create(), range(2)))
    assert sorted(result["created"] for result in results) == [False, True]
    assert len(store.list_tasks()) == 1
    assert len(store.task_events("PMT-0001")) == 2


def test_web_preview_confirm_and_replay_are_safe(client, settings, monkeypatch) -> None:
    snapshots = iter([_snapshot(revision=False), _snapshot(revision=False)])

    async def fake_fetch(self, _source_url):
        return next(snapshots)

    monkeypatch.setattr(GoogleDocsContextService, "_fetch", fake_fetch)
    login = client.post("/login", data={"username": "admin", "password": "admin-password"})
    assert login.status_code == 200
    intake = client.get("/pmt/tasks/from-google-doc")
    csrf = _csrf_from(intake)
    preview = client.post(
        "/pmt/tasks/from-google-doc/preview",
        data={
            "source_url": DOC_URL,
            "title": "",
            "project": "HMX",
            "module": "core_hr",
            "menu": "Employee",
            "assignee": "Farhan",
            "priority": "normal",
            "target_branch": "Human-Resources",
            "csrf_token": csrf,
        },
    )
    assert preview.status_code == 200
    assert "People &lt;Roadmap&gt;" in preview.text
    assert "Main" in preview.text and "tab.main" in preview.text
    assert "Details" in preview.text and "tab.child" in preview.text
    assert "Untrusted external content boundary" in preview.text
    assert "Requirement &lt;script&gt;alert(1)&lt;/script&gt;" in preview.text
    assert "People &lt;Roadmap&gt; — Main" in preview.text
    assert _field(preview, "expected_content_sha256")

    confirm_data = {
        "source_url": _field(preview, "source_url"),
        "title": _field(preview, "title"),
        "project": _field(preview, "project"),
        "module": _field(preview, "module"),
        "menu": _field(preview, "menu"),
        "assignee": _field(preview, "assignee"),
        "priority": _field(preview, "priority"),
        "target_branch": _field(preview, "target_branch"),
        "expected_content_sha256": _field(preview, "expected_content_sha256"),
        "idempotency_key": _field(preview, "idempotency_key"),
        "csrf_token": csrf,
    }
    created = client.post(
        "/pmt/tasks/from-google-doc/confirm", data=confirm_data, follow_redirects=False
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/pmt/tasks/PMT-0001"
    replay = client.post(
        "/pmt/tasks/from-google-doc/confirm", data=confirm_data, follow_redirects=False
    )
    assert replay.status_code == 303
    store = PmtStore(settings.pmt_db_path)
    assert len(store.list_tasks()) == 1
    assert len(store.list_task_context_documents("PMT-0001")) == 1
    assert len(store.task_events("PMT-0001")) == 2


def test_web_preview_failures_do_not_create_tasks(client, settings, monkeypatch) -> None:
    async def denied_fetch(self, _source_url):
        raise ValueError("provider token=secret private_key=/tmp/key")

    monkeypatch.setattr(GoogleDocsContextService, "_fetch", denied_fetch)
    client.post("/login", data={"username": "admin", "password": "admin-password"})
    intake = client.get("/pmt/tasks/from-google-doc")
    csrf = _csrf_from(intake)
    invalid = client.post(
        "/pmt/tasks/from-google-doc/preview",
        data={"source_url": "https://evil.example/doc", "csrf_token": csrf},
    )
    assert invalid.status_code == 400
    assert "Google Docs URL" in invalid.text
    assert "secret" not in invalid.text and "private_key" not in invalid.text
    denied = client.post(
        "/pmt/tasks/from-google-doc/preview",
        data={"source_url": DOC_URL, "csrf_token": csrf},
    )
    assert denied.status_code == 400
    assert "secret" not in denied.text and "/tmp/key" not in denied.text
    assert PmtStore(settings.pmt_db_path).list_tasks() == []


def test_web_google_doc_flow_keeps_csrf_and_390px_controls(client) -> None:
    client.post("/login", data={"username": "admin", "password": "admin-password"})
    dashboard = client.get("/pmt")
    assert 'data-bs-toggle="dropdown"' in dashboard.text
    assert 'data-bs-target="#newTaskModal"' in dashboard.text
    assert "Buat manual" in dashboard.text
    assert "Dari Google Docs" in dashboard.text
    assert "/pmt/tasks/from-google-doc" in dashboard.text
    intake = client.get("/pmt/tasks/from-google-doc")
    assert 'name="csrf_token"' in intake.text
    missing = client.post("/pmt/tasks/from-google-doc/preview", data={"source_url": DOC_URL})
    assert missing.status_code == 422
    wrong = client.post(
        "/pmt/tasks/from-google-doc/preview",
        data={"source_url": DOC_URL, "csrf_token": "wrong"},
    )
    assert wrong.status_code == 403
    css = client.get("/static/pmt-mobile.css?v=20260712-mobile6")
    assert "overflow-x: clip" in css.text
    assert "min-height: 44px" in css.text
    assert "width=device-width, initial-scale=1" in intake.text


def test_web_confirm_changed_hash_is_conflict_without_task(client, settings, monkeypatch) -> None:
    snapshots = iter([_snapshot("old"), _snapshot("new")])

    async def fake_fetch(self, _source_url):
        return next(snapshots)

    monkeypatch.setattr(GoogleDocsContextService, "_fetch", fake_fetch)
    client.post("/login", data={"username": "admin", "password": "admin-password"})
    intake = client.get("/pmt/tasks/from-google-doc")
    csrf = _csrf_from(intake)
    preview = client.post(
        "/pmt/tasks/from-google-doc/preview",
        data={"source_url": DOC_URL, "csrf_token": csrf},
    )
    confirm = client.post(
        "/pmt/tasks/from-google-doc/confirm",
        data={
            "source_url": _field(preview, "source_url"),
            "title": _field(preview, "title"),
            "project": _field(preview, "project"),
            "module": _field(preview, "module"),
            "menu": _field(preview, "menu"),
            "assignee": _field(preview, "assignee"),
            "priority": _field(preview, "priority"),
            "target_branch": _field(preview, "target_branch"),
            "expected_content_sha256": _field(preview, "expected_content_sha256"),
            "idempotency_key": _field(preview, "idempotency_key"),
            "csrf_token": csrf,
        },
    )
    assert confirm.status_code == 409
    assert "changed" in confirm.text.lower() or "preview" in confirm.text.lower()
    assert PmtStore(settings.pmt_db_path).list_tasks() == []


def test_google_doc_api_requires_refresh_scope_and_returns_metadata_only(
    client, settings, monkeypatch
) -> None:
    async def fake_fetch(self, _source_url):
        return _snapshot()

    monkeypatch.setattr(GoogleDocsContextService, "_fetch", fake_fetch)
    denied = client.post(
        "/api/v1/pmt/tasks/from-google-doc",
        headers={"Authorization": "Bearer valid-token", "X-PMT-Agent": "server-a"},
        json={"source_url": DOC_URL, "idempotency_key": "api-1"},
    )
    assert denied.status_code == 403
    _enable_context_scope(settings)
    headers = {"Authorization": "Bearer valid-token", "X-PMT-Agent": "server-a"}
    created = client.post(
        "/api/v1/pmt/tasks/from-google-doc",
        headers=headers,
        json={"source_url": DOC_URL, "idempotency_key": "api-1"},
    )
    assert created.status_code == 201
    data = created.json()["data"]
    assert data["task"]["title"] == "People <Roadmap> — Main"
    assert "snapshot terlampir" in data["task"]["description"]
    assert data["context"]["selected_tab_id"] == "tab.main"
    assert "tabs" not in data["context"]
    replay = client.post(
        "/api/v1/pmt/tasks/from-google-doc",
        headers=headers,
        json={"source_url": DOC_URL, "idempotency_key": "api-1"},
    )
    assert replay.status_code == 200
    assert len(PmtStore(settings.pmt_db_path).list_tasks()) == 1


def test_mcp_google_doc_create_forwards_explicit_confirmation(monkeypatch) -> None:
    calls = []

    def fake_request(method, path, *, json_body=None, params=None):
        calls.append((method, path, json_body, params))
        return {"task": {"task_key": "PMT-0001"}, "context": {"title": "Spec"}}

    monkeypatch.setattr("mcp_transfer_node.pmt_mcp_server._request", fake_request)
    result = pmt_create_task_from_google_doc(
        source_url=DOC_URL,
        idempotency_key="mcp-1",
        title="Override",
        module="core_hr",
    )
    assert result["task"]["task_key"] == "PMT-0001"
    assert calls == [
        (
            "POST",
            "/tasks/from-google-doc",
            {
                "source_url": DOC_URL,
                "idempotency_key": "mcp-1",
                "title": "Override",
                "description": "",
                "project": "HMX",
                "module": "core_hr",
                "menu": "",
                "assignee": "Farhan",
                "priority": "normal",
                "target_branch": "Human-Resources",
                "acceptance_criteria": [],
                "required_checks": [],
            },
            None,
        )
    ]
