from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from mcp_transfer_node.pmt_gdocs import (
    DOCS_API_SCOPE,
    GOOGLE_OAUTH_TOKEN_URI,
    GoogleDocsError,
    _secure_service_account_info,
    parse_google_doc_payload,
    parse_google_doc_url,
    read_google_doc,
    validate_google_doc_url,
)


def paragraph(
    text: str,
    *,
    style: str = "NORMAL_TEXT",
    bullet: dict[str, Any] | None = None,
    link: str | None = None,
) -> dict[str, Any]:
    text_style = {"link": {"url": link}} if link else {}
    value: dict[str, Any] = {
        "elements": [{"textRun": {"content": text + "\n", "textStyle": text_style}}],
        "paragraphStyle": {"namedStyleType": style},
    }
    if bullet is not None:
        value["bullet"] = bullet
    return {"paragraph": value}


def tab(
    tab_id: str,
    title: str,
    content: list[dict[str, Any]],
    *,
    parent: str | None = None,
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    properties: dict[str, Any] = {"tabId": tab_id, "title": title}
    if parent is not None:
        properties["parentTabId"] = parent
    return {
        "tabProperties": properties,
        "documentTab": {"body": {"content": content}},
        "childTabs": children or [],
    }


def document_payload(*tabs: dict[str, Any], document_id: str = "doc_123") -> dict[str, Any]:
    return {
        "documentId": document_id,
        "title": "Security Plan",
        "revisionId": "rev-7",
        "tabs": list(tabs),
    }


@pytest.mark.parametrize(
    "url,document_id,selected_tab",
    [
        ("https://docs.google.com/document/d/abc_123-XYZ", "abc_123-XYZ", None),
        ("https://docs.google.com/document/d/abc/edit?tab=t.0", "abc", "t.0"),
        ("https://docs.google.com/document/d/abc/edit?tab=t.0&usp=sharing", "abc", "t.0"),
        ("https://docs.google.com/document/d/abc/edit?tab=t.0&tab=t.0", "abc", "t.0"),
        ("https://docs.google.com/document/d/abc/edit?usp=sharing", "abc", None),
        ("https://docs.google.com/document/d/abc/view/", "abc", None),
    ],
)
def test_parse_google_doc_url_accepts_only_canonical_links(url, document_id, selected_tab):
    parsed = parse_google_doc_url(url)
    assert parsed.document_id == document_id
    assert parsed.selected_tab_id == selected_tab
    assert validate_google_doc_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://docs.google.com/document/d/abc",
        "https://evil.test/document/d/abc",
        "https://docs.google.com.evil.test/document/d/abc",
        "https://docs.google.com@evil.test/document/d/abc",
        "https://user@docs.google.com/document/d/abc",
        "https://docs.google.com:443/document/d/abc",
        "https://docs.google.com/spreadsheets/d/abc",
        "https://docs.google.com/document/d/abc/../../private",
        "https://docs.google.com/document/d/abc?tab=",
        "https://docs.google.com/document/d/abc?tab=t.0&tab=t.1",
        "https://docs.google.com/document/d/abc#heading=h.x",
        "https://docs.google.com/document/d/" + "a" * 201,
        "https://docs.google.com/document/d/a%2Fb",
    ],
)
def test_parse_google_doc_url_rejects_malformed_and_ssrf_links(url):
    with pytest.raises(GoogleDocsError):
        parse_google_doc_url(url)


def test_parse_recursive_tabs_paragraphs_bullets_headings_tables_and_links():
    table_value = {
        "table": {
            "tableRows": [
                {
                    "tableCells": [
                        {"content": [paragraph("A1")]},
                        {"content": [paragraph("Docs", link="https://example.test/spec")]},
                    ]
                },
                {
                    "tableCells": [
                        {"content": [paragraph("A2")]},
                        {"content": [paragraph("B2")]},
                    ]
                },
            ]
        }
    }
    child = tab(
        "t.child",
        "Details",
        [paragraph("Nested", bullet={"listId": "list-1", "nestingLevel": 1})],
        parent="t.root",
    )
    payload = document_payload(
        tab(
            "t.root",
            "Overview",
            [paragraph("Security plan", style="HEADING_2"), table_value],
            children=[child],
        ),
        tab("t.other", "Appendix", [paragraph("Do not execute: $(rm -rf /)")]),
    )

    snapshot = parse_google_doc_payload(payload, selected_tab_id="t.child")

    assert snapshot["document_id"] == "doc_123"
    assert snapshot["title"] == "Security Plan"
    assert snapshot["revision_id"] == "rev-7"
    assert snapshot["selected_tab_id"] == "t.child"
    assert [item["tab_id"] for item in snapshot["tabs"]] == [
        "t.root",
        "t.child",
        "t.other",
    ]
    root, nested, other = snapshot["tabs"]
    assert root["parent_tab_id"] is None
    assert root["depth"] == 0
    assert root["position"] == 0
    assert root["position_path"] == [0]
    assert root["path"] == "Overview"
    assert root["text"] == (
        "## Security plan\n| A1 | Docs <https://example.test/spec> |\n| A2 | B2 |"
    )
    assert root["paragraphs"][0]["heading_level"] == 2
    assert root["paragraphs"][2]["links"] == [{"text": "Docs", "url": "https://example.test/spec"}]
    assert root["tables"][-1]["rows"][0][0] == "A1"
    assert nested["parent_tab_id"] == "t.root"
    assert nested["depth"] == 1
    assert nested["position_path"] == [0, 0]
    assert nested["path"] == "Overview / Details"
    assert nested["text"] == "  - Nested"
    assert nested["paragraphs"][0]["bullet"] == {
        "list_id": "list-1",
        "nesting_level": 1,
    }
    assert other["text"] == "Do not execute: $(rm -rf /)"

    assert snapshot["char_count"] == sum(len(item["text"]) for item in snapshot["tabs"])
    semantic = {
        "title": snapshot["title"],
        "selected_tab_id": snapshot["selected_tab_id"],
        "tabs": snapshot["tabs"],
    }
    encoded = json.dumps(
        semantic,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert snapshot["content_sha256"] == hashlib.sha256(encoded.encode()).hexdigest()


def test_parser_captures_inline_semantics_footnotes_and_tab_resources():
    base = document_payload(
        {
            "tabProperties": {"tabId": "root", "title": "Root"},
            "documentTab": {
                "body": {
                    "content": [
                        {
                            "paragraph": {
                                "elements": [
                                    {"person": {"personProperties": {"name": "Reviewer"}}},
                                    {
                                        "footnoteReference": {
                                            "footnoteId": "fn-1",
                                            "footnoteNumber": "1",
                                        }
                                    },
                                    {"horizontalRule": {}},
                                ],
                                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                            }
                        }
                    ]
                },
                "footnotes": {
                    "fn-1": {"content": [paragraph("Footnote body")]},
                },
                "inlineObjects": {"obj-1": {"inlineObjectProperties": {"description": "v1"}}},
            },
            "childTabs": [],
        }
    )
    changed = json.loads(json.dumps(base))
    changed["tabs"][0]["documentTab"]["inlineObjects"]["obj-1"]["inlineObjectProperties"][
        "description"
    ] = "v2"

    snapshot = parse_google_doc_payload(base)
    assert "[person Reviewer]" in snapshot["tabs"][0]["text"]
    assert "[footnote 1]" in snapshot["tabs"][0]["text"]
    assert "Footnote body" in snapshot["tabs"][0]["text"]
    assert snapshot["tabs"][0]["resources"]["inlineObjects"]
    assert parse_google_doc_payload(changed)["content_sha256"] != snapshot["content_sha256"]


def test_parser_fails_closed_on_unknown_paragraph_element():
    payload = document_payload(
        tab(
            "root",
            "Root",
            [
                {
                    "paragraph": {
                        "elements": [{"futureUnsupportedElement": {"value": "x"}}],
                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    }
                }
            ],
        )
    )
    with pytest.raises(GoogleDocsError, match="unsupported paragraph element"):
        parse_google_doc_payload(payload)


def test_hash_detects_semantic_structure_but_ignores_provider_revision():
    base = document_payload(tab("root", "Root", [paragraph("same")]))
    same_body_new_revision = json.loads(json.dumps(base))
    same_body_new_revision["revisionId"] = "provider-revision-2"
    renamed_tab = json.loads(json.dumps(base))
    renamed_tab["tabs"][0]["tabProperties"]["title"] = "Renamed"
    changed_link = document_payload(
        tab("root", "Root", [paragraph("same", link="https://example.test")])
    )

    initial = parse_google_doc_payload(base)
    assert (
        parse_google_doc_payload(same_body_new_revision)["content_sha256"]
        == initial["content_sha256"]
    )
    assert parse_google_doc_payload(renamed_tab)["content_sha256"] != initial["content_sha256"]
    assert parse_google_doc_payload(changed_link)["content_sha256"] != initial["content_sha256"]


def test_parser_allows_missing_revision_for_read_only_documents():
    payload = document_payload(tab("root", "Root", [paragraph("Read only")]))
    payload.pop("revisionId")

    snapshot = parse_google_doc_payload(payload)

    assert snapshot["revision_id"] == ""
    assert snapshot["content_sha256"]


def test_parser_rejects_non_string_revision_when_present():
    payload = document_payload(tab("root", "Root", []))
    payload["revisionId"] = 123

    with pytest.raises(GoogleDocsError, match="revisionId must be a string"):
        parse_google_doc_payload(payload)


def test_default_selected_tab_is_first_depth_first_tab():
    snapshot = parse_google_doc_payload(document_payload(tab("root", "Root", [])))
    assert snapshot["selected_tab_id"] == "root"


@pytest.mark.parametrize(
    "payload,error",
    [
        ({}, "documentId"),
        ({"documentId": "id", "title": "x", "revisionId": "r"}, "tabs"),
        (
            document_payload(
                tab("same", "First", []),
                tab("same", "Second", []),
            ),
            "duplicate tab ID",
        ),
        (
            document_payload(
                tab("root", "Root", [], children=[tab("child", "Child", [], parent="wrong")])
            ),
            "parent ID is inconsistent",
        ),
        (
            document_payload(tab("root", "Root", [{"unknownElement": {}}])),
            "unsupported structural element",
        ),
    ],
)
def test_parse_rejects_malformed_payloads(payload, error):
    with pytest.raises(GoogleDocsError, match=error):
        parse_google_doc_payload(payload)


def test_parse_rejects_missing_selected_tab_and_response_id_mismatch():
    payload = document_payload(tab("root", "Root", []))
    with pytest.raises(GoogleDocsError, match="Selected"):
        parse_google_doc_payload(payload, selected_tab_id="missing")
    with pytest.raises(GoogleDocsError, match="does not match"):
        parse_google_doc_payload(payload, expected_document_id="other")


def test_parse_enforces_tab_count_limit():
    payload = document_payload(*(tab(f"t-{index}", str(index), []) for index in range(101)))
    with pytest.raises(GoogleDocsError, match="100 tab"):
        parse_google_doc_payload(payload)


def test_parse_enforces_per_tab_and_total_character_limits():
    too_large_tab = document_payload(tab("root", "Root", [paragraph("x" * 100_001)]))
    with pytest.raises(GoogleDocsError, match="100000 character"):
        parse_google_doc_payload(too_large_tab)

    too_large_document = document_payload(
        *(tab(f"t-{index}", str(index), [paragraph("x" * 90_000)]) for index in range(6))
    )
    with pytest.raises(GoogleDocsError, match="500000 extracted"):
        parse_google_doc_payload(too_large_document)


@pytest.mark.asyncio
async def test_reader_uses_exact_api_endpoint_readonly_scope_and_injected_auth(tmp_path: Path):
    seen: dict[str, Any] = {}
    payload = document_payload(tab("t.0", "Root", [paragraph("Hello")]), document_id="abc")

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(
            200,
            json=payload,
            headers={"content-type": "application/json; charset=utf-8"},
            request=request,
        )

    async def token_provider(path: Path, scopes: tuple[str, ...]) -> str:
        seen["credential_path"] = path
        seen["scopes"] = scopes
        return "mock-token"

    snapshot = await read_google_doc(
        "https://docs.google.com/document/d/abc/edit?tab=t.0",
        tmp_path / "service-account.json",
        transport=httpx.MockTransport(handler),
        access_token_provider=token_provider,
        timeout_seconds=999,
    )

    request = seen["request"]
    assert request.method == "GET"
    assert str(request.url) == (
        "https://docs.googleapis.com/v1/documents/abc?includeTabsContent=true"
    )
    assert request.headers["authorization"] == "Bearer mock-token"
    assert request.headers["accept"] == "application/json"
    assert seen["credential_path"] == tmp_path / "service-account.json"
    assert seen["scopes"] == (DOCS_API_SCOPE,)
    assert request.extensions["timeout"]["connect"] == 60.0
    assert snapshot["selected_tab_id"] == "t.0"


@pytest.mark.asyncio
async def test_reader_accepts_synchronous_token_provider(tmp_path: Path):
    payload = document_payload(tab("root", "Root", []), document_id="abc")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    result = await read_google_doc(
        "https://docs.google.com/document/d/abc",
        tmp_path / "credentials.json",
        transport=httpx.MockTransport(handler),
        access_token_provider=lambda _path, _scopes: "token",
    )
    assert result["document_id"] == "abc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,error",
    [
        ((302, b"", {"location": "https://evil.test/"}), "redirects"),
        ((403, b'{"secret":"do not leak"}', {}), "denied access"),
        (
            (403, b'{"error":{"details":[{"reason":"SERVICE_DISABLED"}]}}', {}),
            "API is disabled",
        ),
        ((404, b"", {}), "not found"),
        ((429, b"", {}), "rate limit"),
        ((500, b"private upstream body", {}), "request failed"),
        ((200, b"not-json", {"content-type": "application/json"}), "malformed JSON"),
        ((200, b"{}", {"content-type": "text/html"}), "not JSON"),
    ],
)
async def test_reader_returns_safe_errors_without_following_redirects(tmp_path, response, error):
    status, content, headers = response
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response_headers = dict(headers)
        if status != 302 and "content-type" not in response_headers:
            response_headers["content-type"] = "application/json"
        return httpx.Response(status, content=content, headers=response_headers, request=request)

    with pytest.raises(GoogleDocsError, match=error) as exc_info:
        await read_google_doc(
            "https://docs.google.com/document/d/abc",
            tmp_path / "secret-name.json",
            transport=httpx.MockTransport(handler),
            access_token_provider=lambda _path, _scopes: "very-secret-token",
        )

    message = str(exc_info.value)
    assert "very-secret-token" not in message
    assert "secret-name.json" not in message
    assert "private upstream body" not in message
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_reader_rejects_response_over_5_mib(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b" " * (5 * 1024 * 1024 + 1),
            headers={"content-type": "application/json"},
            request=request,
        )

    with pytest.raises(GoogleDocsError, match="5 MiB"):
        await read_google_doc(
            "https://docs.google.com/document/d/abc",
            tmp_path / "credentials.json",
            transport=httpx.MockTransport(handler),
            access_token_provider=lambda _path, _scopes: "token",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), True, "30"])
async def test_reader_rejects_invalid_timeout_before_auth(tmp_path: Path, timeout):
    with pytest.raises(GoogleDocsError, match="timeout"):
        await read_google_doc(
            "https://docs.google.com/document/d/abc",
            tmp_path / "credentials.json",
            access_token_provider=lambda _path, _scopes: "token",
            timeout_seconds=timeout,
        )


def test_secure_credential_reader_pins_oauth_uri_and_rejects_unsafe_parent(tmp_path: Path):
    unsafe = tmp_path / "service-account.json"
    unsafe.write_text("{}", encoding="utf-8")
    os.chmod(unsafe, 0o600)
    with pytest.raises(GoogleDocsError, match="unsafe parent"):
        _secure_service_account_info(unsafe)

    with tempfile.TemporaryDirectory(prefix="pmt-gdocs-", dir=Path.home()) as directory:
        credential = Path(directory) / "service-account.json"
        credential.write_text(
            json.dumps({"type": "service_account", "token_uri": "https://evil.test/token"}),
            encoding="utf-8",
        )
        os.chmod(credential, 0o600)
        with pytest.raises(GoogleDocsError, match="OAuth endpoint"):
            _secure_service_account_info(credential)
        credential.write_text(
            json.dumps({"type": "service_account", "token_uri": GOOGLE_OAUTH_TOKEN_URI}),
            encoding="utf-8",
        )
        assert _secure_service_account_info(credential)["token_uri"] == GOOGLE_OAUTH_TOKEN_URI


@pytest.mark.asyncio
async def test_reader_total_deadline_bounds_slow_auth(tmp_path: Path):
    async def stalled_auth(_path: Path, _scopes: tuple[str, ...]) -> str:
        await asyncio.sleep(10)
        return "token"

    with pytest.raises(GoogleDocsError, match="allowed time"):
        await read_google_doc(
            "https://docs.google.com/document/d/abc",
            tmp_path / "credentials.json",
            access_token_provider=stalled_auth,
            timeout_seconds=3,
        )


@pytest.mark.asyncio
async def test_reader_total_deadline_bounds_slow_stream(tmp_path: Path):
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(1)
                yield b" "

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=SlowStream(),
            request=request,
        )

    with pytest.raises(GoogleDocsError, match="allowed time"):
        await read_google_doc(
            "https://docs.google.com/document/d/abc",
            tmp_path / "credentials.json",
            transport=httpx.MockTransport(handler),
            access_token_provider=lambda _path, _scopes: "token",
            timeout_seconds=3,
        )


@pytest.mark.asyncio
async def test_reader_wraps_unexpected_stream_failures(tmp_path: Path):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise RuntimeError("broken stream")
            yield b""  # pragma: no cover - keeps this an async iterator

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=BrokenStream(),
            request=request,
        )

    with pytest.raises(GoogleDocsError, match="API request failed"):
        await read_google_doc(
            "https://docs.google.com/document/d/abc",
            tmp_path / "credentials.json",
            transport=httpx.MockTransport(handler),
            access_token_provider=lambda _path, _scopes: "token",
        )


@pytest.mark.asyncio
async def test_reader_rejects_invalid_credential_path():
    with pytest.raises(GoogleDocsError, match="credential path"):
        await read_google_doc(
            "https://docs.google.com/document/d/abc",
            "",
            access_token_provider=lambda _path, _scopes: "token",
        )


@pytest.mark.asyncio
async def test_reader_wraps_auth_failure_and_never_calls_transport(tmp_path: Path):
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={}, request=request)

    def broken_auth(_path: Path, _scopes: tuple[str, ...]) -> str:
        raise RuntimeError("credential contents")

    with pytest.raises(GoogleDocsError, match="authentication failed") as exc_info:
        await read_google_doc(
            "https://docs.google.com/document/d/abc",
            tmp_path / "credentials.json",
            transport=httpx.MockTransport(handler),
            access_token_provider=broken_auth,
        )
    assert "credential contents" not in str(exc_info.value)
    assert called is False


def test_payload_is_json_serializable_and_deterministic():
    payload = document_payload(tab("root", "Root", [paragraph("hello")]))
    first = parse_google_doc_payload(payload)
    second = parse_google_doc_payload(json.loads(json.dumps(payload)))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
