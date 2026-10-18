"""Phase 2: extract the *decisions* behind PR discussions.

For every PR that has a real discussion we ask the model for strict JSON: a
list of decisions, each with a one-sentence summary, the rationale, any
rejected alternatives, and which of the PR's files it affects.

Results become Decision nodes with DECIDED_IN (PR and optionally Issue) and
AFFECTS (File) edges. We are deliberately conservative: no real decision, no
node. Every model response is cached under .cache/decisions/<pr>.json.

Run:  python extract.py
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import config
import db
import llm
from github_client import GitHub

DECISION_CACHE: Path = config.CACHE_DIR / "decisions"
DECISION_CACHE.mkdir(parents=True, exist_ok=True)

MAX_WORKERS = 5
MAX_INPUT = 12000  # characters of discussion per call

SYSTEM_PROMPT = """You read a merged pull request and extract the *decisions* \
behind it: the deliberate choices that explain why the code is the way it is.

Return ONLY a JSON object of this exact shape:
{
  "decisions": [
    {
      "summary": "one sentence: what was decided",
      "rationale": "why, in the authors' own words where possible",
      "rejected_alternatives": "alternatives that were discussed and rejected, or ''",
      "affected_files": ["path/from/the/pr.py"],
      "issue_number": 1234
    }
  ]
}

Rules:
- Be conservative. If the PR is routine (a version bump, a typo, a dependency
  update) return {"decisions": []}.
- Only include a decision if there is real reasoning in the text. Do not invent.
- affected_files must come from the PR's changed-file list.
- issue_number is the linked issue this decision is discussed in, or 0 if none.
- No markdown, no commentary, JSON only."""


def _window(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def has_discussion(pr: dict[str, Any]) -> bool:
    body = pr.get("body") or ""
    review_comments = int(pr.get("_review_comment_count", 0) or 0)
    has_issue = bool(pr.get("_has_issue"))
    return len(body) > 200 or review_comments >= 2 or has_issue


def gather_discussion(gh: GitHub, repo: str, pr: dict[str, Any]) -> dict[str, Any]:
    """Collect PR text, review comments and linked issues for one PR."""
    number = pr["number"]
    comments = gh.pr_review_comments(repo, number)
    reviews = gh.pr_reviews(repo, number)
    files = gh.pr_files(repo, number)

    import ingest as _ingest  # local import to reuse the reference parser

    fixes, mentions = _ingest._refs(pr.get("body") or "", number)
    issue_texts = []
    for inum in sorted(fixes | mentions):
        issue = gh.issue(repo, inum)
        if not issue:
            continue
        icomments = gh.issue_comments(repo, inum)
        issue_texts.append(
            f"Issue #{inum}: {issue.get('title')}\n{(issue.get('body') or '')[:3000]}\n"
            + "\n".join((c.get("body") or "")[:1000] for c in icomments)
        )

    comment_texts = [f"{r.get('user', {}).get('login', '?')}: {r.get('body')}" for r in reviews if r.get("body")]
    comment_texts += [f"{c.get('user', {}).get('login', '?')}: {c.get('body')}" for c in comments if c.get("body")]

    return {
        "number": number,
        "title": pr.get("title") or "",
        "body": pr.get("body") or "",
        "comments": comment_texts,
        "issues": issue_texts,
        "files": [f.get("filename") for f in files if f.get("filename")],
        "linked": sorted(fixes | mentions),
    }


def build_prompt(disc: dict[str, Any]) -> list[dict[str, str]]:
    user = f"""PR #{disc['number']}: {disc['title']}

BODY:
{_window(disc['body'], 4000)}

CHANGED FILES:
{chr(10).join(disc['files'])}

DISCUSSION:
{_window(chr(10).join(disc['comments']), 4000)}

LINKED ISSUES:
{_window(chr(10).join(disc['issues']), 3500)}"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user[:MAX_INPUT]},
    ]


def extract_for_pr(gh: GitHub, repo: str, pr: dict[str, Any]) -> dict[str, Any]:
    number = pr["number"]
    cache_file = DECISION_CACHE / f"{number}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    disc = gather_discussion(gh, repo, pr)
    try:
        payload = llm.chat_json(build_prompt(disc), max_tokens=1500)
    except ValueError as err:
        print(f"  PR #{number}: model JSON error ({err}); skipping")
        payload = {"decisions": []}

    decisions = payload.get("decisions", []) if isinstance(payload, dict) else []
    if not isinstance(decisions, list):
        decisions = []

    result = {"number": number, "title": disc["title"], "files": disc["files"], "linked": disc["linked"], "decisions": decisions}
    cache_file.write_text(json.dumps(result), encoding="utf-8")
    return result


def _match_files(requested: list[str], pr_files: list[str]) -> list[str]:
    """Intersect model-provided paths with the PR's real file list."""
    if not requested:
        return []
    out = []
    for want in requested:
        if not isinstance(want, str):
            continue
        for path in pr_files:
            if path == want or path.endswith(want) or want.endswith(path):
                if path not in out:
                    out.append(path)
    return out


def store_decisions(result: dict[str, Any], repo: str) -> int:
    number = result["number"]
    stored = 0
    for i, dec in enumerate(result["decisions"]):
        if not isinstance(dec, dict):
            continue
        summary = (dec.get("summary") or "").strip()
        if not summary:
            continue
        did = f"{repo}#{number}-{i}"
        files = _match_files(dec.get("affected_files") or [], result["files"])
        if not files and result["files"]:
            # Fall back to the PR's files so the decision is reachable from code.
            files = result["files"]
        rejected = dec.get("rejected_alternatives")
        if isinstance(rejected, list):
            rejected = "; ".join(str(x) for x in rejected)

        db.run(
            """
            MERGE (d:Decision {id: $id})
            SET d.summary = $summary, d.rationale = $rationale,
                d.rejected_alternatives = $rejected, d.pr_number = $number,
                d.repo = $repo
            WITH d
            MATCH (pr:PullRequest {number: $number})
            MERGE (d)-[:DECIDED_IN]->(pr)
            """,
            {
                "id": did,
                "summary": summary,
                "rationale": (dec.get("rationale") or "").strip(),
                "rejected": (rejected or "").strip(),
                "number": number,
                "repo": repo,
            },
        )
        for path in files:
            db.run(
                """
                MATCH (d:Decision {id: $id})
                MERGE (f:File {path: $path})
                MERGE (d)-[:AFFECTS]->(f)
                """,
                {"id": did, "path": path},
            )
        inum = dec.get("issue_number") or 0
        if isinstance(inum, int) and inum in (result.get("linked") or []):
            db.run(
                """
                MATCH (d:Decision {id: $id})
                MERGE (i:Issue {number: $inum})
                MERGE (d)-[:DECIDED_IN]->(i)
                """,
                {"id": did, "inum": inum},
            )
        stored += 1
    return stored


def _candidates(repo: str) -> list[dict[str, Any]]:
    """PRs with real discussion, from the graph (so we only call the model when worthwhile)."""
    rows = db.run(
        """
        MATCH (pr:PullRequest)
        OPTIONAL MATCH (r:Person)-[rev:REVIEWED]->(pr)
        OPTIONAL MATCH (pr)-[:FIXES|MENTIONS]->(i:Issue)
        WITH pr, count(DISTINCT rev) AS reviewers, count(DISTINCT i) AS issues
        WHERE size(coalesce(pr.body, '')) > 200 OR reviewers >= 2 OR issues > 0
        RETURN pr.number AS number, pr.title AS title, pr.body AS body,
               reviewers, issues
        ORDER BY pr.number DESC
        """
    )
    out = []
    for number, title, body, reviewers, issues in rows:
        out.append(
            {
                "number": number,
                "title": title,
                "body": body or "",
                "_review_comment_count": 0,
                "_has_issue": issues > 0,
            }
        )
    return out


def extract(repo: str | None = None, limit: int | None = None) -> int:
    repo = repo or config.REPO
    gh = GitHub()
    cands = _candidates(repo)
    if limit:
        cands = cands[:limit]
    print(f"Extracting decisions for {len(cands)} PRs with discussion (workers={MAX_WORKERS})")

    total = 0
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(extract_for_pr, gh, repo, pr): pr for pr in cands}
        for fut in as_completed(futures):
            pr = futures[fut]
            try:
                result = fut.result()
                total += store_decisions(result, repo)
            except Exception as err:
                print(f"  PR #{pr['number']} failed: {err}")
            done += 1
            if done % 10 == 0:
                print(f"  {done}/{len(cands)} PRs processed, {total} decisions so far")

    print(f"\nStored {total} decisions.")
    return total


def show_samples(n: int = 5) -> None:
    rows = db.run(
        """
        MATCH (d:Decision)-[:DECIDED_IN]->(pr:PullRequest)
        RETURN d.summary, d.rationale, pr.number, pr.title
        LIMIT $n
        """,
        {"n": n},
    )
    print(f"\nSample decisions ({len(rows)}):")
    for summary, rationale, number, title in rows:
        print(f"\n  PR #{number}: {title}")
        print(f"    decision:  {summary}")
        print(f"    rationale: {(rationale or '')[:200]}")


def main() -> int:
    try:
        extract()
    except Exception as err:
        print(f"extract failed: {err}", file=sys.stderr)
        return 1
    show_samples()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
