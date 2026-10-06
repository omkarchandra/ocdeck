import asyncio
import sys
import time
from unittest.mock import AsyncMock, patch

import pytest

from ocdeck.source import DashboardSource, V2ApiError, V2Location
from ocdeck.v2_read_api import MANAGED_CLIENT, ReadAPI, ReadAPIError
from ocdeck_permission_watcher import v2_api_json


def worker(body):
    return ReadAPI([sys.executable, "-u", "-c", "import sys,json,time,os\n" + body])


def test_worker_reused_and_large_response_is_not_truncated():
    api = worker("for line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'data':{'pid':os.getpid(),'text':'x'*524288}}))")
    try:
        first = api.request("v2.health.get")
        second = api.request("v2.health.get")
        assert first["pid"] == second["pid"]
        assert len(second["text"]) == 524288
        process = api.process
    finally:
        api.close()
    assert process.poll() is not None


@pytest.mark.parametrize("body", [
    "for line in sys.stdin: print('not json')",
    "for line in sys.stdin: print(json.dumps({'id':999,'data':{}}))",
    "for line in sys.stdin: print('x'*1100000)",
    "for line in sys.stdin: time.sleep(30)",
])
def test_failed_worker_is_reaped_and_respawns_are_rate_limited(body):
    api = worker(body)
    try:
        with pytest.raises(ReadAPIError):
            api.request("v2.health.get", timeout=.05)
        assert api.process is None
        started = time.monotonic()
        for _ in range(100):
            with pytest.raises(ReadAPIError, match="retry delayed"):
                api.request("v2.health.get")
        assert time.monotonic() - started < .5
        assert api.process is None
    finally:
        api.close()


def test_service_error_keeps_worker_for_discovery_backoff():
    api = worker("for line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'error':'unavailable'}))")
    try:
        with pytest.raises(ReadAPIError): api.request("v2.health.get")
        process = api.process
        with pytest.raises(ReadAPIError): api.request("v2.health.get")
        assert api.process is process and process.poll() is None
    finally:
        api.close()


def test_managed_background_reads_never_fall_back_to_autostarting_cli():
    async def check():
        source = DashboardSource(backend="v2", opencode_bin=MANAGED_CLIENT)
        with patch("ocdeck.source.read_api", side_effect=ReadAPIError("offline")), patch(
            "ocdeck.source.asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as spawn:
            with pytest.raises(V2ApiError): await source._v2_api_json("v2.health.get")
            spawn.assert_not_called()
    asyncio.run(check())
    with patch("ocdeck_permission_watcher.read_api", side_effect=ReadAPIError("offline")), patch(
        "ocdeck_permission_watcher.subprocess.run"
    ) as spawn:
        assert v2_api_json(MANAGED_CLIENT, "v2.debug.location.list") is None
        spawn.assert_not_called()


def test_sdk_response_must_still_match_the_requested_location():
    async def check():
        source = DashboardSource(backend="v2", opencode_bin=MANAGED_CLIENT)
        with patch("ocdeck.source.read_api", return_value={"location": {"directory": "/other"}, "data": []}):
            with pytest.raises(V2ApiError, match="location envelope"):
                await source._v2_api_json("v2.form.request.list", location=V2Location("/work"))
    asyncio.run(check())
