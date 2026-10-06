import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from ocdeck import v2_browser
from ocdeck.source import V2ApiError


class Source:
    api_url = ""

    def __init__(self, directory):
        self.info = {"id": "ses_existing", "location": {"directory": str(directory)}, "agent": "build",
                     "model": {"providerID": "openai", "id": "gpt-6-astra", "variant": "high"},
                     "title": "Existing history", "metadata": {}, "permissions": [{"action": "edit", "resource": "*", "effect": "deny"}]}
        self.calls = []
        self.active = {}
        self.pending = {}
        self.connected = True
        self.lost_reply = False

    async def _v2_api_json(self, operation, **kwargs):
        self.calls.append((operation, deepcopy(kwargs)))
        if operation == "v2.mcp.list":
            return {"location": {"directory": self.info["location"]["directory"]},
                    "data": [{"name": "signed_in_tabs", "status": {"status": "connected" if self.connected else "failed"}}]}
        if operation == "v2.session.get":
            return {"data": deepcopy(self.info)}
        if operation == "v2.session.active":
            return {"data": self.active}
        if operation == "v2.session.update":
            self.info["permissions"] = deepcopy(kwargs["payload"]["permissions"])
            if self.lost_reply:
                raise V2ApiError("response lost")
            return None
        if operation == "v2.session.create":
            self.info = {"title": "Untitled", "agent": "build", **deepcopy(kwargs["payload"])}
            if self.lost_reply:
                raise V2ApiError("response lost")
            return {"data": deepcopy(self.info)}
        raise AssertionError(operation)

    async def _v2_pending_requests(self):
        return self.pending, True


def test_existing_grant_preserves_identity_model_history_and_denials(tmp_path):
    source = Source(tmp_path)
    before = deepcopy(source.info)
    source.lost_reply = True
    result = asyncio.run(v2_browser.browser_access(source, tmp_path, "ses_existing"))
    assert not result.error and not result.uncertain
    for key in ("id", "model", "agent", "title", "metadata", "location"):
        assert source.info[key] == before[key]
    assert source.info["permissions"][0] == before["permissions"][0]
    count = len(source.info["permissions"])
    assert not asyncio.run(v2_browser.browser_access(source, tmp_path, "ses_existing")).error
    assert len(source.info["permissions"]) == count


def test_rejects_busy_child_managed_wrong_location_and_disconnected(tmp_path):
    for modify in (
        lambda source: source.active.update({"ses_existing": {"type": "running"}}),
        lambda source: source.info.update(parentID="ses_parent"),
        lambda source: source.info.update(metadata={"homeAgent": {"kind": "project-worker"}}),
        lambda source: source.info.update(location={"directory": "/different"}),
        lambda source: setattr(source, "connected", False),
        lambda source: source.pending.update({"ses_existing": [{"id": "per_pending"}]}),
    ):
        source = Source(tmp_path)
        modify(source)
        assert asyncio.run(v2_browser.browser_access(source, tmp_path, "ses_existing")).error
        assert not any(operation in {"v2.session.update", "v2.session.create"} for operation, _ in source.calls)


def test_new_session_reconciles_lost_create_with_one_stable_id(tmp_path, monkeypatch):
    source = Source(tmp_path)
    source.lost_reply = True
    metadata = {"homeAgent": {"kind": "interactive-project", "projectPath": str(tmp_path), "notePath": "", "browserEnabled": True}}
    signer = Mock(side_effect=lambda settings, identifier, body: {**body, "proofForTest": identifier})
    monkeypatch.setattr(v2_browser, "scoped_metadata", lambda *_: (SimpleNamespace(guarded_metadata=signer), None, metadata))
    result = asyncio.run(v2_browser.browser_access(source, tmp_path))
    assert not result.error and result.session_id == source.info["id"]
    creates = [kwargs for operation, kwargs in source.calls if operation == "v2.session.create"]
    assert len(creates) == 1
    assert "model" not in creates[0]["payload"]
    assert not any("prompt" in operation for operation, _ in source.calls)


def test_remote_grant_does_not_read_local_keys_or_fallback(tmp_path, monkeypatch):
    source = Source(tmp_path)
    source.api_url = "https://remote.example"
    helper = Mock()
    monkeypatch.setattr(v2_browser, "scoped_metadata", helper)
    assert asyncio.run(v2_browser.browser_access(source, tmp_path)).error
    assert source.calls == []
    helper.assert_not_called()
