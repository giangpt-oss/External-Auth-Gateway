"""Workspace boundary contract: paths, scopes and request schemas."""
import importlib
from pathlib import Path

import pytest


def catalog_module():
    assert Path("app/workspace/catalog.py").exists(), "Workspace catalog not implemented"
    return importlib.import_module("app.workspace.catalog")


def test_catalog_has_active_services_and_read_write_methods():
    catalog = catalog_module()
    assert set(catalog.SERVICES) == {"gmail", "calendar", "drive", "docs", "sheets", "slides"}
    for service in catalog.SERVICES:
        methods = catalog.methods_for(service)
        assert any(m["kind"] == "read" for m in methods.values())
        assert any(m["kind"] == "write" for m in methods.values())


def test_read_method_does_not_accept_write_or_extra_parameters():
    catalog = catalog_module()
    with pytest.raises(ValueError, match="read"):
        catalog.validate_call("drive", "read", "files.create", {}, {"name": "x"})
    with pytest.raises(ValueError):
        catalog.validate_call("drive", "read", "files.list", {"url": "https://evil.invalid"}, {})


def test_required_fields_and_resource_path_injection_are_rejected():
    catalog = catalog_module()
    with pytest.raises(ValueError):
        catalog.validate_call("drive", "read", "files.get", {}, {})
    for bad in ["../secrets", "x?alt=media", "https://evil.invalid", "a#b"]:
        with pytest.raises(ValueError):
            catalog.validate_call("drive", "read", "files.get", {"fileId": bad}, {})


def test_readonly_scope_never_grants_write_and_scope_matching_is_exact():
    catalog = catalog_module()
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    assert catalog.is_authorized("drive", "files.list", scopes)
    assert not catalog.is_authorized("drive", "files.create", scopes)
    assert not catalog.is_authorized("drive", "files.list", [scopes[0] + ".evil"])


def test_native_body_schema_and_page_limits_are_validated():
    catalog = catalog_module()
    with pytest.raises(ValueError):
        catalog.validate_call("sheets", "write", "spreadsheets.values.update", {"spreadsheetId": "abc", "range": "A1"}, {"values": "invalid"})
    with pytest.raises(ValueError):
        catalog.validate_call("drive", "read", "files.list", {"pageSize": 999999}, {})
    method, params, body = catalog.validate_call("docs", "write", "documents.create", {}, {"title": "Báo cáo"})
    assert method["httpMethod"] == "POST"
    assert body["title"] == "Báo cáo"


def test_gmail_is_always_current_google_user():
    catalog = catalog_module()
    with pytest.raises(ValueError):
        catalog.validate_call("gmail", "read", "users.messages.list", {"userId": "other@example.com"}, {})
    _, params, _ = catalog.validate_call("gmail", "read", "users.messages.list", {}, {})
    assert params["userId"] == "me"


def test_oauth_can_request_each_services_user_scopes():
    from app.google.oauth import GoogleOAuthService
    from app.workspace.catalog import methods_for
    for service in catalog_module().SERVICES:
        assert any(set(spec["scopes"]) & set(GoogleOAuthService.DEFAULT_SCOPES) for spec in methods_for(service).values())


def test_sheets_a1_range_is_encoded_not_rejected():
    from app.workspace.catalog import build_url
    module = catalog_module()
    spec, params, _ = module.validate_call("sheets", "read", "spreadsheets.values.get", {"spreadsheetId": "abc", "range": "'Báo cáo'!A1:D20"}, {})
    url = build_url("sheets", spec, params)
    assert "%3A" in url and "%21" in url
