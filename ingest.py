"""Phase 1: ingest a public GitHub repo into FalkorDB.

Pulls the most recent merged PRs (with files, commits, reviews, review
comments), resolves "fixes #N" / "mentions #N" references to issues (with
their comments), and loads people/PRs/commits/issues/files into the graph.

Run:  python ingest.py
Re-running is safe: every write is a MERGE, and every API response is cached
under .cache/gh/ so a second run never hits the rate limit.
"""

from __future__ import annotations

import re
import sys
from typing import Any, Iterable

import config
import db
import graph_tools as gt
from github_client import GitHub

FIX_RE = re.compile(r"\b(fixes|fix|closes|close|resolves|resolve)\s+#(\d+)", re.IGNORECASE)
REF_RE = re.compile(r"#(\d+)")


def _login(user: dict[str, Any] | None, fallback: str = "") -> str | None:
    if user and user.get("login"):
        return user["login"]
    return fallback or None


def _refs(text: str, self_number: int) -> tuple[set[int], set[int]]:
    """Return (fixed_issue_numbers, mentioned_issue_numbers), excluding self."""
    text = text or ""
    fixes = {int(m.group(2)) for m in FIX_RE.finditer(text)}
    all_refs = {int(m.group(1)) for m in REF_RE.finditer(text)}
    mentions = all_refs - fixes
    fixes.discard(self_number)
    mentions.discard(self_number)
    return fixes, mentions


def collect_bundle(gh: GitHub, repo: str, pr: dict[str, Any]) -> dict[str, Any]:
    """Fetch everything we need for one PR."""
    number = pr["number"]
    body = pr.get("body") or ""
    fixes, mentions = _refs(body, number)

    bundle: dict[str, Any] = {
        "pr": pr,
        "files": gh.pr_files(repo, number),
        "commits": gh.pr_commits(repo, number),
        "reviews": gh.pr_reviews(repo, number),
        "review_comments": gh.pr_review_comments(repo, number),
        "issues": {},
    }
    # Also scan the title for references.
    t_fixes, t_mentions = _refs(pr.get("title") or "", number)
    fixes |= t_fixes
    mentions |= t_mentions - fixes
    bundle["fixes"] = fixes
    bundle["mentions"] = mentions

    for inum in sorted(fixes | mentions):
        issue = gh.issue(repo, inum)
        comments = gh.issue_comments(repo, inum) if issue else []
        bundle["issues"][inum] = {"issue": issue, "comments": comments}
    return bundle


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def _chunks(rows: list[dict], size: int = 200) -> Iterable[list[dict]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def load_bundle(bundle: dict[str, Any], repo: str) -> None:
    pr = bundle["pr"]
    number = pr["number"]
    author = _login(pr.get("user"))
    merged_at = pr.get("merged_at") or ""

    # PR node
    db.run(
        """
        MERGE (pr:PullRequest {number: $number})
        SET pr.title = $title, pr.body = $body, pr.merged_at = $merged_at,
            pr.url = $url, pr.repo = $repo
        """,
        {
            "number": number,
            "title": pr.get("title") or "",
            "body": (pr.get("body") or "")[:20000],
            "merged_at": merged_at,
            "url": pr.get("html_url") or "",
            "repo": repo,
        },
    )
    if author:
        db.run(
            """
            MERGE (p:Person {login: $login})
            WITH p
            MATCH (pr:PullRequest {number: $number})
            MERGE (p)-[:AUTHORED]->(pr)
            """,
            {"login": author, "number": number},
        )

    # Files
    file_rows = [
        {
            "path": f.get("filename"),
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
            "repo": repo,
        }
        for f in bundle["files"]
        if f.get("filename")
    ]
    for chunk in _chunks(file_rows):
        db.run(
            """
            UNWIND $rows AS row
            MERGE (pr:PullRequest {number: $number})
            MERGE (f:File {path: row.path})
            SET f.repo = row.repo
            MERGE (pr)-[t:TOUCHES]->(f)
            SET t.additions = row.additions, t.deletions = row.deletions
            """,
            {"number": number, "rows": chunk},
        )

    # Commits
    commit_rows = []
    for c in bundle["commits"]:
        sha = c.get("sha")
        if not sha:
            continue
        meta = c.get("commit", {}) or {}
        c_author = meta.get("author", {}) or {}
        commit_rows.append(
            {
                "sha": sha,
                "message": (meta.get("message") or "")[:1000],
                "date": c_author.get("date") or "",
                "login": _login(c.get("author"), (c_author.get("name") or "unknown")),
                "repo": repo,
            }
        )
    for chunk in _chunks(commit_rows):
        db.run(
            """
            UNWIND $rows AS row
            MERGE (pr:PullRequest {number: $number})
            MERGE (c:Commit {sha: row.sha})
            SET c.message = row.message, c.date = row.date, c.repo = row.repo
            MERGE (pr)-[:CONTAINS]->(c)
            MERGE (p:Person {login: row.login})
            MERGE (p)-[:AUTHORED]->(c)
            """,
            {"number": number, "rows": chunk},
        )

    # Reviews -> REVIEWED with comment_count
    counts: dict[str, int] = {}
    for rc in bundle["review_comments"]:
        login = _login(rc.get("user"))
        if login:
            counts[login] = counts.get(login, 0) + 1
    for r in bundle["reviews"]:
        login = _login(r.get("user"))
        if login and r.get("body"):
            counts[login] = counts.get(login, 0) + 1
    review_rows = [{"login": k, "count": v} for k, v in counts.items() if k != author]
    for chunk in _chunks(review_rows):
        db.run(
            """
            UNWIND $rows AS row
            MERGE (pr:PullRequest {number: $number})
            MERGE (p:Person {login: row.login})
            MERGE (p)-[r:REVIEWED]->(pr)
            SET r.comment_count = row.count
            """,
            {"number": number, "rows": chunk},
        )

    # Linked issues (with comments) -> Issue nodes
    issue_rows = []
    for inum, data in bundle["issues"].items():
        issue = data["issue"] or {}
        issue_rows.append(
            {
                "number": inum,
                "title": issue.get("title") or f"Issue #{inum}",
                "body": (issue.get("body") or "")[:20000],
                "url": issue.get("html_url") or f"https://github.com/{repo}/issues/{inum}",
                "is_pr": bool(issue.get("pull_request")),
                "comments": "\n---\n".join(
                    (c.get("body") or "")[:4000] for c in data["comments"] if c.get("body")
                )[:20000],
                "repo": repo,
            }
        )
    for chunk in _chunks(issue_rows):
        db.run(
            """
            UNWIND $rows AS row
            MERGE (pr:PullRequest {number: $number})
            MERGE (i:Issue {number: row.number})
            SET i.title = row.title, i.body = row.body, i.url = row.url,
                i.comments = row.comments, i.repo = row.repo, i.is_pr = row.is_pr
            """,
            {"number": number, "rows": chunk},
        )

    fix_rows = [{"number": n} for n in sorted(bundle["fixes"]) if n in bundle["issues"]]
    mention_rows = [{"number": n} for n in sorted(bundle["mentions"]) if n in bundle["issues"]]
    for rows, rel in ((fix_rows, "FIXES"), (mention_rows, "MENTIONS")):
        for chunk in _chunks(rows):
            db.run(
                f"""
                UNWIND $rows AS row
                MERGE (pr:PullRequest {{number: $number}})
                MERGE (i:Issue {{number: row.number}})
                MERGE (pr)-[:{rel}]->(i)
                """,
                {"number": number, "rows": chunk},
            )


def ingest(repo: str | None = None, limit: int | None = None) -> dict[str, Any]:
    repo = repo or config.REPO
    limit = limit or config.MAX_PRS
    gh = GitHub()

    db.init_schema()
    print(f"Ingesting {repo}: up to {limit} merged PRs (cache: {config.CACHE_DIR / 'gh'})")
    prs = gh.merged_prs(repo, limit)
    print(f"Found {len(prs)} merged PRs")

    for idx, pr in enumerate(prs, 1):
        try:
            bundle = collect_bundle(gh, repo, pr)
            load_bundle(bundle, repo)
        except Exception as err:
            print(f"  [{idx}/{len(prs)}] PR #{pr.get('number')} failed: {err}")
            continue
        if idx % 10 == 0 or idx == len(prs):
            s = db.stats()
            print(f"  [{idx}/{len(prs)}] PR #{pr['number']} loaded -- graph: {s['nodes']} nodes / {s['edges']} edges")

    scores, algorithm = gt.refresh_connector_scores()
    print(f"Stored connector scores for {len(scores)} people ({algorithm}).")

    s = db.stats()
    print(f"\nDone. {s['nodes']} nodes, {s['edges']} edges.")
    return s


def sanity_queries() -> None:
    checks = [
        ("top authors by PRs", """
            MATCH (p:Person)-[:AUTHORED]->(pr:PullRequest)
            RETURN p.login AS person, count(DISTINCT pr) AS prs
            ORDER BY prs DESC LIMIT 5
        """),
        ("most-changed files", """
            MATCH (pr:PullRequest)-[t:TOUCHES]->(f:File)
            RETURN f.path AS file, count(DISTINCT pr) AS touched
            ORDER BY touched DESC LIMIT 5
        """),
        ("PRs that fix issues", """
            MATCH (pr:PullRequest)-[:FIXES]->(i:Issue)
            RETURN pr.number AS pr, i.number AS issue, i.title AS title
            LIMIT 5
        """),
    ]
    print("\nSanity queries:")
    for label, q in checks:
        print(f"\n  {label}:")
        for row in db.run(q):
            print(f"    {row}")


def main() -> int:
    try:
        ingest()
    except Exception as err:
        print(f"ingest failed: {err}", file=sys.stderr)
        return 1
    sanity_queries()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
