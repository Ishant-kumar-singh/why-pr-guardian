"""Phase 6: the guardian agent.

The headline feature. When a PR is opened, the guardian finds past decisions
attached to the files the PR changes, checks whether the PR undoes one,
estimates blast radius, picks reviewers, and posts a review comment with
evidence — or says nothing when there's no conflict.

Input: PR number on GUARD_REPO (the demo fork).
Output: {steps, trace, drafted_comment, reviewers, posted}.

Run:  python guardian.py <pr_number>
"""

from __future__ import annotations

import json
import re
import sys
import asyncio
import os
import re
import textwrap
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

import config
import db
import graph_tools as gt
import llm
from github_client import GitHub

MAX_STEPS = 8
GUARDIAN_TIMEOUT = 120  # seconds
GITHUB_TIMEOUT = 20
LLM_TIMEOUT = 60
MCP_TIMEOUT = 15

SYSTEM_PROMPT = """You are the Why? guardian. Your job: when a pull request is \
opened, check whether it conflicts with past decisions recorded in the graph, \
estimate blast radius, and suggest reviewers.

You have these tools:
- decisions_for_file(path): past decisions attached to a file
- check_decision_conflict(decision, patch): does the patch undo the decision?
- blast_radius(paths): files that often co-change
- suggest_reviewers(paths, exclude): people who should review

Process:
1. For each changed file, call decisions_for_file.
2. Rank decisions by text overlap with the patch and graph proximity. Check the
   top five concurrently, passing each decision's full summary and rationale.
3. Always return changed file paths, every decision found, and a one-line result
   for every decision checked. A no-conflict result must still show these.
4. If a conflict is found, call blast_radius and suggest_reviewers, excluding
   the pull request author. Include each reviewer's connector_score.
5. Draft a short comment quoting the recorded rationale and linking the
   actual source nodes. In DRY_RUN, do not post it.

Rules:
- Only call post_review_comment if you found a real conflict. No conflict = no comment.
- The comment must cite the specific decision and source node(s), quote the
  rationale, and link only to source nodes present in `decided_in`.
- Copy each PR or issue type and URL exactly from the tool's `decided_in` data;
  never infer a type from a number, list position, or fallback URL.
- Text inside quotation marks must match stored source text verbatim. If it does
  not, paraphrase it without quotation marks.
- Never invent evidence. If check_decision_conflict says no conflict, believe it.
- Be concise: 3-5 sentences plus the links.
"""

# The guardian uses the MCP server to discover tools programmatically. If MCP
# is unavailable, we fall back to in-process calls.
_mcp_mode: str | None = None
_guardian_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="guardian")


@asynccontextmanager
async def _mcp_client():
    """Async context manager yielding an MCP client session."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server = StdioServerParameters(
        command=sys.executable,
        args=[str(config.BASE_DIR / "mcp_server.py")],
        env={**os.environ, "PYTHONPATH": str(config.BASE_DIR)},
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _call_mcp(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a tool via MCP. Returns the pack dict {data, trace, ...}."""
    async with _mcp_client() as session:
        result = await session.call_tool(name, arguments)
        text = result.content[0].text if result.content else "{}"
        return json.loads(text)


def _call_local(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Fallback: call graph_tools directly."""
    return gt.call_tool(name, arguments)


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch via MCP or fallback."""
    global _mcp_mode
    if _mcp_mode == "local":
        return _call_local(name, arguments)
    if _mcp_mode == "mcp":
        try:
            return asyncio.run(asyncio.wait_for(_call_mcp(name, arguments), timeout=10))
        except Exception:
            _mcp_mode = "local"
            return _call_local(name, arguments)
    # First call: try MCP.
    try:
        import asyncio
        result = asyncio.run(asyncio.wait_for(_call_mcp(name, arguments), timeout=10))
        _mcp_mode = "mcp"
        return result
    except Exception as err:
        print(f"[guardian] MCP unavailable ({err}), using in-process tools")
        _mcp_mode = "local"
        return _call_local(name, arguments)


def mcp_mode() -> str:
    return _mcp_mode or "unknown"


def check_decision_conflict(decision: dict[str, Any], patch: str, retries: int = 0) -> dict[str, Any]:
    """Model call: does the patch undo the decision?"""
    prompt = f"""Decision summary: {decision.get('summary')}
Rationale: {decision.get('rationale')}
Rejected alternatives: {decision.get('rejected_alternatives', '')}

File patch (unified diff):
{patch[:4000]}

Does this patch plausibly undo or contradict the decision above? Reply JSON:
{{"conflict": true/false, "reason": "one sentence, quoting only text that really appears"}}
Set conflict=true only when the patch changes or contradicts the decision's subject. If your reason says the change is unrelated, does not affect the subject, or does not contradict the decision, conflict must be false. Be conservative: when unsure, reply {{"conflict": false, "reason": "insufficient evidence"}}.
"""
    try:
        data = llm.chat_json([{"role": "user", "content": prompt}], max_tokens=500, retries=retries)
        conflict = bool(data.get("conflict"))
        reason = data.get("reason", "")
        # Reject self-contradictory model output: an explanation that explicitly
        # says the patch is unrelated cannot support a positive conflict flag.
        if conflict and re.search(r"\b(?:unrelated to|not related to|does not (?:touch|affect|contradict)|doesn't (?:touch|affect|contradict))\b", reason, re.IGNORECASE):
            conflict = False
        return {
            "data": {
                "conflict": conflict,
                "reason": reason,
                "decision_id": decision.get("id"),
            },
            "trace": {"nodes": [], "edges": []},
        }
    except ValueError as exc:
        return {
            "data": {"conflict": False, "reason": "model did not return valid JSON", "_error": str(exc)},
            "trace": {"nodes": [], "edges": []},
        }


def post_review_comment(pr_number: int, body: str, repo: str) -> dict[str, Any]:
    """Create or update the prior Guardian review summary on this PR."""
    if not config.GUARD_REPO or repo != config.GUARD_REPO:
        return {
            "data": {"error": f"refusing to write to {repo} (only {config.GUARD_REPO} allowed)"},
            "trace": {"nodes": [], "edges": []},
        }
    if config.DRY_RUN:
        return {
            "data": {"dry_run": True, "comment": body},
            "trace": {"nodes": [], "edges": []},
        }
    gh = GitHub()
    # Review summaries are editable, including after submission. Find a
    # previous Guardian comment by this authenticated user before creating one.
    user_resp = gh._client.get("/user", headers=gh._headers())
    reviews_resp = gh._client.get(f"/repos/{repo}/pulls/{pr_number}/reviews", headers=gh._headers())
    if user_resp.status_code < 400 and reviews_resp.status_code < 400:
        login = (user_resp.json() or {}).get("login")
        reviews = reviews_resp.json() if isinstance(reviews_resp.json(), list) else []
        for review in reversed(reviews):
            old_body = str(review.get("body") or "")
            if review.get("user", {}).get("login") == login and (
                "psf/requests#7437-0" in old_body or "<!-- why-guardian:" in old_body or
                ("decision recorded in PR #" in old_body and "Also worth checking:" in old_body)
            ):
                review_id = review.get("id")
                update = gh._client.put(
                    f"/repos/{repo}/pulls/{pr_number}/reviews/{review_id}",
                    json={"body": body}, headers=gh._headers(),
                )
                if update.status_code >= 400:
                    return {"data": {"error": f"GitHub API {update.status_code}: {update.text[:300]}", "updated": False}, "trace": {"nodes": [], "edges": []}}
                return {"data": {"posted": True, "updated": True, "url": update.json().get("html_url")}, "trace": {"nodes": [], "edges": []}}
    url = f"/repos/{repo}/pulls/{pr_number}/reviews"
    resp = gh._client.post(
        url,
        json={"event": "COMMENT", "body": body},
        headers=gh._headers(),
    )
    if resp.status_code >= 400:
        return {
            "data": {"error": f"GitHub API {resp.status_code}: {resp.text[:300]}"},
            "trace": {"nodes": [], "edges": []},
        }
    return {
        "data": {"posted": True, "updated": False, "url": resp.json().get("html_url")},
        "trace": {"nodes": [], "edges": []},
    }


GUARDIAN_COMMENT_MARKER = "<!-- why-guardian -->"


def post_guardian_comment(pr_number: int, body: str, repo: str) -> dict[str, Any]:
    """Create or update the marked plain conversation comment in GUARD_REPO."""
    if not config.GUARD_REPO or repo != config.GUARD_REPO:
        return {"data": {"error": f"refusing to write to {repo} (only {config.GUARD_REPO} allowed)"}}
    if config.DRY_RUN:
        return {"data": {"error": "DRY_RUN is true; cannot post"}}

    # Prevent GitHub mentions while preserving a readable login in markdown.
    clean = re.sub(r"(?<![\w`])@([A-Za-z0-9-]+)", r"`\1`", body.strip())
    clean = clean.replace(GUARDIAN_COMMENT_MARKER, "").strip()
    max_content = 4000 - len(GUARDIAN_COMMENT_MARKER) - 2
    clean = clean[:max_content].rstrip()
    safe_body = f"{GUARDIAN_COMMENT_MARKER}\n\n{clean}"

    gh = GitHub()
    comments_resp = gh._client.get(
        f"/repos/{repo}/issues/{pr_number}/comments", headers=gh._headers()
    )
    if comments_resp.status_code >= 400:
        return {"data": {"error": f"GitHub API {comments_resp.status_code}: {comments_resp.text[:300]}", "posted": False}}
    comments = comments_resp.json() if isinstance(comments_resp.json(), list) else []
    existing = next((comment for comment in reversed(comments)
                     if GUARDIAN_COMMENT_MARKER in str(comment.get("body") or "")), None)
    if existing:
        response = gh._client.patch(
            f"/repos/{repo}/issues/comments/{existing['id']}",
            json={"body": safe_body}, headers=gh._headers(),
        )
    else:
        response = gh._client.post(
            f"/repos/{repo}/issues/{pr_number}/comments",
            json={"body": safe_body}, headers=gh._headers(),
        )
    if response.status_code >= 400:
        return {"data": {"error": f"GitHub API {response.status_code}: {response.text[:300]}", "posted": False}}
    payload = response.json()
    return {"data": {
        "posted": True,
        "updated": bool(existing),
        "id": payload.get("id"),
        "url": payload.get("html_url"),
        "body": payload.get("body"),
    }}


def find_guardian_comment(pr_number: int, repo: str) -> dict[str, Any]:
    """Read the existing marked conversation comment, if one exists."""
    if not config.GUARD_REPO or repo != config.GUARD_REPO:
        return {"data": {"error": f"reads are restricted to GUARD_REPO ({config.GUARD_REPO})"}}
    gh = GitHub()
    response = gh._client.get(
        f"/repos/{repo}/issues/{pr_number}/comments", headers=gh._headers()
    )
    if response.status_code >= 400:
        return {"data": {"error": f"GitHub API {response.status_code}: {response.text[:300]}"}}
    comments = response.json() if isinstance(response.json(), list) else []
    match = next((comment for comment in reversed(comments)
                  if GUARDIAN_COMMENT_MARKER in str(comment.get("body") or "")), None)
    return {"data": {
        "posted": bool(match),
        "id": match.get("id") if match else None,
        "url": match.get("html_url") if match else None,
    }}


def request_reviewers(pr_number: int, logins: list[str], repo: str) -> dict[str, Any]:
    """Request reviewers (action tool)."""
    if not config.GUARD_REPO or repo != config.GUARD_REPO:
        return {
            "data": {"error": f"refusing to write to {repo}"},
            "trace": {"nodes": [], "edges": []},
        }
    if config.DRY_RUN:
        return {
            "data": {"dry_run": True, "reviewers": logins},
            "trace": {"nodes": [], "edges": []},
        }
    gh = GitHub()
    url = f"/repos/{repo}/pulls/{pr_number}/requested_reviewers"
    resp = gh._client.post(url, json={"reviewers": logins}, headers=gh._headers())
    if resp.status_code >= 400:
        return {
            "data": {"error": f"GitHub API {resp.status_code}: {resp.text[:200]}"},
            "trace": {"nodes": [], "edges": []},
        }
    return {"data": {"requested": True}, "trace": {"nodes": [], "edges": []}}


def _tool_specs() -> list[dict[str, Any]]:
    """Tool specs for the guardian agent."""
    return [
        {
            "type": "function",
            "function": {
                "name": "decisions_for_file",
                "description": "Past decisions attached to a file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "check_decision_conflict",
                "description": "Check if a patch undoes a recorded decision. Returns {conflict, reason}.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "decision": {"type": "object", "description": "decision dict from decisions_for_file"},
                        "patch": {"type": "string", "description": "unified diff patch"},
                    },
                    "required": ["decision", "patch"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "blast_radius",
                "description": "Files that often co-change with the given files.",
                "parameters": {
                    "type": "object",
                    "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
                    "required": ["paths"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "suggest_reviewers",
                "description": "People who should review changes to these files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paths": {"type": "array", "items": {"type": "string"}},
                        "exclude": {"type": "string", "description": "login to exclude"},
                    },
                    "required": ["paths"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "post_review_comment",
                "description": "Post a review comment on the PR. Only call if a real conflict was found.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pr_number": {"type": "integer"},
                        "body": {"type": "string"},
                        "repo": {"type": "string"},
                    },
                    "required": ["pr_number", "body", "repo"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "request_reviewers",
                "description": "Request reviewers on the PR.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pr_number": {"type": "integer"},
                        "logins": {"type": "array", "items": {"type": "string"}},
                        "repo": {"type": "string"},
                    },
                    "required": ["pr_number", "logins", "repo"],
                },
            },
        },
    ]


def _guard_impl(pr_number: int, repo: str | None = None, on_step=None) -> dict[str, Any]:
    """Run a bounded, streaming evidence pass for a PR."""
    repo = repo or config.GUARD_REPO
    if repo != config.GUARD_REPO:
        return {"error": f"Guardian reads are restricted to GUARD_REPO ({config.GUARD_REPO})", "steps": []}
    gh = GitHub()
    pr = gh.pull(repo, pr_number)
    if not pr:
        return {"error": f"PR {pr_number} not found in GUARD_REPO ({repo})", "steps": []}

    files = gh.pr_files(repo, pr_number)
    author = pr.get("user", {}).get("login") if pr.get("user") else None
    changed = [item.get("filename") for item in files if item.get("filename")]
    patches = {item.get("filename"): item.get("patch", "") for item in files if item.get("filename")}
    steps: list[dict[str, Any]] = []

    def record(name: str, args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        step = {
            "tool": name,
            "arguments": args,
            "summary": _summarize(name, result.get("data")),
            "mcp_mode": mcp_mode(),
            "data": result.get("data"),
            "nodes": result.get("trace", {}).get("nodes", []),
            "edges": result.get("trace", {}).get("edges", []),
        }
        steps.append(step)
        if on_step:
            on_step(step)
        return step

    record("changed_files", {"pr_number": pr_number}, {
        "data": {"files": changed, "author": author}, "trace": {"nodes": [], "edges": []}
    })

    decisions_by_file: dict[str, list[dict[str, Any]]] = {}
    for path in changed:
        args = {"path": path}
        step = record("decisions_for_file", args, call_tool("decisions_for_file", args))
        decisions_by_file[path] = (step.get("data") or {}).get("decisions", [])

    all_decisions: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for path, decisions in decisions_by_file.items():
        for decision in decisions:
            decision_id = str(decision.get("id") or "")
            if decision_id and decision_id not in seen:
                seen.add(decision_id)
                all_decisions.append((path, decision))

    def overlap(item: tuple[str, dict[str, Any]]) -> int:
        path, decision = item
        patch_terms = set(re.findall(r"[a-z0-9_]+", patches.get(path, "").lower()))
        decision_terms = set(re.findall(
            r"[a-z0-9_]+",
            (decision.get("summary", "") + " " + decision.get("rationale", "")).lower(),
        ))
        graph_proximity = 1 + len(decision.get("decided_in", []))
        return len(patch_terms & decision_terms) + graph_proximity * 2

    # Rank by patch overlap plus proximity to a source PR/issue, then check the
    # five best candidates concurrently. Each future receives the full object.
    ranked = sorted(all_decisions, key=overlap, reverse=True)
    checks: list[dict[str, Any]] = []
    check_results: dict[str, dict[str, Any]] = {}
    selected = ranked[:5]
    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="decision-check") as executor:
        futures = {
            executor.submit(check_decision_conflict, decision, patches.get(path, "")): (path, decision)
            for path, decision in selected
        }
        for future in as_completed(futures):
            path, decision = futures[future]
            try:
                result = future.result()
                check_data = result.get("data", {})
            except Exception as exc:
                check_data = {"conflict": False, "reason": f"Conflict check unavailable: {type(exc).__name__}: {exc}", "decision_id": decision.get("id")}
                result = {"data": check_data, "trace": {"nodes": [], "edges": []}}
            check_results[str(decision.get("id"))] = check_data
    for path, decision in selected:
        check_data = check_results[str(decision.get("id"))]
        if check_data.get("_error"):
            # Keep the primary five calls concurrent, then retry malformed
            # model JSON one at a time so a local gateway is not overloaded.
            retry = check_decision_conflict(decision, patches.get(path, ""), retries=1)
            check_data = retry.get("data", check_data)
            if check_data.get("_error"):
                check_data = {
                    "conflict": False,
                    "reason": "The conflict check returned invalid JSON after a retry; treated conservatively as no conflict.",
                }
            check_results[str(decision.get("id"))] = check_data
        result = {"data": check_data, "trace": {"nodes": [], "edges": []}}
        record("check_decision_conflict", {"decision": decision, "path": path}, result)
        checks.append({
            "path": path,
            "decision_id": decision.get("id"),
            "summary": decision.get("summary", ""),
            "rationale": decision.get("rationale", ""),
            "decided_in": decision.get("decided_in", []),
            "conflict": bool(check_data.get("conflict")),
            "reason": check_data.get("reason", ""),
        })

    conflicts = [check for check in checks if check["conflict"]]
    blast_radius: list[dict[str, Any]] = []
    reviewer_details: list[dict[str, Any]] = []
    reviewers: list[str] = []
    drafted_comment = None
    posted = False
    post_result: dict[str, Any] = {}
    reviewer_request_result: dict[str, Any] = {}
    if conflicts:
        args = {"paths": changed}
        result = call_tool("blast_radius", args)
        record("blast_radius", args, result)
        blast_radius = (result.get("data") or {}).get("files", [])

        args = {"paths": changed, "exclude": author}
        result = call_tool("suggest_reviewers", args)
        record("suggest_reviewers", args, result)
        reviewer_details = [
            reviewer for reviewer in (result.get("data") or {}).get("reviewers", [])
            if reviewer.get("login") and reviewer.get("login") != author
        ][:3]
        reviewers = [r["login"] for r in reviewer_details if r.get("login") and r.get("login") != author]

        check = conflicts[0]
        decision = next(item for _, item in all_decisions if item.get("id") == check["decision_id"])
        rationale = decision.get("rationale") or check["reason"] or "The patch changes a previously recorded decision."
        source_links = [
            f"[{('PR' if source['type'] == 'PullRequest' else 'issue')} #{source['number']}]({source['url']})"
            for source in check.get("decided_in", [])
            if source.get("type") in {"PullRequest", "Issue"} and source.get("number") is not None and source.get("url")
        ]
        primary_source = next((source for source in check.get("decided_in", []) if source.get("type") == "PullRequest" and source.get("number") is not None), None)
        source_phrase = f"the decision recorded in PR #{primary_source['number']}" if primary_source else "the recorded decision"
        source_links_md = " · ".join(source_links)
        rationale_quote = "\n".join("> " + line for line in textwrap.wrap(str(rationale).strip(), width=105, break_long_words=False, break_on_hyphens=False))
        blast_paths = [f"`{item.get('path')}`" for item in blast_radius[:3] if item.get("path")]
        reviewer_names = [f"`{login}`" for login in reviewers[:3]]
        drafted_comment = (
            f"This patch may reverse {source_phrase} for `{check['path']}`. {source_links_md}\n\n"
            f"The recorded rationale:\n{rationale_quote}\n\n"
            f"Also worth checking: {', '.join(blast_paths) or 'no related files found'}.\n"
            f"Suggested reviewers: {', '.join(reviewer_names) or 'none returned'}."
        )
        # Post-check: preserve the block quote only when rejoining its lines
        # yields the source rationale exactly. Never publish internal IDs.
        quoted = " ".join(line[2:] for line in rationale_quote.splitlines())
        if quoted != str(rationale).strip() or str(check.get("decision_id", "")) in drafted_comment:
            drafted_comment = drafted_comment.replace(rationale_quote, str(rationale).strip()).replace(str(check.get("decision_id", "")), "")
        if config.AUTO_POST and not config.DRY_RUN:
            result = post_review_comment(pr_number, drafted_comment, repo)
            posted = bool((result.get("data") or {}).get("posted"))
            post_result = result.get("data", {})
            record("post_review_comment", {"pr_number": pr_number, "repo": repo, "body": drafted_comment}, result)
            result = request_reviewers(pr_number, reviewers, repo)
            reviewer_request_result = result.get("data", {})
            if "422" in str(reviewer_request_result.get("error", "")):
                reviewer_request_result = {
                    "skipped": True,
                    "message": "reviewer request skipped (not a collaborator on this repo); suggested reviewers are listed in the comment",
                    "error_detail": reviewer_request_result.get("error"),
                }
                result = {"data": reviewer_request_result, "trace": {"nodes": [], "edges": []}}
            record("request_reviewers", {"pr_number": pr_number, "repo": repo, "logins": reviewers}, result)
        else:
            post_result = {"drafted": True, "auto_post": False}
            reviewer_request_result = {"drafted": True, "auto_post": False}

    guardian_trace = _guardian_trace(pr, repo, changed, checks, blast_radius, reviewer_details)
    return {
        "pr_number": pr_number,
        "repo": repo,
        "title": pr.get("title"),
        "author": author,
        "steps": steps,
        "trace": guardian_trace,
        "changed_files": changed,
        "decisions_found": [
            {"path": path, **decision} for path, found in decisions_by_file.items() for decision in found
        ],
        "decision_checks": checks,
        "checked_decision_count": len(checks),
        "total_decision_count": len(all_decisions),
        "decision_check_note": f"Checked {len(checks)} of {len(all_decisions)} decisions found, ranked by text overlap plus graph proximity.",
        "blast_radius": blast_radius,
        "reviewers": reviewers,
        "reviewer_details": reviewer_details,
        "connector_algorithm": next((step.get("data", {}).get("connector_algorithm") for step in steps if step["tool"] == "suggest_reviewers"), None),
        "drafted_comment": drafted_comment,
        "posted": posted,
        "post_result": post_result,
        "reviewer_request_result": reviewer_request_result,
        "dry_run": config.DRY_RUN,
        "mcp_mode": mcp_mode(),
    }


def _truncate_words(value: Any, limit: int = 120) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit + 1)
    if cut <= 0:
        cut = text.find(" ", limit)
    return (text[:cut] if cut > 0 else text).rstrip() + "…" if cut > 0 else text


def _guardian_trace(pr: dict[str, Any], repo: str, changed: list[str], checks: list[dict[str, Any]],
                    blast_radius: list[dict[str, Any]], reviewers: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Build a bounded, action-specific Guardian graph from checked evidence."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    included: set[str] = set()
    limit = 15

    def add_node(node_id: str, kind: str, label: str, props: dict[str, Any] | None = None, **extra: Any) -> bool:
        if node_id in included:
            return True
        if len(nodes) >= limit:
            return False
        included.add(node_id)
        nodes.append({"id": node_id, "type": kind, "label": _truncate_words(label, 45), "props": props or {}, **extra})
        return True

    def add_edge(source: str, target: str, kind: str) -> None:
        if source in included and target in included:
            edges.append({"source": source, "target": target, "type": kind, "props": {}})

    number = pr.get("number")
    author = (pr.get("user") or {}).get("login")
    review_id = f"PullRequest:{repo}#{number}"
    add_node(review_id, "PullRequest", f"#{number}", {"number": number, "title": pr.get("title"), "url": pr.get("html_url"), "author": author})

    file_ids: list[str] = []
    for path in changed[:3]:
        file_id = f"File:{path}"
        file_ids.append(file_id)
        add_node(file_id, "File", path.replace("\\", "/").rsplit("/", 1)[-1], {"path": path, "title": path})
        add_edge(review_id, file_id, "TOUCHES")

    decision_ids: dict[str, str] = {}
    for check in checks:
        did = str(check.get("decision_id") or "")
        if not did:
            continue
        decision_id = f"Decision:{did}"
        decision_ids[did] = decision_id
        conflicted = bool(check.get("conflict"))
        if not add_node(decision_id, "Decision", check.get("summary", ""), {
            "summary": check.get("summary", ""), "rationale": check.get("rationale", ""),
        }, conflict=conflicted):
            continue
        for file_id in file_ids:
            if file_id == f"File:{check.get('path')}":
                add_edge(decision_id, file_id, "AFFECTS")
        add_edge(review_id, decision_id, "MAY_UNDO" if conflicted else "CHECKED_DECISION")

    conflict_checks = [check for check in checks if check.get("conflict")]
    for check in conflict_checks:
        decision_id = decision_ids.get(str(check.get("decision_id")))
        if not decision_id:
            continue
        for source in check.get("decided_in", []):
            kind = source.get("type")
            source_number = source.get("number")
            if kind not in {"PullRequest", "Issue"} or source_number is None:
                continue
            source_id = f"{kind}:{source_number}"
            title = source.get("title") or f"#{source_number}"
            source_label = f"issue #{source_number}" if kind == "Issue" else f"#{source_number}"
            add_node(source_id, kind, source_label, {"number": source_number, "title": title, "url": source.get("url")})
            add_edge(decision_id, source_id, "DECIDED_IN")

    for person in reviewers[:3]:
        login = person.get("login")
        if not login:
            continue
        person_id = f"Person:{login}"
        if add_node(person_id, "Person", login, {"login": login, "score": person.get("score"), "connector_score": person.get("connector_score")}):
            add_edge(review_id, person_id, "SUGGESTED_REVIEWER")

    for item in blast_radius[:3]:
        path = item.get("path")
        if not path:
            continue
        file_id = f"File:{path}"
        if add_node(file_id, "File", path.replace("\\", "/").rsplit("/", 1)[-1], {"path": path, "title": path}):
            for changed_id in file_ids:
                add_edge(changed_id, file_id, "CO_CHANGED_WITH")

    return {"nodes": nodes, "edges": edges[:30]}


def guard(pr_number: int, repo: str | None = None, on_step=None) -> dict[str, Any]:
    """Run blocking work with a hard 120-second wall-clock deadline."""
    future = _guardian_executor.submit(_guard_impl, pr_number, repo, on_step)
    try:
        return future.result(timeout=GUARDIAN_TIMEOUT)
    except TimeoutError:
        future.cancel()
        return {"error": f"Guardian timed out after {GUARDIAN_TIMEOUT}s", "steps": []}


def guard_bounded(pr_number: int, repo: str | None = None, on_step=None) -> dict[str, Any]:
    """Compatibility alias used by the API."""
    return guard(pr_number, repo, on_step)


def _merge(steps: list[dict[str, Any]]) -> dict[str, Any]:
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}
    for step in steps:
        for n in step["nodes"]:
            nodes[n["id"]] = n
        for e in step["edges"]:
            edges[(e["source"], e["target"], e["type"])] = e
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def _summarize(tool: str, data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    if tool == "changed_files":
        paths = data.get("files", [])
        return "Changed files: " + (", ".join(paths) if paths else "none returned by GitHub")
    if tool == "decisions_for_file":
        items = data.get("decisions", [])
        described = []
        for decision in items[:8]:
            refs = ", ".join(f"{source.get('type')} #{source.get('number')}" for source in decision.get("decided_in", []))
            described.append(f"{_truncate_words(decision.get('summary', ''), 120)} ({refs or 'source not linked'})")
        return f"{data.get('path', 'file')}: " + ("; ".join(described) or "no decisions recorded")
    if tool == "check_decision_conflict":
        status = "conflict" if data.get("conflict") else "no conflict"
        reason = data.get("reason", "")
        return f"{status}: {reason}" if reason else status
    if tool == "blast_radius":
        paths = ", ".join(f.get("path", "") for f in data.get("files", [])[:5]) or "no related files"
        return f"Blast radius ({data.get('algorithm', 'graph')}): {paths}"
    if tool == "suggest_reviewers":
        return "Reviewers: " + (", ".join(
            f"{p.get('login', '')} (score {p.get('score', 0):.4f}, connector {p.get('connector_score', 0):.4f})"
            for p in data.get("reviewers", [])[:5]
        ) or "none found")
    if tool in ("post_review_comment", "request_reviewers"):
        if data.get("skipped") and data.get("message"):
            return str(data["message"])
        if data.get("error"):
            return str(data["error"])
        return "posted" if data.get("posted") or data.get("requested") else "dry-run"
    return ""


def main() -> int:
    config.require_llm_model()
    if len(sys.argv) < 2:
        print("usage: python guardian.py <pr_number>")
        return 2
    pr_number = int(sys.argv[1])
    result = guard(pr_number)
    if result.get("error"):
        print(result["error"])
        return 1
    print(f"\n=== Guardian for PR #{result['pr_number']}: {result['title']}")
    print(f"DRY_RUN={result['dry_run']}, MCP mode={mcp_mode()}\n")
    for step in result["steps"]:
        print(f"  {step['tool']}({json.dumps(step['arguments'], default=str)[:80]}) -> {step['summary']}")
    if result["drafted_comment"]:
        print(f"\nDrafted comment:\n{result['drafted_comment']}")
    if result.get("post_result", {}).get("url"):
        print(f"\nPosted comment URL: {result['post_result']['url']}")
    reviewer_result = result.get("reviewer_request_result") or {}
    if reviewer_result.get("skipped") and reviewer_result.get("message"):
        print(reviewer_result["message"])
    elif reviewer_result.get("error"):
        print(f"Reviewer request failed: {reviewer_result['error']}")
    elif reviewer_result.get("requested"):
        print("Reviewer request sent to GitHub.")
    if result["reviewers"]:
        print(f"\nSuggested reviewers: {', '.join(result['reviewers'])}")
    if not result["drafted_comment"]:
        print("\nNo conflict found; no comment drafted.")
    print(f"\nTraced: {len(result['trace']['nodes'])} nodes, {len(result['trace']['edges'])} edges")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
