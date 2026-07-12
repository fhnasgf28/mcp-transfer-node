from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
import re
import stat
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

import httpx

DOCS_API_SCOPE = "https://www.googleapis.com/auth/documents.readonly"
DOCS_API_ORIGIN = "https://docs.googleapis.com"
GOOGLE_OAUTH_TOKEN_URI = "https://oauth2.googleapis.com/token"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_CREDENTIAL_BYTES = 128 * 1024
MAX_TABS = 100
MAX_TAB_CHARS = 100_000
MAX_TOTAL_CHARS = 500_000
MAX_NESTING_DEPTH = 50
DEFAULT_TIMEOUT_SECONDS = 30.0

_DOCUMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_TAB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_DOCUMENT_PATH_RE = re.compile(
    r"^/document/d/(?P<document_id>[A-Za-z0-9_-]{1,200})"
    r"(?:/(?:edit|view|preview))?/?$"
)
_JSON_CONTENT_TYPES = {"application/json", "application/problem+json"}


class GoogleDocsError(ValueError):
    """A safe, user-facing Google Docs read or parsing error."""


@dataclass(frozen=True)
class GoogleDocLink:
    document_id: str
    selected_tab_id: str | None = None


AccessTokenProvider = Callable[[Path, tuple[str, ...]], str | Awaitable[str]]


def parse_google_doc_url(url: str) -> GoogleDocLink:
    """Validate a Google Docs document link and return its bounded identifiers.

    Only canonical HTTPS ``docs.google.com/document/d/...`` links are accepted.
    Sharing metadata is ignored. Repeated ``tab`` parameters are accepted only
    when every value selects the same non-empty tab.
    """
    if not isinstance(url, str) or not url or len(url) > 2_048:
        raise GoogleDocsError("Google Docs URL is invalid")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise GoogleDocsError("Google Docs URL is malformed") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname != "docs.google.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise GoogleDocsError("Google Docs URL must be an HTTPS document link on docs.google.com")
    match = _DOCUMENT_PATH_RE.fullmatch(parsed.path)
    if match is None:
        raise GoogleDocsError("Google Docs URL must contain a valid document ID")

    try:
        query_items = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=20,
        )
    except (ValueError, TypeError) as exc:
        raise GoogleDocsError("Google Docs URL query is malformed") from exc
    tab_ids = [value for key, value in query_items if key == "tab"]
    selected_tab_id = tab_ids[0] if tab_ids else None
    if selected_tab_id is not None and _TAB_ID_RE.fullmatch(selected_tab_id) is None:
        raise GoogleDocsError("Google Docs tab ID is invalid")
    if len(set(tab_ids)) > 1:
        raise GoogleDocsError("Google Docs URL contains conflicting tab query parameters")
    return GoogleDocLink(match.group("document_id"), selected_tab_id)


def validate_google_doc_url(url: str) -> str:
    """Validate *url* and return it unchanged for call-site convenience."""
    parse_google_doc_url(url)
    return url


def _object(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GoogleDocsError(f"Malformed Google Docs payload: {where} must be an object")
    return value


def _list(value: Any, where: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise GoogleDocsError(f"Malformed Google Docs payload: {where} must be a list")
    return value


def _string(value: Any, where: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise GoogleDocsError(f"Malformed Google Docs payload: {where} must be a string")
    return value


def _heading_level(named_style: str) -> int | None:
    if named_style.startswith("HEADING_") and named_style[8:].isdigit():
        level = int(named_style[8:])
        return level if 1 <= level <= 6 else None
    if named_style == "TITLE":
        return 1
    if named_style == "SUBTITLE":
        return 2
    return None


def _paragraph(paragraph: Any, where: str) -> tuple[str, dict[str, Any]]:
    value = _object(paragraph, where)
    elements = _list(value.get("elements", []), f"{where}.elements")
    rendered: list[str] = []
    links: list[dict[str, str]] = []
    for index, element_value in enumerate(elements):
        element = _object(element_value, f"{where}.elements[{index}]")
        if "textRun" in element:
            text_run = _object(element["textRun"], f"{where}.elements[{index}].textRun")
            content = _string(text_run.get("content"), f"{where}.elements[{index}].textRun.content")
            style = _object(
                text_run.get("textStyle", {}),
                f"{where}.elements[{index}].textRun.textStyle",
            )
            link_value = style.get("link")
            if link_value is not None:
                link = _object(link_value, f"{where}.elements[{index}].textRun.textStyle.link")
                url = _string(
                    link.get("url"),
                    f"{where}.elements[{index}].textRun.textStyle.link.url",
                    allow_empty=False,
                )
                clean_label = content.rstrip("\n")
                links.append({"text": clean_label, "url": url})
                rendered.append(f"{content.rstrip(chr(10))} <{url}>")
                if content.endswith("\n"):
                    rendered.append("\n")
            else:
                rendered.append(content)
        elif "richLink" in element:
            rich_link = _object(element["richLink"], f"{where}.elements[{index}].richLink")
            props = _object(
                rich_link.get("richLinkProperties", {}),
                f"{where}.elements[{index}].richLink.richLinkProperties",
            )
            title = _string(props.get("title", ""), f"{where}.richLink.title")
            uri = _string(props.get("uri", ""), f"{where}.richLink.uri")
            label = title or uri
            rendered.append(f"{label} <{uri}>" if title and uri else label)
            if uri:
                links.append({"text": title, "url": uri})
        elif "inlineObjectElement" in element:
            object_element = _object(
                element["inlineObjectElement"], f"{where}.elements[{index}].inlineObjectElement"
            )
            object_id = _string(
                object_element.get("inlineObjectId"),
                f"{where}.elements[{index}].inlineObjectElement.inlineObjectId",
                allow_empty=False,
            )
            rendered.append(f"[inline object {object_id}]")
        elif "footnoteReference" in element:
            reference = _object(
                element["footnoteReference"],
                f"{where}.elements[{index}].footnoteReference",
            )
            footnote_id = _string(
                reference.get("footnoteId"),
                f"{where}.elements[{index}].footnoteReference.footnoteId",
                allow_empty=False,
            )
            number = _string(
                reference.get("footnoteNumber", ""),
                f"{where}.elements[{index}].footnoteReference.footnoteNumber",
            )
            rendered.append(f"[footnote {number or footnote_id}]")
        elif "person" in element:
            person = _object(element["person"], f"{where}.elements[{index}].person")
            properties = _object(
                person.get("personProperties", {}),
                f"{where}.elements[{index}].person.personProperties",
            )
            name = _string(
                properties.get("name", ""),
                f"{where}.elements[{index}].person.personProperties.name",
            )
            email = _string(
                properties.get("email", ""),
                f"{where}.elements[{index}].person.personProperties.email",
            )
            rendered.append(f"[person {name or email}]")
        elif "autoText" in element:
            auto_text = _object(element["autoText"], f"{where}.elements[{index}].autoText")
            auto_type = _string(
                auto_text.get("type"),
                f"{where}.elements[{index}].autoText.type",
                allow_empty=False,
            )
            rendered.append(f"[auto text {auto_type}]")
        elif "horizontalRule" in element:
            _object(element["horizontalRule"], f"{where}.elements[{index}].horizontalRule")
            rendered.append("\n---\n")
        elif "pageBreak" in element:
            _object(element["pageBreak"], f"{where}.elements[{index}].pageBreak")
            rendered.append("\n[page break]\n")
        elif "columnBreak" in element:
            _object(element["columnBreak"], f"{where}.elements[{index}].columnBreak")
            rendered.append("\n[column break]\n")
        elif "equation" in element:
            equation = _object(element["equation"], f"{where}.elements[{index}].equation")
            rendered.append(
                "[equation "
                + json.dumps(
                    equation,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "]"
            )
        else:
            raise GoogleDocsError(
                f"Malformed Google Docs payload: unsupported paragraph element at "
                f"{where}.elements[{index}]"
            )

    raw_text = "".join(rendered).rstrip("\n")
    style = _object(value.get("paragraphStyle", {}), f"{where}.paragraphStyle")
    named_style = _string(
        style.get("namedStyleType", "NORMAL_TEXT"), f"{where}.paragraphStyle.namedStyleType"
    )
    heading_level = _heading_level(named_style)
    bullet_meta: dict[str, Any] | None = None
    prefix = ""
    if "bullet" in value:
        bullet = _object(value["bullet"], f"{where}.bullet")
        nesting = bullet.get("nestingLevel", 0)
        if not isinstance(nesting, int) or isinstance(nesting, bool) or not 0 <= nesting <= 20:
            raise GoogleDocsError(
                f"Malformed Google Docs payload: {where}.bullet nesting is invalid"
            )
        list_id = _string(bullet.get("listId", ""), f"{where}.bullet.listId")
        bullet_meta = {"list_id": list_id, "nesting_level": nesting}
        prefix = "  " * nesting + "- "
    elif heading_level is not None:
        prefix = "#" * heading_level + " "

    metadata: dict[str, Any] = {
        "text": raw_text,
        "named_style": named_style,
        "heading_level": heading_level,
        "bullet": bullet_meta,
        "links": links,
    }
    return prefix + raw_text, metadata


def _structural_content(
    content: Any,
    where: str,
    *,
    nesting_depth: int = 0,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    if nesting_depth > MAX_NESTING_DEPTH:
        raise GoogleDocsError("Malformed Google Docs payload: content nesting is too deep")
    items = _list(content, where)
    blocks: list[str] = []
    paragraphs: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    for index, item_value in enumerate(items):
        item = _object(item_value, f"{where}[{index}]")
        if "paragraph" in item:
            text, metadata = _paragraph(item["paragraph"], f"{where}[{index}].paragraph")
            blocks.append(text)
            paragraphs.append(metadata)
        elif "table" in item:
            table = _object(item["table"], f"{where}[{index}].table")
            row_values = _list(table.get("tableRows"), f"{where}[{index}].table.tableRows")
            rows: list[list[str]] = []
            for row_index, row_value in enumerate(row_values):
                row = _object(row_value, f"{where}[{index}].table.tableRows[{row_index}]")
                cell_values = _list(
                    row.get("tableCells"),
                    f"{where}[{index}].table.tableRows[{row_index}].tableCells",
                )
                cells: list[str] = []
                for cell_index, cell_value in enumerate(cell_values):
                    cell = _object(
                        cell_value,
                        f"{where}[{index}].table.tableRows[{row_index}].tableCells[{cell_index}]",
                    )
                    cell_text, cell_paragraphs, cell_tables = _structural_content(
                        cell.get("content", []),
                        f"{where}[{index}].table.tableRows[{row_index}].tableCells[{cell_index}].content",
                        nesting_depth=nesting_depth + 1,
                    )
                    paragraphs.extend(cell_paragraphs)
                    tables.extend(cell_tables)
                    cells.append(" / ".join(cell_text.splitlines()))
                rows.append(cells)
            table_lines = ["| " + " | ".join(cells) + " |" for cells in rows]
            table_text = "\n".join(table_lines)
            tables.append({"rows": rows, "text": table_text})
            blocks.append(table_text)
        elif "tableOfContents" in item:
            toc = _object(item["tableOfContents"], f"{where}[{index}].tableOfContents")
            toc_text, toc_paragraphs, toc_tables = _structural_content(
                toc.get("content", []),
                f"{where}[{index}].tableOfContents.content",
                nesting_depth=nesting_depth + 1,
            )
            blocks.append(toc_text)
            paragraphs.extend(toc_paragraphs)
            tables.extend(toc_tables)
        elif not any(key in item for key in ("sectionBreak", "columnBreak")):
            raise GoogleDocsError(
                f"Malformed Google Docs payload: unsupported structural element at {where}[{index}]"
            )
    return "\n".join(blocks).strip("\n"), paragraphs, tables


def _document_tab_text(
    document_tab: Mapping[str, Any], where: str
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Normalize body plus headers, footers and footnotes in stable key order."""
    body = _object(document_tab.get("body"), f"{where}.body")
    text, paragraphs, tables = _structural_content(body.get("content", []), f"{where}.body.content")
    blocks = [text] if text else []
    labels = {"headers": "Header", "footers": "Footer", "footnotes": "Footnote"}
    for collection_name, label in labels.items():
        collection = _object(document_tab.get(collection_name, {}), f"{where}.{collection_name}")
        for segment_id in sorted(collection):
            segment = _object(collection[segment_id], f"{where}.{collection_name}[{segment_id!r}]")
            segment_text, segment_paragraphs, segment_tables = _structural_content(
                segment.get("content", []),
                f"{where}.{collection_name}[{segment_id!r}].content",
                nesting_depth=1,
            )
            blocks.append(f"## {label} {segment_id}\n{segment_text}".rstrip())
            paragraphs.extend(segment_paragraphs)
            tables.extend(segment_tables)

    resources = {
        key: value
        for key, value in document_tab.items()
        if key not in {"body", "headers", "footers", "footnotes"}
    }
    try:
        json.dumps(resources, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise GoogleDocsError("Malformed Google Docs payload: tab resources are invalid") from exc
    return "\n\n".join(blocks), paragraphs, tables, resources


def parse_google_doc_payload(
    payload: Any,
    *,
    selected_tab_id: str | None = None,
    expected_document_id: str | None = None,
) -> dict[str, Any]:
    """Normalize a ``documents.get(includeTabsContent=true)`` response.

    Tabs are emitted in deterministic depth-first API order. Document text is
    data only: it is copied/formatted and is never evaluated or interpolated
    into executable code.
    """
    document = _object(payload, "document")
    document_id = _string(document.get("documentId"), "document.documentId", allow_empty=False)
    if _DOCUMENT_ID_RE.fullmatch(document_id) is None:
        raise GoogleDocsError("Malformed Google Docs payload: document ID is invalid")
    if expected_document_id is not None and document_id != expected_document_id:
        raise GoogleDocsError(
            "Google Docs response document ID does not match the requested document"
        )
    title = _string(document.get("title"), "document.title")
    revision_value = document.get("revisionId")
    revision_id = (
        ""
        if revision_value is None
        else _string(revision_value, "document.revisionId", allow_empty=False)
    )
    root_tabs = _list(document.get("tabs"), "document.tabs")
    if not root_tabs:
        raise GoogleDocsError("Malformed Google Docs payload: document has no tabs")

    normalized_tabs: list[dict[str, Any]] = []
    seen_tab_ids: set[str] = set()
    total_chars = 0

    def visit(
        tab_value: Any,
        *,
        parent_tab_id: str | None,
        depth: int,
        position: int,
        title_path: tuple[str, ...],
        position_path: tuple[int, ...],
    ) -> None:
        nonlocal total_chars
        if depth > MAX_NESTING_DEPTH:
            raise GoogleDocsError("Malformed Google Docs payload: tab nesting is too deep")
        if len(normalized_tabs) >= MAX_TABS:
            raise GoogleDocsError("Google Docs document exceeds the 100 tab limit")
        tab = _object(tab_value, "document.tabs[]")
        properties = _object(tab.get("tabProperties"), "tab.tabProperties")
        tab_id = _string(properties.get("tabId"), "tab.tabProperties.tabId", allow_empty=False)
        if _TAB_ID_RE.fullmatch(tab_id) is None:
            raise GoogleDocsError("Malformed Google Docs payload: tab ID is invalid")
        if tab_id in seen_tab_ids:
            raise GoogleDocsError(f"Malformed Google Docs payload: duplicate tab ID {tab_id!r}")
        seen_tab_ids.add(tab_id)
        tab_title = _string(properties.get("title"), "tab.tabProperties.title")
        declared_parent = properties.get("parentTabId")
        if declared_parent is not None:
            declared_parent = _string(declared_parent, "tab.tabProperties.parentTabId")
            if declared_parent != parent_tab_id:
                raise GoogleDocsError(
                    "Malformed Google Docs payload: tab parent ID is inconsistent"
                )

        document_tab = _object(tab.get("documentTab"), "tab.documentTab")
        text, paragraphs, tables, resources = _document_tab_text(document_tab, "tab.documentTab")
        char_count = len(text)
        if char_count > MAX_TAB_CHARS:
            raise GoogleDocsError(f"Google Docs tab {tab_id!r} exceeds the 100000 character limit")
        total_chars += char_count
        if total_chars > MAX_TOTAL_CHARS:
            raise GoogleDocsError(
                "Google Docs document exceeds the 500000 extracted character limit"
            )

        current_title_path = title_path + (tab_title,)
        current_position_path = position_path + (position,)
        normalized_tabs.append(
            {
                "tab_id": tab_id,
                "parent_tab_id": parent_tab_id,
                "depth": depth,
                "position": position,
                "position_path": list(current_position_path),
                "path": " / ".join(current_title_path),
                "title": tab_title,
                "text": text,
                "char_count": char_count,
                "paragraphs": paragraphs,
                "tables": tables,
                "resources": resources,
            }
        )
        child_tabs = _list(tab.get("childTabs", []), "tab.childTabs")
        for child_position, child in enumerate(child_tabs):
            visit(
                child,
                parent_tab_id=tab_id,
                depth=depth + 1,
                position=child_position,
                title_path=current_title_path,
                position_path=current_position_path,
            )

    for root_position, root_tab in enumerate(root_tabs):
        visit(
            root_tab,
            parent_tab_id=None,
            depth=0,
            position=root_position,
            title_path=(),
            position_path=(),
        )

    effective_selected_tab_id = selected_tab_id or normalized_tabs[0]["tab_id"]
    if effective_selected_tab_id not in seen_tab_ids:
        raise GoogleDocsError("Selected Google Docs tab was not found in the document")
    # Provider revision IDs are intentionally excluded: they may change for edits
    # that do not affect the normalized task context. Everything agents can
    # observe semantically (selection, titles, hierarchy, text and structured
    # metadata) is included so a changed link/list/table cannot hide behind the
    # same rendered plain text.
    semantic_snapshot = {
        "title": title,
        "selected_tab_id": effective_selected_tab_id,
        "tabs": normalized_tabs,
    }
    semantic_json = json.dumps(
        semantic_snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return {
        "document_id": document_id,
        "title": title,
        "revision_id": revision_id,
        "tabs": normalized_tabs,
        "content_sha256": hashlib.sha256(semantic_json.encode("utf-8")).hexdigest(),
        "char_count": total_chars,
        "selected_tab_id": effective_selected_tab_id,
    }


def _secure_service_account_info(credential_path: Path) -> dict[str, Any]:
    """Read one pinned service-account file without following symlinks."""
    absolute = credential_path.absolute()
    for parent in absolute.parents:
        try:
            metadata = parent.lstat()
        except OSError as exc:
            raise GoogleDocsError("Google Docs credential directory is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise GoogleDocsError("Google Docs credential path has an unsafe parent directory")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise GoogleDocsError("Google Docs credential file is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise GoogleDocsError(
                "Google Docs credential must be an owner-owned, owner-only regular file"
            )
        chunks = bytearray()
        while len(chunks) <= MAX_CREDENTIAL_BYTES:
            chunk = os.read(descriptor, min(16_384, MAX_CREDENTIAL_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > MAX_CREDENTIAL_BYTES:
            raise GoogleDocsError("Google Docs credential file is unexpectedly large")
    finally:
        os.close(descriptor)
    try:
        info = json.loads(bytes(chunks))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GoogleDocsError("Google Docs credential file is malformed") from exc
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise GoogleDocsError("Google Docs credential is not a service account")
    if info.get("token_uri") != GOOGLE_OAUTH_TOKEN_URI:
        raise GoogleDocsError("Google Docs credential OAuth endpoint is not allowed")
    return info


async def _default_access_token_provider(
    credential_path: Path, scopes: tuple[str, ...], timeout_seconds: float
) -> str:
    def load_and_refresh() -> str:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
        except ImportError as exc:
            raise GoogleDocsError(
                "Google Docs authentication requires the google-auth[requests] dependency"
            ) from exc
        try:
            credentials = service_account.Credentials.from_service_account_info(
                _secure_service_account_info(credential_path), scopes=list(scopes)
            )

            class BoundedRequest:
                def __init__(self) -> None:
                    self.request = Request()

                def __call__(self, *args: Any, **kwargs: Any) -> Any:
                    kwargs["timeout"] = timeout_seconds
                    return self.request(*args, **kwargs)

            credentials.refresh(BoundedRequest())
            token = credentials.token
        except GoogleDocsError:
            raise
        except Exception as exc:
            raise GoogleDocsError("Google service-account authentication failed") from exc
        if not isinstance(token, str) or not token:
            raise GoogleDocsError("Google service-account authentication returned no access token")
        return token

    return await asyncio.to_thread(load_and_refresh)


async def get_google_access_token(
    credential_path: Path, scopes: tuple[str, ...], timeout_seconds: float
) -> str:
    """Acquire a bounded OAuth token using the hardened service-account reader."""
    timeout = max(3.0, min(float(timeout_seconds), 60.0))
    try:
        async with asyncio.timeout(timeout):
            return await _default_access_token_provider(credential_path, scopes, timeout)
    except TimeoutError as exc:
        raise GoogleDocsError("Google authentication timed out") from exc


async def read_google_doc(
    url: str,
    service_account_file: str | Path,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token_provider: AccessTokenProvider | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Fetch and normalize one Google document through the Google Docs API."""
    link = parse_google_doc_url(url)
    try:
        if not isinstance(service_account_file, (str, Path)) or not str(service_account_file):
            raise ValueError
        credential_path = Path(service_account_file)
    except (TypeError, ValueError) as exc:
        raise GoogleDocsError("Google service-account credential path is invalid") from exc
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(float(timeout_seconds))
    ):
        raise GoogleDocsError("Google Docs timeout is invalid")
    timeout = max(3.0, min(float(timeout_seconds), 60.0))
    try:
        async with asyncio.timeout(timeout):
            try:
                if access_token_provider is None:
                    token_result = _default_access_token_provider(
                        credential_path, (DOCS_API_SCOPE,), timeout
                    )
                else:
                    token_result = access_token_provider(credential_path, (DOCS_API_SCOPE,))
                token = await token_result if inspect.isawaitable(token_result) else token_result
            except GoogleDocsError:
                raise
            except Exception as exc:
                raise GoogleDocsError("Google service-account authentication failed") from exc
            if not isinstance(token, str) or not token or any(char.isspace() for char in token):
                raise GoogleDocsError(
                    "Google service-account authentication returned an invalid token"
                )

            endpoint = f"{DOCS_API_ORIGIN}/v1/documents/{quote(link.document_id, safe='')}"
            body = bytearray()
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=False,
                transport=transport,
            ) as client:
                async with client.stream(
                    "GET",
                    endpoint,
                    params={"includeTabsContent": "true"},
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                ) as response:
                    if response.is_redirect:
                        raise GoogleDocsError("Google Docs API redirects are not allowed")
                    if str(response.url).split("?", 1)[0] != endpoint:
                        raise GoogleDocsError("Google Docs API returned an unexpected endpoint")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type not in _JSON_CONTENT_TYPES:
                        raise GoogleDocsError("Google Docs API response is not JSON")
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise GoogleDocsError("Google Docs API response exceeds 5 MiB")
                    if response.status_code == 403:
                        try:
                            error_payload = json.loads(bytes(body))
                            details = error_payload.get("error", {}).get("details", [])
                            service_disabled = any(
                                isinstance(item, Mapping)
                                and item.get("reason") == "SERVICE_DISABLED"
                                for item in details
                            )
                        except (AttributeError, json.JSONDecodeError, UnicodeDecodeError):
                            service_disabled = False
                        if service_disabled:
                            raise GoogleDocsError(
                                "Google Docs API is disabled for the configured Google Cloud project"
                            )
                        raise GoogleDocsError("Google Docs API denied access to the document")
                    if response.status_code in {401, 403}:
                        raise GoogleDocsError("Google Docs API denied access to the document")
                    if response.status_code == 404:
                        raise GoogleDocsError("Google Docs document was not found")
                    if response.status_code == 429:
                        raise GoogleDocsError("Google Docs API rate limit was reached")
                    if response.status_code >= 400:
                        raise GoogleDocsError("Google Docs API request failed")
    except TimeoutError as exc:
        raise GoogleDocsError(
            "Google Docs API could not be reached within the allowed time"
        ) from exc
    except GoogleDocsError:
        raise
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise GoogleDocsError(
            "Google Docs API could not be reached within the allowed time"
        ) from exc
    except httpx.HTTPError as exc:
        raise GoogleDocsError("Google Docs API request failed") from exc
    except Exception as exc:
        raise GoogleDocsError("Google Docs API request failed") from exc

    try:
        payload = json.loads(bytes(body))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GoogleDocsError("Google Docs API returned malformed JSON") from exc
    return parse_google_doc_payload(
        payload,
        selected_tab_id=link.selected_tab_id,
        expected_document_id=link.document_id,
    )


# Concise aliases for callers that prefer fetch/parse terminology.
fetch_google_doc = read_google_doc
parse_google_docs_payload = parse_google_doc_payload
