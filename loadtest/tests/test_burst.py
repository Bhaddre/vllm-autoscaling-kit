import asyncio
import json
import math
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import burst  # noqa: E402


def test_parse_phases():
    assert burst.parse_phases("60:2, 180:32") == [burst.Phase(60, 2), burst.Phase(180, 32)]


@pytest.mark.parametrize("bad", ["", "60", "0:2", "60:0", "a:b"])
def test_parse_phases_rejects_garbage(bad):
    with pytest.raises(ValueError):
        burst.parse_phases(bad)


def test_percentile_nearest_rank():
    vals = [float(v) for v in range(1, 11)]
    assert burst.percentile(vals, 50) == 5.0
    assert burst.percentile(vals, 95) == 10.0
    assert burst.percentile(vals, 100) == 10.0
    assert math.isnan(burst.percentile([], 95))


def _sse(*chunks):
    lines = [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]
    return ("\n\n".join(lines) + "\n\n").encode()


def test_one_request_measures_first_content_token():
    # first chunk is role-only, no content. TTFT should come from the second.
    body = _sse(
        {"choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [{"delta": {"content": "Hi"}}]},
        {"choices": [{"delta": {"content": " there"}}]},
    )

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/chat/completions"
        assert json.loads(req.content)["stream"] is True
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await burst.one_request(c, "http://x", "m", 16)

    assert asyncio.run(go()) >= 0


def test_one_request_errors_on_empty_stream():
    def handler(req):
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await burst.one_request(c, "http://x", "m", 16)

    with pytest.raises(RuntimeError):
        asyncio.run(go())
