from time import monotonic
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.workspace import routes
from app.workspace.network_slots import request_deadline


def test_workspace_query_uses_and_resets_remote_budget(monkeypatch):
    def run_request(user_id, payload, request, kind):
        remaining = request_deadline.get() - monotonic()
        assert 0 < remaining <= 0.25
        return {"status": "OK"}

    monkeypatch.setattr(routes, "run_request", run_request)
    request = SimpleNamespace(headers={"X-Workspace-Budget-Ms": "250"})
    assert routes.workspace_query(1, None, request) == {"status": "OK"}
    assert request_deadline.get() is None


@pytest.mark.parametrize("budget", ["0", "-1", "NaN", "123456"])
def test_workspace_query_rejects_invalid_remote_budget(budget):
    request = SimpleNamespace(headers={"X-Workspace-Budget-Ms": budget})
    with pytest.raises(HTTPException) as error:
        routes.workspace_query(1, None, request)
    assert error.value.status_code == 400
