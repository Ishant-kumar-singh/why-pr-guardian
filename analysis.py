"""Phase 4: bus factor analysis.

How concentrated is knowledge, per file and per top-level directory? The
share is the number of distinct qualifying merged PRs touching the file that
the top person authored or reviewed, divided by all qualifying merged PRs
touching it in the 365-day window. Each PR counts once per person.

Also surfaces the people who connect the most areas of the codebase via a
real graph algorithm in FalkorDB (betweenness centrality over weighted
COLLABORATES_WITH edges), with a networkx fallback.

Run:  python analysis.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import db
import graph_tools as gt
import config
from github_client import GitHub

WINDOW_DAYS = 365
FLAG_SHARE = 0.70
FLAG_MIN_PRS = 5


def _cutoff() -> str:
    """12 months before the most recent PR in the graph."""
    row = db.run_one("MATCH (pr:PullRequest) RETURN max(pr.merged_at) AS m")
    latest = row[0] if row and row[0] else datetime.now(timezone.utc).isoformat()
    try:
        ref = datetime.fromisoformat(str(latest).replace("Z", "+00:00"))
    except ValueError:
        ref = datetime.now(timezone.utc)
    return (ref - timedelta(days=WINDOW_DAYS)).isoformat()


def _is_noise_file(path: str) -> bool:
    """Check if a file is non-source (CI, config, lock, changelog, version)."""
    if not path:
        return True
    lower = path.lower()
    noise = [
        'changelog', 'changes', 'history', 'news', 'version', 'release',
        'lock', '.lock', 'package-lock', 'yarn.lock', 'poetry.lock',
        '.github/workflows', '.gitlab-ci', '.circleci', 
        '.pre-commit-config', 'pyproject.toml', 'setup.py', 'setup.cfg',
        '.gitignore', '.editorconfig', '.pylintrc', 'tox.ini'
    ]
    return any(n in lower for n in noise) or path.startswith('.')


def _rows(cutoff: str) -> list[list[Any]]:
    # Get file counts per PR to filter large PRs
    fc_rows = db.run(
        """
        MATCH (pr:PullRequest)-[:TOUCHES]->(f:File)
        WHERE pr.merged_at >= $cutoff
        RETURN pr.number, pr.title, count(DISTINCT f) AS fc
        """,
        {"cutoff": cutoff},
    )
    valid_prs = set()
    for num, title, fc in fc_rows:
        if fc > 15:
            continue
        if title and len(title) > 1 and title[0] == 'v' and title[1].isdigit():
            continue
        if title and ('bump' in title.lower() or 'release' in title.lower()):
            continue
        valid_prs.add(num)
    
    if not valid_prs:
        return []
    
    all_rows = db.run(
        """
        MATCH (pr:PullRequest)-[:TOUCHES]->(f:File)
        WHERE pr.number IN $prs
        OPTIONAL MATCH (a:Person)-[:AUTHORED]->(pr)
        OPTIONAL MATCH (r:Person)-[rev:REVIEWED]->(pr)
        RETURN DISTINCT f.path, pr.number, a.login, r.login
        """,
        {"prs": list(valid_prs)},
    )
    
    filtered = []
    for path, num, author, reviewer in all_rows:
        if author and _is_bot(author):
            author = None
        if reviewer and _is_bot(reviewer):
            reviewer = None
        filtered.append([path, num, author, reviewer])
    
    return filtered


def _is_bot(login: str) -> bool:
    name = login.lower()
    return name.endswith("[bot]") or name in {"copilot", "github-actions", "dependabot", "renovate", "greenkeeper"}


def _concentration(rows: list[list[Any]], key_fn) -> dict[str, dict[str, Any]]:
    """Aggregate contributions by file (or directory) and score concentration."""
    contributions: dict[str, dict[str, set[int]]] = {}
    prs: dict[str, set] = {}
    people_sets: dict[str, set] = {}
    
    for path, number, author, reviewer in rows:
        if not path or _is_noise_file(path):
            continue
        key = key_fn(path)
        bucket = contributions.setdefault(key, {})
        prs.setdefault(key, set()).add(number)
        people_sets.setdefault(key, set())
        
        if author and not _is_bot(author):
            bucket.setdefault(author, set()).add(number)
            people_sets[key].add(author)
        if reviewer and not _is_bot(reviewer) and reviewer != author:
            bucket.setdefault(reviewer, set()).add(number)
            people_sets[key].add(reviewer)

    out: dict[str, dict[str, Any]] = {}
    for key, raw_bucket in contributions.items():
        if not prs[key]:
            continue
        top_login, authored_or_reviewed = max(raw_bucket.items(), key=lambda kv: (len(kv[1]), kv[0]))
        top_count = len(authored_or_reviewed)
        out[key] = {
            "path": key,
            "prs": len(prs[key]),
            "top_person": top_login,
            "top_share": round(top_count / len(prs[key]), 3),
            "top_pr_count": top_count,
            "supporting_prs": sorted(authored_or_reviewed),
            "qualifying_prs": sorted(prs[key]),
            "window_days": WINDOW_DAYS,
            "people": len(people_sets[key]),
            "other_people": len(people_sets[key]) - 1,
        }
    return out


def _top_dir(path: str) -> str:
    parts = path.split("/")
    return parts[0] if len(parts) > 1 else "(root)"


def bus_factor() -> dict[str, Any]:
    cutoff = _cutoff()
    rows = _rows(cutoff)

    files = _concentration(rows, lambda p: p)
    dirs = _concentration(rows, _top_dir)

    flagged = [
        f
        for f in files.values()
        if f["top_share"] > FLAG_SHARE and f["prs"] >= FLAG_MIN_PRS
    ]
    flagged.sort(key=lambda f: (-f["top_share"], -f["prs"]))

    files_out = sorted(files.values(), key=lambda f: (-f["top_share"], -f["prs"]))
    dirs_out = sorted(dirs.values(), key=lambda f: (-f["top_share"], -f["prs"]))

    conn = gt.connectors(limit=10)
    audit = _sessions_pr_audit()
    return {
        "window_days": WINDOW_DAYS,
        "cutoff": cutoff,
        "share_definition": "distinct qualifying merged PRs authored or reviewed by the top person / all distinct qualifying merged PRs touching the file",
        "flag_rule": f"top_share > {FLAG_SHARE} and prs >= {FLAG_MIN_PRS}",
        "files": files_out[:200],
        "directories": dirs_out[:50],
        "flagged": flagged,
        "connectors": conn["data"]["people"],
        "connector_algorithm": conn["data"]["algorithm"],
        "sessions_pr_audit": audit,
    }


def _sessions_pr_audit(path: str = "src/requests/sessions.py") -> dict[str, Any]:
    """Compare distinct merged PRs in FalkorDB with cached GitHub file data."""
    row = db.run_one(
        """MATCH (pr:PullRequest)-[:TOUCHES]->(f:File {path: $path})
           WHERE pr.merged_at IS NOT NULL
           RETURN count(DISTINCT pr)""",
        {"path": path},
    )
    graph_count = int(row[0]) if row and row[0] is not None else 0
    gh = GitHub()
    prs = gh.merged_prs(config.REPO, config.MAX_PRS)
    cached_numbers = set()
    for pr in prs:
        number = pr.get("number")
        if not number or not pr.get("merged_at"):
            continue
        if any(item.get("filename") == path for item in gh.pr_files(config.REPO, number)):
            cached_numbers.add(number)
    return {
        "path": path,
        "graph_distinct_merged_prs": graph_count,
        "cached_github_distinct_merged_prs": len(cached_numbers),
        "cached_merged_prs_examined": len(prs),
    }


def main() -> int:
    result = bus_factor()
    print(f"window: last {result['window_days']} days (cutoff {result['cutoff'][:10]})")
    audit = result["sessions_pr_audit"]
    print(
        f"Audit {audit['path']}: {audit['graph_distinct_merged_prs']} distinct merged PRs in FalkorDB; "
        f"{audit['cached_github_distinct_merged_prs']} in cached GitHub data "
        f"({audit['cached_merged_prs_examined']} merged PRs examined)."
    )
    print(f"\nFlagged files (top_share > {FLAG_SHARE}, prs >= {FLAG_MIN_PRS}):")
    for f in result["flagged"][:20]:
        print(f"  {f['top_share']:.0%}  {f['top_person']:<20} {f['prs']} PRs   {f['path']}")
    print("\nMost concentrated directories:")
    for d in result["directories"][:10]:
        print(f"  {d['top_share']:.0%}  {d['top_person']:<20} {d['prs']} PRs   {d['path']}")
    print(f"\nConnectors ({result['connector_algorithm']}):")
    for p in result["connectors"][:10]:
        print(f"  {p['score']:.4f}  {p['login']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
