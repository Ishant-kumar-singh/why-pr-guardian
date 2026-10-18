"""Phase 7: FastAPI server exposing the agents and graph tools.

Endpoints:
  POST /api/guard        - run the guardian on a PR
  POST /api/guard/post   - actually post the drafted comment (requires DRY_RUN=false)
  POST /api/ask          - ask the question agent
  GET /api/graph/neighborhood?path=...  - subgraph around a file
  GET /api/bus-factor    - bus factor analysis
  GET /api/stats         - node/edge counts, repo, config
  POST /api/ingest       - rerun ingestion
  POST /api/extract      - rerun decision extraction
  GET /                  - serve the UI

Run:  uvicorn server:app --reload
"""

from __future__ import annotations

import sys
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent
import analysis
import config
import db
import extract
import graph_tools as gt
import guardian
import ingest

app = FastAPI(title="Why?")


class AskRequest(BaseModel):
    question: str


class GuardRequest(BaseModel):
    pr_number: int
    repo: str | None = None


class GuardPostRequest(BaseModel):
    pr_number: int
    body: str


@app.post("/api/ask")
async def api_ask(req: AskRequest) -> dict[str, Any]:
    config.require_llm_model()
    result = await asyncio.wait_for(asyncio.to_thread(agent.ask, req.question), timeout=120)
    result["graph"] = _trace_graph(result.get("trace", {}))
    return JSONResponse(result)


@app.post("/api/guard")
async def api_guard(req: GuardRequest) -> dict[str, Any]:
    config.require_llm_model()
    result = await asyncio.to_thread(guardian.guard_bounded, req.pr_number, req.repo)
    result["graph"] = _trace_graph(result.get("trace", {}))
    return JSONResponse(result)


@app.post("/api/guard/post")
async def api_guard_post(req: GuardPostRequest) -> dict[str, Any]:
    if config.DRY_RUN:
        raise HTTPException(400, "DRY_RUN is true; cannot post")
    if not config.GUARD_REPO:
        raise HTTPException(400, "GUARD_REPO is not configured")
    if not req.body.strip():
        raise HTTPException(422, "Comment body cannot be empty")
    result = await asyncio.to_thread(guardian.post_guardian_comment, req.pr_number, req.body, config.GUARD_REPO)
    if result.get("data", {}).get("error"):
        raise HTTPException(502, result["data"]["error"])
    return result["data"]


@app.get("/api/guard/comment/{pr_number}")
async def api_guard_comment(pr_number: int) -> dict[str, Any]:
    if not config.GUARD_REPO:
        raise HTTPException(400, "GUARD_REPO is not configured")
    result = await asyncio.to_thread(guardian.find_guardian_comment, pr_number, config.GUARD_REPO)
    if result.get("data", {}).get("error"):
        raise HTTPException(502, result["data"]["error"])
    return result["data"]


@app.post("/api/guard/stream")
async def api_guard_stream(req: GuardRequest):
    """Stream guardian steps as server-sent events while work runs in a thread."""
    config.require_llm_model()
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    def emit(event: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, {"event": "step", "data": event})
    future = asyncio.create_task(asyncio.to_thread(guardian.guard_bounded, req.pr_number, req.repo, emit))

    async def events():
        deadline = loop.time() + guardian.GUARDIAN_TIMEOUT + 2
        while not future.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                future.cancel()
                yield f"event: error\ndata: {json.dumps({'error': 'Guardian timed out after 120s'})}\n\n"
                return
            try:
                item = await asyncio.wait_for(queue.get(), timeout=min(remaining, 1))
                yield f"event: step\ndata: {json.dumps(item['data'], default=str)}\n\n"
            except asyncio.TimeoutError:
                continue
        try:
            while not queue.empty():
                item = queue.get_nowait()
                yield f"event: step\ndata: {json.dumps(item['data'], default=str)}\n\n"
            result = future.result()
            result["graph"] = _trace_graph(result.get("trace", {}))
            yield f"event: result\ndata: {json.dumps(result, default=str)}\n\n"
        except Exception as exc:
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _trace_graph(trace: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    allowed = {"File", "PullRequest", "Issue", "Decision", "Person"}
    edges = list(trace.get("edges", []))
    ids = {str(value) for edge in edges for value in (edge.get("source"), edge.get("target"))}
    nodes = [node for node in trace.get("nodes", []) if str(node.get("id")) in ids and node.get("type") in allowed]
    nodes = nodes[:15]
    kept = {str(node.get("id")) for node in nodes}
    edges = [edge for edge in edges if str(edge.get("source")) in kept and str(edge.get("target")) in kept]
    return {"nodes": nodes, "edges": edges[:30]}


@app.get("/api/graph/neighborhood")
async def api_neighborhood(path: str, limit: int = 40, owner: str = "") -> dict[str, Any]:
    result = await asyncio.to_thread(gt.neighborhood, path, limit, owner)
    return JSONResponse(result)


@app.get("/api/graph/bus-factor")
async def api_bus_factor_graph(path: str, owner: str, prs: str = "") -> dict[str, Any]:
    numbers = [int(value) for value in prs.split(",") if value.strip().isdigit()]
    result = await asyncio.to_thread(gt.bus_factor_neighborhood, path, owner, numbers)
    return JSONResponse(result)


@app.get("/api/bus-factor")
async def api_bus_factor() -> dict[str, Any]:
    result = await asyncio.to_thread(analysis.bus_factor)
    return JSONResponse(result)


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    """Health check endpoint."""
    try:
        db_ok = await asyncio.to_thread(db.ping)
        return {
            "status": "ok",
            "database": "connected" if db_ok else "disconnected",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "database": "error"
        }


@app.get("/api/stats")
async def api_stats() -> dict[str, Any]:
    s = await asyncio.to_thread(db.stats)
    return {
        "nodes": s["nodes"],
        "edges": s["edges"],
        "nodes_by_label": s["nodes_by_label"],
        "edges_by_type": s["edges_by_type"],
        "graph": s["graph"],
        "repo": config.REPO,
        "guard_repo": config.GUARD_REPO,
        "dry_run": config.DRY_RUN,
        "mcp_mode": guardian.mcp_mode(),
        "llm_model": config.LLM_MODEL,
    }


@app.post("/api/ingest")
async def api_ingest(repo: str | None = None, limit: int | None = None) -> dict[str, Any]:
    result = await asyncio.to_thread(ingest.ingest, repo, limit)
    return JSONResponse(result)


@app.post("/api/extract")
async def api_extract(repo: str | None = None, limit: int | None = None) -> dict[str, Any]:
    count = await asyncio.to_thread(extract.extract, repo, limit)
    return {"decisions_stored": count}


@app.get("/api/examples")
async def api_examples() -> dict[str, Any]:
    return {"questions": await asyncio.to_thread(agent.example_questions)}


@app.get("/")
async def root():
    return FileResponse(config.STATIC_DIR / "index.html")


if config.STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


def main() -> int:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
