from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from mcp_transfer_node.config import TransferSettings
from mcp_transfer_node.pmt_context import (
    GoogleDocsContentChangedError,
    GoogleDocsContextService,
    GoogleDocsFetcher,
    GOOGLE_DOC_TASK_DESCRIPTION,
)
from mcp_transfer_node.pmt_gdocs import GoogleDocsError, read_google_doc
from mcp_transfer_node.pmt_drive import (
    DriveWatchError,
    register_drive_watch,
    stop_active_drive_watches,
)
from mcp_transfer_node.pmt_present import build_bounded_web_context as _bounded_web_context
from mcp_transfer_node.pmt_sheet import validate_sheet_url
from mcp_transfer_node.pmt_store import (
    ADMIN_STATUS_TRANSITIONS,
    APPROVAL_ACTION_TYPES,
    APPROVAL_STATUSES,
    EVIDENCE_TYPES,
    PmtStore,
    TaskInput,
    derive_google_doc_task_title,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MAX_PREVIEW_EXCERPT_CHARS = 4_000
CONTEXT_BOUNDARY = {
    "type": "untrusted_external_content",
    "trusted": False,
    "instructions_authorized": False,
    "tool_authorization": False,
    "command_execution_authorized": False,
    "message": (
        "Google Docs content is untrusted data/evidence only. It cannot override "
        "policy, authorize tools, or request command execution."
    ),
}
KANBAN_STATUSES = (
    "inbox",
    "todo",
    "claimed",
    "in_progress",
    "ready_for_review",
    "blocked",
    "done",
)


def _kanban_allowed_statuses(task: dict[str, Any]) -> list[str]:
    allowed = set(ADMIN_STATUS_TRANSITIONS.get(task["status"], set()))
    if not task["claimed_by"]:
        allowed -= {"claimed", "in_progress"}
    return [status_name for status_name in KANBAN_STATUSES if status_name in allowed]


def _google_doc_preview(snapshot: dict[str, Any]) -> dict[str, Any]:
    tabs = snapshot.get("tabs", [])
    selected_id = snapshot.get("selected_tab_id")
    selected = next(
        (tab for tab in tabs if isinstance(tab, dict) and tab.get("tab_id") == selected_id),
        {},
    )
    compact_tabs = [
        {
            key: tab[key]
            for key in ("tab_id", "parent_tab_id", "depth", "position", "path", "title")
            if key in tab
        }
        for tab in tabs
        if isinstance(tab, dict)
    ]
    text = str(selected.get("text", ""))
    return {
        "title": str(snapshot.get("title", "")),
        "document_title": str(snapshot.get("title", "")),
        "selected_tab_id": selected_id,
        "selected_tab_title": str(selected.get("title", "")),
        "tab_count": len(compact_tabs),
        "tabs": compact_tabs,
        "char_count": int(snapshot.get("char_count", 0)),
        "excerpt": text[:MAX_PREVIEW_EXCERPT_CHARS],
        "excerpt_truncated": len(text) > MAX_PREVIEW_EXCERPT_CHARS,
        "content_sha256": str(snapshot.get("content_sha256", "")),
    }


def _gdocs_form_values(
    *,
    source_url: str = "",
    title: str = "",
    project: str = "HMX",
    module: str = "",
    menu: str = "",
    assignee: str = "Farhan",
    priority: str = "normal",
    target_branch: str = "Human-Resources",
    idempotency_key: str = "",
    expected_content_sha256: str = "",
) -> dict[str, str]:
    return {
        "source_url": source_url,
        "title": title,
        "project": project,
        "module": module,
        "menu": menu,
        "assignee": assignee,
        "priority": priority,
        "target_branch": target_branch,
        "idempotency_key": idempotency_key,
        "expected_content_sha256": expected_content_sha256,
    }


def _require_login(request: Request) -> None:
    if request.session.get("authenticated") is not True or not request.session.get("principal"):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )


def _principal(request: Request) -> str:
    _require_login(request)
    return str(request.session["principal"])


def _require_csrf(request: Request, token: str) -> None:
    expected = str(request.session.get("csrf_token", ""))
    if not expected or not secrets.compare_digest(expected, token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="CSRF token tidak valid")


def create_pmt_web_router(
    settings: TransferSettings,
    *,
    google_docs_fetcher: GoogleDocsFetcher = read_google_doc,
) -> APIRouter:
    store = PmtStore(settings.pmt_db_path)
    store.initialize()
    context_service = GoogleDocsContextService(store, settings, fetcher=google_docs_fetcher)
    router = APIRouter(prefix="/pmt", tags=["PMT Web"])

    def render_google_doc_intake(
        request: Request,
        *,
        form: dict[str, str],
        preview: dict[str, Any] | None = None,
        error: str | None = None,
        status_code: int = status.HTTP_200_OK,
    ) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "pmt_task_from_gdocs.html",
            {
                "settings": settings,
                "csrf_token": request.session["csrf_token"],
                "form": form,
                "preview": preview,
                "boundary": CONTEXT_BOUNDARY,
                "derived_title": derive_google_doc_task_title(preview, form["title"])
                if preview
                else "",
                "error": error,
            },
            status_code=status_code,
        )

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, task_status: str | None = None):
        _require_login(request)
        flash = request.session.pop("pmt_flash", None)
        tasks = store.list_tasks(limit=200)
        initial_status = task_status if task_status in {*KANBAN_STATUSES, "all"} else "todo"
        grouped = {
            name: [task for task in tasks if task["status"] == name] for name in KANBAN_STATUSES
        }
        task_status_transitions = {
            task["task_key"]: _kanban_allowed_statuses(task) for task in tasks
        }
        agents = store.list_agents()
        return TEMPLATES.TemplateResponse(
            request,
            "pmt_dashboard.html",
            {
                "settings": settings,
                "tasks": tasks,
                "grouped": grouped,
                "initial_status": initial_status,
                "task_status_transitions": task_status_transitions,
                "agents": agents,
                "online_agents": sum(
                    agent["effective_status"] in {"online", "busy", "draining"} for agent in agents
                ),
                "csrf_token": request.session["csrf_token"],
                "flash": flash,
            },
        )

    @router.get("/internal-status", response_class=HTMLResponse)
    def internal_status_reports(
        request: Request,
        owner: str = "",
        report_date: str = "",
        period: str = "",
        report_version: int | None = Query(default=None, ge=1, le=1_000_000),
    ) -> HTMLResponse:
        _require_login(request)
        selected = None
        error = request.session.pop("pmt_report_error", None)
        if owner and report_date and period:
            try:
                selected = store.get_internal_status_report(
                    owner, report_date, period, report_version
                )
            except ValueError as exc:
                error = str(exc)
        return TEMPLATES.TemplateResponse(
            request,
            "pmt_internal_status.html",
            {
                "settings": settings,
                "reports": store.list_internal_status_reports(owner=owner or None, limit=100),
                "selected": selected,
                "csrf_token": request.session["csrf_token"],
                "error": error,
            },
        )

    @router.post("/internal-status/generate")
    def generate_internal_status_report_web(
        request: Request,
        csrf_token: str = Form(...),
        owner: str = Form(...),
        report_date: str = Form(default=""),
        period: str = Form(...),
        timezone_name: str = Form(default="Asia/Jakarta"),
        regenerate: bool = Form(default=False),
        expected_version: int | None = Form(default=None),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            report = store.generate_internal_status_report(
                owner=owner,
                report_date=report_date or None,
                period=period,
                timezone_name=timezone_name,
                actor=_principal(request),
                regenerate=regenerate,
                expected_version=expected_version,
            )
        except (KeyError, PermissionError, ValueError) as exc:
            request.session["pmt_report_error"] = str(exc)
            return RedirectResponse("/pmt/internal-status", status_code=status.HTTP_303_SEE_OTHER)
        query = urlencode(
            {
                "owner": report["owner"],
                "report_date": report["report_date"],
                "period": report["period"],
                "report_version": report["report_version"],
            }
        )
        return RedirectResponse(
            f"/pmt/internal-status?{query}", status_code=status.HTTP_303_SEE_OTHER
        )

    @router.post("/internal-status/revise")
    def revise_internal_status_report_web(
        request: Request,
        csrf_token: str = Form(...),
        owner: str = Form(...),
        report_date: str = Form(...),
        period: str = Form(...),
        expected_version: int = Form(...),
        include_section: str = Form(default=""),
        include_task_ref: str = Form(default=""),
        include_note: str = Form(default=""),
        exclude_section: str = Form(default=""),
        exclude_task_ref: str = Form(default=""),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        overrides: dict[str, list[dict[str, str]]] = {"include": [], "exclude": []}
        try:
            prior = store.get_internal_status_report(owner, report_date, period, expected_version)
            if prior is None:
                raise KeyError(f"{owner}:{report_date}:{period}:{expected_version}")
            for action in ("include", "exclude"):
                overrides[action] = [dict(item) for item in prior["overrides"][action]]
        except (KeyError, ValueError) as exc:
            request.session["pmt_report_error"] = str(exc)
            return RedirectResponse("/pmt/internal-status", status_code=status.HTTP_303_SEE_OTHER)
        if include_section and include_task_ref:
            addition = {
                "section": include_section,
                "task_ref": include_task_ref,
                "note": include_note,
            }
            overrides["include"] = [
                item
                for item in overrides["include"]
                if (item["section"], item["task_ref"]) != (include_section, include_task_ref)
            ]
            overrides["include"].append(addition)
        if exclude_section and exclude_task_ref:
            overrides["exclude"] = [
                item
                for item in overrides["exclude"]
                if (item["section"], item["task_ref"]) != (exclude_section, exclude_task_ref)
            ]
            overrides["exclude"].append({"section": exclude_section, "task_ref": exclude_task_ref})
        try:
            store.revise_internal_status_report(
                owner=owner,
                report_date=report_date,
                period=period,
                expected_version=expected_version,
                overrides=overrides,
                actor=_principal(request),
            )
        except (KeyError, PermissionError, ValueError) as exc:
            request.session["pmt_report_error"] = str(exc)
        query = urlencode(
            {
                "owner": owner,
                "report_date": report_date,
                "period": period,
                "report_version": expected_version + 1,
            }
        )
        return RedirectResponse(
            f"/pmt/internal-status?{query}", status_code=status.HTTP_303_SEE_OTHER
        )

    def transition_internal_status_report_web(
        request: Request,
        *,
        csrf_token: str,
        owner: str,
        report_date: str,
        period: str,
        expected_version: int,
        target_state: str,
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            store.transition_internal_status_report(
                owner=owner,
                report_date=report_date,
                period=period,
                expected_version=expected_version,
                target_state=target_state,
                actor=_principal(request),
            )
        except (KeyError, PermissionError, ValueError) as exc:
            request.session["pmt_report_error"] = str(exc)
        query = urlencode(
            {
                "owner": owner,
                "report_date": report_date,
                "period": period,
                "report_version": expected_version,
            }
        )
        return RedirectResponse(
            f"/pmt/internal-status?{query}", status_code=status.HTTP_303_SEE_OTHER
        )

    @router.post("/internal-status/approve")
    def approve_internal_status_report_web(
        request: Request,
        csrf_token: str = Form(...),
        owner: str = Form(...),
        report_date: str = Form(...),
        period: str = Form(...),
        expected_version: int = Form(...),
    ) -> RedirectResponse:
        return transition_internal_status_report_web(
            request,
            csrf_token=csrf_token,
            owner=owner,
            report_date=report_date,
            period=period,
            expected_version=expected_version,
            target_state="approved",
        )

    @router.post("/internal-status/mark-sent")
    def mark_internal_status_report_sent_web(
        request: Request,
        csrf_token: str = Form(...),
        owner: str = Form(...),
        report_date: str = Form(...),
        period: str = Form(...),
        expected_version: int = Form(...),
    ) -> RedirectResponse:
        return transition_internal_status_report_web(
            request,
            csrf_token=csrf_token,
            owner=owner,
            report_date=report_date,
            period=period,
            expected_version=expected_version,
            target_state="sent",
        )

    @router.get("/tasks/from-google-doc", response_class=HTMLResponse)
    def google_doc_intake(request: Request) -> HTMLResponse:
        _require_login(request)
        return render_google_doc_intake(
            request,
            form=_gdocs_form_values(idempotency_key=secrets.token_urlsafe(24)),
        )

    @router.post("/tasks/from-google-doc/preview", response_class=HTMLResponse)
    async def preview_google_doc_task(
        request: Request,
        source_url: str = Form(...),
        title: str = Form(default=""),
        project: str = Form(default="HMX"),
        module: str = Form(default=""),
        menu: str = Form(default=""),
        assignee: str = Form(default="Farhan"),
        priority: str = Form(default="normal"),
        target_branch: str = Form(default="Human-Resources"),
        idempotency_key: str = Form(default=""),
        csrf_token: str = Form(...),
    ) -> HTMLResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        form = _gdocs_form_values(
            source_url=source_url,
            title=title,
            project=project,
            module=module,
            menu=menu,
            assignee=assignee,
            priority=priority,
            target_branch=target_branch,
            idempotency_key=idempotency_key.strip() or secrets.token_urlsafe(24),
        )
        try:
            snapshot = await context_service.preview(source_url)
        except GoogleDocsError as exc:
            return render_google_doc_intake(
                request, form=form, error=str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        except ValueError:
            return render_google_doc_intake(
                request,
                form=form,
                error="Google Docs provider returned an invalid response",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return render_google_doc_intake(request, form=form, preview=_google_doc_preview(snapshot))

    @router.post("/tasks/from-google-doc/confirm", response_class=HTMLResponse)
    async def confirm_google_doc_task(
        request: Request,
        source_url: str = Form(...),
        title: str = Form(default=""),
        project: str = Form(default="HMX"),
        module: str = Form(default=""),
        menu: str = Form(default=""),
        assignee: str = Form(default="Farhan"),
        priority: str = Form(default="normal"),
        target_branch: str = Form(default="Human-Resources"),
        expected_content_sha256: str = Form(...),
        idempotency_key: str = Form(...),
        csrf_token: str = Form(...),
    ) -> Response:
        _require_login(request)
        _require_csrf(request, csrf_token)
        form = _gdocs_form_values(
            source_url=source_url,
            title=title,
            project=project,
            module=module,
            menu=menu,
            assignee=assignee,
            priority=priority,
            target_branch=target_branch,
            idempotency_key=idempotency_key,
            expected_content_sha256=expected_content_sha256,
        )
        try:
            result = await context_service.create_task_from_google_doc(
                TaskInput(
                    title="Google Docs requirement",
                    description=GOOGLE_DOC_TASK_DESCRIPTION,
                    project=project,
                    module=module,
                    menu=menu,
                    assignee=assignee,
                    priority=priority,
                    target_branch=target_branch,
                ),
                source_url=source_url,
                title_override=title,
                actor=_principal(request),
                idempotency_key=idempotency_key,
                expected_content_sha256=expected_content_sha256,
            )
        except GoogleDocsContentChangedError as exc:
            return render_google_doc_intake(
                request, form=form, error=str(exc), status_code=status.HTTP_409_CONFLICT
            )
        except GoogleDocsError as exc:
            return render_google_doc_intake(
                request, form=form, error=str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        except (KeyError, PermissionError, ValueError) as exc:
            return render_google_doc_intake(
                request, form=form, error=str(exc), status_code=status.HTTP_409_CONFLICT
            )
        return RedirectResponse(
            f"/pmt/tasks/{result['task']['task_key']}", status_code=status.HTTP_303_SEE_OTHER
        )

    @router.get("/agents", response_class=HTMLResponse)
    def agent_control_center(request: Request):
        _require_login(request)
        agents = store.list_agents()
        return TEMPLATES.TemplateResponse(
            request,
            "pmt_agents.html",
            {
                "settings": settings,
                "agents": agents,
                "counts": {
                    status_name: sum(agent["effective_status"] == status_name for agent in agents)
                    for status_name in (
                        "online",
                        "busy",
                        "draining",
                        "offline",
                        "disabled",
                    )
                },
                "csrf_token": request.session["csrf_token"],
            },
        )

    @router.post("/agents/{agent_id}/mode")
    def update_agent_mode(
        request: Request,
        agent_id: str,
        mode: str = Form(...),
        csrf_token: str = Form(...),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            store.set_agent_mode(agent_id, mode, _principal(request))
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Agent tidak ditemukan") from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return RedirectResponse("/pmt/agents", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/agents/reconcile-leases")
    def reconcile_agent_leases(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        store.reconcile_expired_leases(_principal(request))
        store.reconcile_expired_approval_leases(_principal(request))
        return RedirectResponse("/pmt/agents", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/sync", response_class=HTMLResponse)
    def sync_center(request: Request):
        _require_login(request)
        schedules = [
            schedule
            for schedule in store.list_schedules()
            if schedule["job_type"] == "google_sheet_sync"
        ]
        return TEMPLATES.TemplateResponse(
            request,
            "pmt_sync.html",
            {
                "settings": settings,
                "schedules": schedules,
                "runs": store.list_schedule_runs(limit=50),
                "drive_watch": store.drive_watch_status(settings.pmt_drive_spreadsheet_id)
                if settings.pmt_drive_spreadsheet_id
                else {"channels": [], "events": {}, "desired_active": False},
                "csrf_token": request.session["csrf_token"],
            },
        )

    @router.get("/sync/drive-watch/status")
    def drive_watch_status(request: Request) -> dict[str, Any]:
        _require_login(request)
        return {
            "enabled": settings.pmt_drive_watch_enabled,
            **(
                store.drive_watch_status(settings.pmt_drive_spreadsheet_id)
                if settings.pmt_drive_spreadsheet_id
                else {"channels": [], "events": {}, "desired_active": False}
            ),
        }

    @router.post("/sync/drive-watch/register")
    @router.post("/sync/drive-watch/renew")
    async def register_or_renew_drive_watch(
        request: Request, csrf_token: str = Form(...)
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            await register_drive_watch(store, settings)
        except DriveWatchError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        return RedirectResponse("/pmt/sync", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/sync/drive-watch/stop")
    async def stop_drive_watch(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            await stop_active_drive_watches(store, settings)
        except DriveWatchError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return RedirectResponse("/pmt/sync", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/sync/schedules")
    def create_sync_schedule(
        request: Request,
        name: str = Form(...),
        csv_url: str = Form(...),
        interval_minutes: int = Form(default=15),
        assignee: str = Form(default="Farhan"),
        dev_status: str = Form(default="To-Do"),
        project: str = Form(default="HMX"),
        target_branch: str = Form(default="Human-Resources"),
        csrf_token: str = Form(...),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            csv_url = validate_sheet_url(csv_url)
            store.create_schedule(
                name,
                "google_sheet_sync",
                max(1, interval_minutes) * 60,
                {
                    "csv_url": csv_url,
                    "assignee": assignee,
                    "dev_status": dev_status,
                    "project": project,
                    "target_branch": target_branch,
                },
                _principal(request),
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return RedirectResponse("/pmt/sync", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/sync/schedules/{schedule_id}/toggle")
    def toggle_sync_schedule(
        request: Request,
        schedule_id: str,
        enabled: bool = Form(...),
        csrf_token: str = Form(...),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            store.set_schedule_enabled(schedule_id, enabled)
        except KeyError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail="Schedule tidak ditemukan"
            ) from exc
        return RedirectResponse("/pmt/sync", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/approvals", response_class=HTMLResponse)
    def approval_center(request: Request, approval_status: str | None = None):
        _require_login(request)
        try:
            approvals = store.list_approvals(status=approval_status, limit=200)
        except ValueError:
            approvals = store.list_approvals(limit=200)
        return TEMPLATES.TemplateResponse(
            request,
            "pmt_approvals.html",
            {
                "settings": settings,
                "approvals": approvals,
                "selected": None,
                "events": [],
                "runs": [],
                "action_types": sorted(APPROVAL_ACTION_TYPES),
                "approval_statuses": sorted(APPROVAL_STATUSES),
                "csrf_token": request.session["csrf_token"],
                "principal": _principal(request),
            },
        )

    @router.get("/approvals/{approval_ref}", response_class=HTMLResponse)
    def approval_detail(request: Request, approval_ref: str):
        _require_login(request)
        approval = store.get_approval(approval_ref)
        if approval is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Approval tidak ditemukan")
        return TEMPLATES.TemplateResponse(
            request,
            "pmt_approvals.html",
            {
                "settings": settings,
                "approvals": store.list_approvals(limit=200),
                "selected": approval,
                "events": store.approval_events(approval_ref),
                "runs": store.list_approval_runs(approval_ref),
                "action_types": sorted(APPROVAL_ACTION_TYPES),
                "approval_statuses": sorted(APPROVAL_STATUSES),
                "csrf_token": request.session["csrf_token"],
                "principal": _principal(request),
            },
        )

    @router.post("/approvals")
    def create_approval(
        request: Request,
        csrf_token: str = Form(...),
        action_type: str = Form(...),
        title: str = Form(...),
        reason: str = Form(default=""),
        payload_json: str = Form(...),
        idempotency_key: str = Form(...),
        task_ref: str = Form(default=""),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            payload = json.loads(payload_json)
            if not isinstance(payload, dict):
                raise ValueError("payload harus berupa JSON object")
            approval = store.create_approval_request(
                action_type=action_type,
                title=title,
                reason=reason,
                payload=payload,
                requested_by=_principal(request),
                idempotency_key=idempotency_key,
                task_ref=task_ref or None,
                admin_request=True,
            )
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail="Payload JSON tidak valid"
            ) from exc
        except (KeyError, PermissionError, ValueError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return RedirectResponse(
            f"/pmt/approvals/{approval['approval_key']}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.post("/approvals/{approval_ref}/decision")
    def decide_approval(
        request: Request,
        approval_ref: str,
        csrf_token: str = Form(...),
        decision: str = Form(...),
        note: str = Form(default=""),
        confirm_key: str = Form(...),
        version: int = Form(...),
        approval_ttl_minutes: int = Form(default=60),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        approval = store.get_approval(approval_ref)
        if approval is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Approval tidak ditemukan")
        if not secrets.compare_digest(confirm_key.strip(), approval["approval_key"]):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="Ketik approval key secara tepat untuk mengonfirmasi keputusan",
            )
        try:
            store.decide_approval(
                approval_ref,
                decision,
                _principal(request),
                note,
                version,
                max(5, approval_ttl_minutes) * 60,
            )
        except KeyError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail="Approval tidak ditemukan"
            ) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return RedirectResponse(
            f"/pmt/approvals/{approval['approval_key']}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.post("/tasks")
    def create_task(
        request: Request,
        title: str = Form(...),
        description: str = Form(default=""),
        project: str = Form(default="HMX"),
        module: str = Form(default=""),
        menu: str = Form(default=""),
        priority: str = Form(default="normal"),
        assignee: str = Form(default="Farhan"),
        target_branch: str = Form(default="Human-Resources"),
        csrf_token: str = Form(...),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            store.create_task(
                TaskInput(
                    title=title,
                    description=description,
                    project=project,
                    module=module,
                    menu=menu,
                    priority=priority,
                    assignee=assignee,
                    target_branch=target_branch,
                ),
                actor=_principal(request),
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return RedirectResponse("/pmt", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/tasks/{task_ref}", response_class=HTMLResponse)
    def task_detail(request: Request, task_ref: str):
        _require_login(request)
        task = store.get_task(task_ref)
        if task is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Task tidak ditemukan")
        allowed_statuses = set(ADMIN_STATUS_TRANSITIONS.get(task["status"], set()))
        if not task["claimed_by"]:
            allowed_statuses -= {"claimed", "in_progress"}
        return TEMPLATES.TemplateResponse(
            request,
            "pmt_task_detail.html",
            {
                "settings": settings,
                "task": task,
                "events": store.task_events(task_ref),
                "evidence": store.list_evidence(task_ref),
                "context_documents": [
                    _bounded_web_context(store.get_task_context_document(task_ref, item["id"]))
                    for item in store.list_task_context_documents(task_ref)
                ],
                "google_docs_configured": settings.google_docs_service_account_file is not None,
                "statuses": [task["status"]]
                + [
                    status_name
                    for status_name in (
                        "inbox",
                        "todo",
                        "claimed",
                        "in_progress",
                        "ready_for_review",
                        "blocked",
                        "done",
                        "cancelled",
                    )
                    if status_name in allowed_statuses
                ],
                "evidence_types": sorted(EVIDENCE_TYPES),
                "csrf_token": request.session["csrf_token"],
            },
        )

    @router.post("/tasks/{task_ref}/context")
    async def attach_context(
        request: Request,
        task_ref: str,
        source_url: str = Form(...),
        version: int = Form(...),
        csrf_token: str = Form(...),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            await context_service.attach(
                task_ref,
                source_url,
                actor=_principal(request),
                expected_version=version,
            )
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Task tidak ditemukan") from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return RedirectResponse(
            f"/pmt/tasks/{task_ref}#external-context", status_code=status.HTTP_303_SEE_OTHER
        )

    @router.post("/tasks/{task_ref}/context/{context_ref}/refresh")
    async def refresh_context(
        request: Request,
        task_ref: str,
        context_ref: str,
        version: int = Form(...),
        context_version: int = Form(...),
        csrf_token: str = Form(...),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            await context_service.refresh(
                task_ref,
                context_ref,
                actor=_principal(request),
                expected_version=version,
                expected_context_version=context_version,
            )
        except KeyError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail="Context tidak ditemukan"
            ) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return RedirectResponse(
            f"/pmt/tasks/{task_ref}#external-context", status_code=status.HTTP_303_SEE_OTHER
        )

    @router.post("/tasks/{task_ref}/context/{context_ref}/remove")
    def remove_context(
        request: Request,
        task_ref: str,
        context_ref: str,
        version: int = Form(...),
        context_version: int = Form(...),
        csrf_token: str = Form(...),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            store.remove_task_context_document(
                task_ref,
                context_ref,
                actor=_principal(request),
                expected_version=version,
                expected_context_version=context_version,
            )
        except KeyError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail="Context tidak ditemukan"
            ) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return RedirectResponse(
            f"/pmt/tasks/{task_ref}#external-context", status_code=status.HTTP_303_SEE_OTHER
        )

    @router.post("/tasks/{task_ref}/remove")
    def remove_task(
        request: Request,
        task_ref: str,
        version: int = Form(...),
        confirm_task_key: str = Form(...),
        csrf_token: str = Form(...),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        task = store.get_task(task_ref)
        if task is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Task tidak ditemukan")
        if not secrets.compare_digest(confirm_task_key.strip(), task["task_key"]):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="Ketik task key secara tepat untuk mengonfirmasi penghapusan",
            )
        try:
            removed = store.remove_task(
                task_ref,
                actor=_principal(request),
                expected_version=version,
            )
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Task tidak ditemukan") from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        request.session["pmt_flash"] = {
            "message": f"{removed['task_key']} berhasil dihapus.",
            "tone": "success",
        }
        return RedirectResponse("/pmt?task_status=all", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/tasks/{task_ref}/edit")
    def edit_task(
        request: Request,
        task_ref: str,
        title: str = Form(...),
        description: str = Form(default=""),
        project: str = Form(default="HMX"),
        module: str = Form(default=""),
        menu: str = Form(default=""),
        assignee: str = Form(default=""),
        priority: str = Form(default="normal"),
        required_checks: str = Form(default=""),
        target_branch: str = Form(default="Human-Resources"),
        source_branch: str = Form(default=""),
        commit_ref: str = Form(default=""),
        mr_url: str = Form(default=""),
        pipeline_url: str = Form(default=""),
        version: int = Form(...),
        csrf_token: str = Form(...),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            store.update_task(
                task_ref,
                actor=_principal(request),
                title=title,
                description=description,
                project=project,
                module=module,
                menu=menu,
                assignee=assignee,
                priority=priority,
                required_checks=[
                    item.strip()
                    for line in required_checks.splitlines()
                    for item in line.split(",")
                    if item.strip()
                ],
                target_branch=target_branch,
                source_branch=source_branch,
                commit_ref=commit_ref,
                mr_url=mr_url,
                pipeline_url=pipeline_url,
                expected_version=version,
            )
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Task tidak ditemukan") from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return RedirectResponse(f"/pmt/tasks/{task_ref}", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/tasks/{task_ref}/status")
    def update_status(
        request: Request,
        task_ref: str,
        task_status: str = Form(...),
        note: str = Form(default=""),
        version: int = Form(...),
        csrf_token: str = Form(...),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            store.admin_transition_task(
                task_ref, task_status, _principal(request), note, expected_version=version
            )
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Task tidak ditemukan") from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return RedirectResponse(
            f"/pmt/tasks/{task_ref}#activity", status_code=status.HTTP_303_SEE_OTHER
        )

    @router.post("/tasks/{task_ref}/status/kanban", response_class=JSONResponse)
    def update_kanban_status(
        request: Request,
        task_ref: str,
        task_status: str = Form(...),
        version: int = Form(...),
        csrf_token: str = Form(...),
    ) -> JSONResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            task = store.admin_transition_task(
                task_ref,
                task_status,
                _principal(request),
                "Moved from kanban",
                expected_version=version,
            )
        except KeyError:
            return JSONResponse(
                {"ok": False, "error": "Task tidak ditemukan"},
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except (PermissionError, ValueError) as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)}, status_code=status.HTTP_409_CONFLICT
            )
        allowed_statuses = _kanban_allowed_statuses(task)
        return JSONResponse(
            {
                "ok": True,
                "task": {
                    "task_key": task["task_key"],
                    "status": task["status"],
                    "version": task["version"],
                    "allowed_statuses": allowed_statuses,
                },
            }
        )

    @router.post("/tasks/{task_ref}/criteria")
    def add_criterion(
        request: Request,
        task_ref: str,
        text: str = Form(...),
        version: int = Form(...),
        csrf_token: str = Form(...),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            store.add_acceptance_criterion(
                task_ref, text, _principal(request), expected_version=version
            )
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Task tidak ditemukan") from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return RedirectResponse(
            f"/pmt/tasks/{task_ref}#acceptance", status_code=status.HTTP_303_SEE_OTHER
        )

    @router.post("/tasks/{task_ref}/criteria/{criterion_id}/toggle")
    def toggle_criterion(
        request: Request,
        task_ref: str,
        criterion_id: str,
        version: int = Form(...),
        csrf_token: str = Form(...),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            store.toggle_acceptance_criterion(
                task_ref, criterion_id, _principal(request), expected_version=version
            )
        except KeyError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail="Task/criterion tidak ditemukan"
            ) from exc
        return RedirectResponse(
            f"/pmt/tasks/{task_ref}#acceptance", status_code=status.HTTP_303_SEE_OTHER
        )

    @router.post("/tasks/{task_ref}/evidence")
    def add_evidence(
        request: Request,
        task_ref: str,
        evidence_type: str = Form(...),
        label: str = Form(default=""),
        url: str = Form(default=""),
        note: str = Form(default=""),
        version: int = Form(...),
        csrf_token: str = Form(...),
    ) -> RedirectResponse:
        _require_login(request)
        _require_csrf(request, csrf_token)
        try:
            store.add_evidence(
                task_ref,
                evidence_type=evidence_type,
                label=label,
                url=url,
                note=note,
                actor=_principal(request),
                expected_version=version,
            )
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Task tidak ditemukan") from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return RedirectResponse(
            f"/pmt/tasks/{task_ref}#evidence", status_code=status.HTTP_303_SEE_OTHER
        )

    return router
