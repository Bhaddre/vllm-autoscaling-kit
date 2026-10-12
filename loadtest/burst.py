"""Burst load against an OpenAI-compatible endpoint and report TTFT per phase.

The point isn't max throughput. It's to watch the autoscaler react:
quiet -> burst -> hold -> quiet, and see how long TTFT stays bad
before new replicas come up.

    pip install -r loadtest/requirements.txt
    python loadtest/burst.py --url http://localhost:8000 --model qwen2.5-7b \
        --phases 60:2,180:32,120:32,300:2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from dataclasses import dataclass, field

import httpx

PROMPT = "Explain how a Kubernetes HorizontalPodAutoscaler decides to scale, in about 200 words."


@dataclass(frozen=True)
class Phase:
    seconds: int
    concurrency: int


@dataclass
class PhaseResult:
    phase: Phase
    ttft: list[float] = field(default_factory=list)
    errors: int = 0


def parse_phases(spec: str) -> list[Phase]:
    """'60:2,180:32' -> [Phase(60, 2), Phase(180, 32)]"""
    phases = []
    for chunk in spec.split(","):
        secs, conc = chunk.strip().split(":")
        p = Phase(int(secs), int(conc))
        if p.seconds <= 0 or p.concurrency <= 0:
            raise ValueError(f"phase values must be positive: {chunk!r}")
        phases.append(p)
    if not phases:
        raise ValueError("need at least one phase")
    return phases


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Returns nan for an empty list."""
    if not values:
        return math.nan
    if not 0 < pct <= 100:
        raise ValueError("pct must be in (0, 100]")
    ordered = sorted(values)
    rank = math.ceil(pct / 100 * len(ordered))
    return ordered[rank - 1]


async def one_request(client: httpx.AsyncClient, url: str, model: str, max_tokens: int) -> float:
    """Stream one chat completion, return seconds until the first content token."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": max_tokens,
        "stream": True,
    }
    start = time.perf_counter()
    async with client.stream("POST", f"{url}/v1/chat/completions", json=body) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            choices = chunk.get("choices") or []
            if choices and choices[0].get("delta", {}).get("content"):
                ttft = time.perf_counter() - start
                # drain the rest so the server sees a normal request
                async for _ in resp.aiter_lines():
                    pass
                return ttft
    raise RuntimeError("stream ended without any content")


async def run_phase(client: httpx.AsyncClient, args: argparse.Namespace, phase: Phase) -> PhaseResult:
    result = PhaseResult(phase)
    deadline = time.monotonic() + phase.seconds

    async def worker() -> None:
        while time.monotonic() < deadline:
            try:
                result.ttft.append(await one_request(client, args.url, args.model, args.max_tokens))
            except (httpx.HTTPError, RuntimeError, json.JSONDecodeError):
                result.errors += 1
                await asyncio.sleep(1)  # don't hot-loop on a dead endpoint

    await asyncio.gather(*(worker() for _ in range(phase.concurrency)))
    return result


def report(r: PhaseResult) -> str:
    return (
        f"{r.phase.seconds:>5}s x{r.phase.concurrency:<4} "
        f"reqs={len(r.ttft):<5} errors={r.errors:<4} "
        f"ttft p50={percentile(r.ttft, 50):.2f}s p95={percentile(r.ttft, 95):.2f}s"
    )


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="base url, e.g. http://localhost:8000")
    ap.add_argument("--model", required=True, help="served model name")
    ap.add_argument("--phases", default="60:2,180:32,120:32,300:2", help="seconds:concurrency,...")
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--timeout", type=float, default=300, help="per-request timeout seconds")
    args = ap.parse_args()

    phases = parse_phases(args.phases)
    limits = httpx.Limits(max_connections=max(p.concurrency for p in phases))
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as client:
        for phase in phases:
            print(report(await run_phase(client, args, phase)), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
