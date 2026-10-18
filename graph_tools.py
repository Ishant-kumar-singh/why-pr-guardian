"""Shared graph tools.

These functions are the *only* way the agents talk to the database. Each one
runs a parameterised Cypher query and returns both the data and the exact
nodes and edges it touched, so every answer and every action can be explained
by the path the agent walked.

Used by agent.py (question agent), guardian.py and mcp_server.py.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

import config
import db


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _pack(rows: list[list[Any]], data: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bundle data + the traced subgraph."""
    nodes, edges = db.collect(rows)
    out = {"data": data, "trace": {"nodes": nodes, "edges": edges}}
    if extra:
        out.update(extra)
    return out


def _repo() -> str:
    return config.REPO


def _pr_url(number: int, repo: str | None = None) -> str:
    return f"https://github.com/{repo or _repo()}/pull/{number}"


def _issue_url(number: int, repo: str | None = None) -> str:
    return f"https://github.com/{repo or _repo()}/issues/{number}"


def _recency_weight(iso: str | None) -> float:
    """Half-life of one year: recent work counts more."""
    if not iso:
        return 0.5
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.5
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 86400.0)
    return 0.5 ** (age_days / 365.0)


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------
def find_file(query: str, limit: int = 10) -> dict[str, Any]:
    """Fuzzy-match a file path. Full-text index first, substring fallback."""
    q = (query or "").strip()
    rows = db.run(
        "CALL db.idx.fulltext.queryNodes('File', $q) YIELD node RETURN node LIMIT $limit",
        {"q": q, "limit": limit},
    )
    if not rows:
        rows = db.run(
            """
            MATCH (node:File)
            WHERE toLower(node.path) CONTAINS toLower($q)
            RETURN node LIMIT $limit
            """,
            {"q": q, "limit": limit},
        )
    matches = []
    for row in rows:
        node = row[0]
        matches.append({"path": node.properties.get("path", "")})
    return _pack(rows, {"query": q, "matches": matches})


def file_history(path: str, limit: int = 25) -> dict[str, Any]:
    """PRs that touched a file, newest first, with author and churn."""
    rows = db.run(
        """
        MATCH (pr:PullRequest)-[t:TOUCHES]->(f:File {path: $path})
        OPTIONAL MATCH (a:Person)-[:AUTHORED]->(pr)
        RETURN f, t, pr, a
        ORDER BY pr.merged_at DESC
        LIMIT $limit
        """,
        {"path": path, "limit": limit},
    )
    prs = []
    for f, t, pr, a in ((r[0], r[1], r[2], r[3]) for r in rows):
        prs.append(
            {
                "number": pr.properties.get("number"),
                "title": pr.properties.get("title"),
                "merged_at": pr.properties.get("merged_at"),
                "url": _pr_url(pr.properties.get("number")),
                "author": a.properties.get("login") if a else None,
                "additions": t.properties.get("additions"),
                "deletions": t.properties.get("deletions"),
            }
        )
    return _pack(rows, {"path": path, "prs": prs})


def decisions_for_file(path: str, limit: int = 20) -> dict[str, Any]:
    """Decisions attached to a file, with the PR/issue where they were made."""
    rows = db.run(
        """
        MATCH (d:Decision)-[affects:AFFECTS]->(f:File {path: $path})
        OPTIONAL MATCH (d)-[in_pr:DECIDED_IN]->(pr:PullRequest)
        OPTIONAL MATCH (d)-[in_issue:DECIDED_IN]->(i:Issue)
        RETURN f, affects, d, in_pr, pr, in_issue, i
        LIMIT $limit
        """,
        {"path": path, "limit": limit},
    )
    decisions = []
    seen = set()
    for f, d, pr, i in ((r[0], r[2], r[4], r[6]) for r in rows):
        did = d.properties.get("id")
        if did in seen:
            continue
        seen.add(did)
        decided_in = []
        if pr:
            decided_in.append({
                "type": "PullRequest", "number": pr.properties.get("number"),
                "title": pr.properties.get("title"),
                "url": _pr_url(pr.properties.get("number")),
            })
        if i:
            decided_in.append({
                "type": "Issue", "number": i.properties.get("number"),
                "title": i.properties.get("title"),
                "url": _issue_url(i.properties.get("number")),
            })
        decisions.append({
            "id": did,
            "summary": d.properties.get("summary"),
            "rationale": d.properties.get("rationale"),
            "rejected_alternatives": d.properties.get("rejected_alternatives"),
            "decided_in": decided_in,
            # Compatibility fields are populated only when the corresponding node exists.
            "pr_number": pr.properties.get("number") if pr else None,
            "pr_url": _pr_url(pr.properties.get("number")) if pr else None,
            "issue_number": i.properties.get("number") if i else None,
            "issue_url": _issue_url(i.properties.get("number")) if i else None,
        })
    return _pack(rows, {"path": path, "decisions": decisions})


def search_decisions(query: str, limit: int = 5) -> dict[str, Any]:
    """Search cleaned AND and expanded OR queries, merge their evidence scores."""
    raw_terms = re.findall(r"[A-Za-z0-9_]+", (query or "")[:300])
    ignored = {"a", "an", "and", "are", "as", "at", "be", "been", "being", "because", "by", "did", "do", "does", "for", "from", "how", "i", "in", "is", "it", "of", "on", "or", "the", "this", "to", "was", "were", "what", "when", "where", "which", "who", "why", "with", "enabled", "instead", "typed", "type", "string", "str", "optional", "avoid", "use", "using", "matching"}
    identifiers = list(dict.fromkeys(term.lower() for term in raw_terms if term.lower() not in ignored))
    if not identifiers:
        return _pack([], {"query": query, "cleaned_query": "", "normalized_query": "", "matches": []})
    # Keep whole identifiers and add their components: full-text tokenizers
    # disagree on underscores, while code decisions often use snake_case names.
    expanded = []
    for identifier in identifiers:
        expanded.append(identifier)
        expanded.extend(part for part in identifier.split("_") if len(part) > 1)
    concept_aliases = {
        "documented": ("documentation", "docstring", "docstrings", "docs", "clarified"),
        "rules": ("behavior", "behaviour", "docstring", "documentation"),
        "reason": ("rationale",),
    }
    for identifier in identifiers:
        expanded.extend(concept_aliases.get(identifier, ()))
    expanded = list(dict.fromkeys(expanded))
    cleaned = " ".join(identifiers)[:300]
    normalized = "|".join(expanded)[:240]
    variants = (cleaned, normalized)
    hits: dict[str, tuple[str, Any, float]] = {}
    for search_query in variants:
        for label in ("Decision", "PullRequest", "Issue"):
            try:
                for node, score in db.run(
                    f"CALL db.idx.fulltext.queryNodes('{label}', $query) YIELD node, score RETURN node, score ORDER BY score DESC LIMIT $limit",
                    {"query": search_query, "limit": max(limit * 8, 40)},
                ):
                    node_id = db.node_id(node)
                    previous = hits.get(node_id)
                    combined = float(score) + (previous[2] if previous else 0.0)
                    hits[node_id] = (label, node, combined)
            except Exception:
                continue
    raw_best_score = max((item[2] for item in hits.values()), default=0.0)
    ranked = []
    for label, node, score in hits.values():
        searchable = " ".join(str(node.properties.get(key) or "") for key in ("summary", "rationale", "title", "body")).lower()
        exact_identifiers = sum(1 for identifier in identifiers if "_" in identifier and identifier in searchable)
        searchable_terms = set(re.findall(r"[a-z0-9_]+", searchable))
        searchable_terms.update(part for term in tuple(searchable_terms) for part in term.split("_") if len(part) > 1)
        token_overlap = sum(1 for term in expanded if term in searchable_terms)
        # Preserve exact code-identifier matches through the cross-label merge;
        # decisions are the primary result type, so an exact identifier match
        # contributes more than a generic issue/PR text hit.
        score += exact_identifiers * (4.0 if label == "Decision" else 0.8)
        if label == "Decision":
            score += token_overlap * 2.0 + raw_best_score * 0.5
        ranked.append((label, node, score))
    ranked.sort(key=lambda item: item[2], reverse=True)
    best_score = ranked[0][2] if ranked else 0.0
    minimum_score = best_score * 0.30
    ranked = [hit for hit in ranked if hit[2] >= minimum_score][:min(limit, 5)]
    rows: list[list[Any]] = []
    matches = []
    for label, node, score in ranked:
        props = node.properties
        match = {
            "id": db.node_id(node), "type": label,
            "summary": props.get("summary") or props.get("title", ""),
            "rationale": props.get("rationale", ""),
            "number": props.get("number") if label in {"PullRequest", "Issue"} else None,
            "relevance_score": round(score, 4),
        }
        if label == "Decision":
            source_rows = db.run(
                "MATCH (d:Decision)-[:DECIDED_IN]->(source) WHERE id(d)=$nid RETURN labels(source)[0], source.number, source.title, source.url",
                {"nid": node.id},
            )
            match["decided_in"] = [
                {"type": kind, "number": number, "title": title,
                 "url": _pr_url(number) if kind == "PullRequest" else _issue_url(number)}
                for kind, number, title, url in source_rows if kind in {"PullRequest", "Issue"}
            ]
        elif label in {"PullRequest", "Issue"}:
            match["reference"] = {
                "type": label, "number": props.get("number"),
                "url": _pr_url(props.get("number")) if label == "PullRequest" else _issue_url(props.get("number")),
            }
        matches.append(match)
        rows.extend(db.run(
            "MATCH (n) WHERE id(n) = $nid OPTIONAL MATCH (n)-[r]-(near) WHERE labels(near)[0] IN ['Decision','PullRequest','Issue','File','Person'] RETURN n, r, near LIMIT 16",
            {"nid": node.id},
        ))
    packed = _pack(rows, {"query": query, "cleaned_query": cleaned, "normalized_query": normalized, "minimum_score": round(minimum_score, 4), "matches": matches})
    trace = packed["trace"]
    by_id = {node["id"]: node for node in trace["nodes"]}
    match_order = {item["id"]: index for index, item in enumerate(matches)}
    keep = set(match_order)
    decision_ids = {item["id"] for item in matches if item["type"] == "Decision"}
    pr_ids = {item["id"] for item in matches if item["type"] == "PullRequest"}
    for edge in trace["edges"]:
        source, target = edge["source"], edge["target"]
        other = target if source in decision_ids else source if target in decision_ids else None
        if other and by_id.get(other, {}).get("type") in {"File", "PullRequest", "Issue"}:
            keep.add(other)
            if by_id.get(other, {}).get("type") in {"PullRequest", "Issue"}:
                pr_ids.add(other)
        for matched_id in match_order:
            if matched_id in (source, target):
                other = target if source == matched_id else source
                if by_id.get(other, {}).get("type") in {"File", "Issue"}:
                    keep.add(other)
    # People are included only when directly tied to a cited or decision-linked PR.
    for edge in trace["edges"]:
        source, target = edge["source"], edge["target"]
        if source in pr_ids and by_id.get(target, {}).get("type") == "Person":
            keep.add(target)
        elif target in pr_ids and by_id.get(source, {}).get("type") == "Person":
            keep.add(source)
    type_order = {"Decision": 0, "PullRequest": 1, "Issue": 2, "File": 3, "Person": 4}
    trace["nodes"] = sorted(
        (node for node in trace["nodes"] if node["id"] in keep),
        key=lambda node: (0, match_order[node["id"]]) if node["id"] in match_order else (1, type_order.get(node["type"], 9), node["label"]),
    )
    trace["edges"] = [
        edge for edge in trace["edges"]
        if edge["source"] in keep and edge["target"] in keep
        and (edge["source"] in match_order or edge["target"] in match_order or edge["source"] in pr_ids or edge["target"] in pr_ids)
    ]
    return packed


def pr_details(number: int) -> dict[str, Any]:
    """A PR plus its issues, reviewers and extracted decisions."""
    rows = db.run(
        """
        MATCH (pr:PullRequest {number: $number})
        OPTIONAL MATCH (a:Person)-[:AUTHORED]->(pr)
        OPTIONAL MATCH (r:Person)-[rev:REVIEWED]->(pr)
        OPTIONAL MATCH (pr)-[link:FIXES|MENTIONS]->(i:Issue)
        OPTIONAL MATCH (d:Decision)-[:DECIDED_IN]->(pr)
        RETURN pr, a, r, rev, link, i, d
        """,
        {"number": number},
    )
    if not rows:
        return _pack([], {"number": number, "found": False}, {"note": "PR not in graph"})
    pr = rows[0][0]
    author = None
    reviewers: dict[str, int] = {}
    issues: dict[int, dict] = {}
    decisions: dict[str, dict] = {}
    for row in rows:
        _, a, r, rev, link, i, d = row
        if a and author is None:
            author = a.properties.get("login")
        if r:
            reviewers[r.properties.get("login")] = rev.properties.get("comment_count", 0) if rev else 0
        if i and link:
            issues[i.properties.get("number")] = {
                "number": i.properties.get("number"),
                "title": i.properties.get("title"),
                "url": _issue_url(i.properties.get("number")),
                "relation": link.relation,
            }
        if d:
            decisions[d.properties.get("id")] = {
                "id": d.properties.get("id"),
                "summary": d.properties.get("summary"),
                "rationale": d.properties.get("rationale"),
                "rejected_alternatives": d.properties.get("rejected_alternatives"),
            }
    data = {
        "number": pr.properties.get("number"),
        "title": pr.properties.get("title"),
        "body": (pr.properties.get("body") or "")[:2000],
        "url": _pr_url(number),
        "merged_at": pr.properties.get("merged_at"),
        "author": author,
        "reviewers": [{"login": k, "comment_count": v} for k, v in reviewers.items()],
        "issues": list(issues.values()),
        "decisions": list(decisions.values()),
    }
    return _pack(rows, data)


def issue_details(number: int) -> dict[str, Any]:
    """An issue, its comments, and the PRs that reference it."""
    rows = db.run(
        """
        MATCH (i:Issue {number: $number})
        OPTIONAL MATCH (pr:PullRequest)-[link:FIXES|MENTIONS]->(i)
        OPTIONAL MATCH (d:Decision)-[:DECIDED_IN]->(i)
        RETURN i, pr, link, d
        """,
        {"number": number},
    )
    if not rows:
        return _pack([], {"number": number, "found": False}, {"note": "issue not in graph"})
    i = rows[0][0]
    prs = {}
    decisions = {}
    for _, pr, link, d in rows:
        if pr:
            prs[pr.properties.get("number")] = {
                "number": pr.properties.get("number"),
                "title": pr.properties.get("title"),
                "url": _pr_url(pr.properties.get("number")),
                "relation": link.relation if link else None,
            }
        if d:
            decisions[d.properties.get("id")] = {
                "id": d.properties.get("id"),
                "summary": d.properties.get("summary"),
                "rationale": d.properties.get("rationale"),
            }
    data = {
        "number": i.properties.get("number"),
        "title": i.properties.get("title"),
        "body": (i.properties.get("body") or "")[:2000],
        "comments": (i.properties.get("comments") or "")[:2000],
        "url": _issue_url(number),
        "prs": list(prs.values()),
        "decisions": list(decisions.values()),
    }
    return _pack(rows, data)


def who_knows_file(path: str, limit: int = 8) -> dict[str, Any]:
    """People ranked by recent authoring (1.0) + reviewing (0.5), recency-weighted.

    Considers the file and its directory.
    """
    directory = path.rsplit("/", 1)[0] if "/" in path else ""
    valid_prs = _valid_pr_numbers()
    rows = db.run(
        """
        MATCH (f:File)<-[:TOUCHES]-(pr:PullRequest)
        WHERE (f.path = $path OR ($dir <> '' AND f.path STARTS WITH $dirpfx))
          AND pr.number IN $valid_prs
        MATCH (p:Person)-[rel:AUTHORED|REVIEWED]->(pr)
        WHERE NOT p.login ENDS WITH '[bot]'
          AND toLower(p.login) <> 'copilot' AND toLower(p.login) <> 'github-actions'
          AND toLower(p.login) <> 'dependabot' AND toLower(p.login) <> 'renovate' AND toLower(p.login) <> 'greenkeeper'
        RETURN p, rel, pr, f
        """,
        {"path": path, "dir": directory, "dirpfx": f"{directory}/", "valid_prs": valid_prs},
    )
    scores: dict[str, float] = {}
    last_active: dict[str, str] = {}
    reasons: dict[str, dict] = {}
    for p, rel, pr, f in ((r[0], r[1], r[2], r[3]) for r in rows):
        login = p.properties.get("login")
        if not login or login.endswith('[bot]'):
            continue
        weight = 1.0 if rel.relation == "AUTHORED" else 0.5
        scores[login] = scores.get(login, 0.0) + weight * _recency_weight(pr.properties.get("merged_at"))
        merged = pr.properties.get("merged_at") or ""
        if merged > last_active.get(login, ""):
            last_active[login] = merged
        info = reasons.setdefault(login, {"login": login, "authored": 0, "reviewed": 0})
        if rel.relation == "AUTHORED":
            info["authored"] += 1
        else:
            info["reviewed"] += 1

    people = sorted(
        (
            {**reasons[k], "score": round(scores[k], 4), "last_active": last_active.get(k, "")}
            for k in scores
        ),
        key=lambda x: x["score"],
        reverse=True,
    )[:limit]
    # Trim the traced graph to People + PRs + files (drop nothing; db.collect already dedups).
    return _pack(rows, {"path": path, "people": people})


def related_files(path: str, limit: int = 10) -> dict[str, Any]:
    """Files that most often change together with a file, via shared PRs."""
    candidates = db.run(
        """
        MATCH (f:File {path: $path})<-[:TOUCHES]-(pr:PullRequest)-[:TOUCHES]->(other:File)
        WHERE other.path <> $path
          AND NOT toLower(coalesce(pr.title, '')) CONTAINS 'release'
          AND NOT toLower(coalesce(pr.title, '')) CONTAINS 'bump'
          AND NOT (pr.title STARTS WITH 'v' AND substring(pr.title, 1, 1) IN ['0','1','2','3','4','5','6','7','8','9'])
        WITH f, other, pr
        MATCH (pr)-[:TOUCHES]->(touched:File)
        WITH f, other, pr, count(DISTINCT touched) AS file_count
        WHERE file_count <= 15
        WITH f, other, count(DISTINCT pr) AS shared
        WHERE shared >= 2
        RETURN f.path, other.path, shared
        ORDER BY shared DESC
        LIMIT $limit
        """,
        {"path": path, "limit": limit},
    )
    paths = [row[1] for row in candidates]
    valid_prs = _valid_pr_numbers()
    rows = db.run(
        """MATCH (f:File {path:$path})<-[r1:TOUCHES]-(pr:PullRequest)-[r2:TOUCHES]->(other:File)
           WHERE other.path IN $paths
             AND NOT toLower(coalesce(pr.title,'')) CONTAINS 'release'
             AND NOT toLower(coalesce(pr.title,'')) CONTAINS 'bump'
             AND NOT (pr.title STARTS WITH 'v' AND substring(pr.title,1,1) IN ['0','1','2','3','4','5','6','7','8','9'])
           MATCH (pr)-[:TOUCHES]->(touched:File)
           WITH f,r1,pr,r2,other,count(DISTINCT touched) AS file_count
           WHERE file_count <= 15
           RETURN f,r1,pr,r2,other LIMIT $limit""",
        {"path": path, "paths": paths, "limit": max(limit * 10, 20)},
    ) if paths else []
    shared_by_path = {row[1]: row[2] for row in candidates}
    files = [{"path": p, "shared_prs": shared_by_path[p]} for p in paths]
    data = {"path": path, "files": files}
    return _pack(rows, data)


# --------------------------------------------------------------------------
# graph algorithms (FalkorDB, with a networkx fallback)
# --------------------------------------------------------------------------
def _file_pagerank() -> tuple[dict[str, float], str]:
    """PageRank over materialized, noise-filtered file co-change edges."""
    try:
        _write_file_cochange_edges()
        rows = db.run("CALL algo.pageRank('File', 'CO_CHANGED_WITH') YIELD node, score RETURN node, score")
        scores = {r[0].properties.get("path"): float(r[1]) for r in rows}
        if scores:
            return scores, "falkordb:algo.pageRank(File,CO_CHANGED_WITH)"
    except Exception as err:
        print(f"[blast_radius] FalkorDB pageRank unavailable: {err}")
    return _file_pagerank_networkx(), "networkx:pagerank(co-change)"


def _file_pagerank_networkx() -> dict[str, float]:
    import networkx as nx

    rows = db.run("MATCH (f:File)-[r:CO_CHANGED_WITH]->(g:File) RETURN f, g, r.shared_prs")
    g = nx.Graph()
    for f, other, weight in rows:
        g.add_edge(f.properties.get("path"), other.properties.get("path"), weight=weight)
    if not g.nodes:
        return {}
    return nx.pagerank(g)


def _write_file_cochange_edges() -> None:
    """Refresh file-pair weights from non-release PRs touching at most 15 files."""
    db.run("MATCH (:File)-[r:CO_CHANGED_WITH]->(:File) DELETE r")
    rows = db.run("""MATCH (pr:PullRequest)-[:TOUCHES]->(f1:File), (pr)-[:TOUCHES]->(f2:File)
       WHERE f1.path < f2.path
         AND NOT toLower(coalesce(pr.title,'')) CONTAINS 'release'
         AND NOT toLower(coalesce(pr.title,'')) CONTAINS 'bump'
         AND NOT (pr.title STARTS WITH 'v' AND substring(pr.title,1,1) IN ['0','1','2','3','4','5','6','7','8','9'])
       WITH pr,f1,f2
       MATCH (pr)-[:TOUCHES]->(touched:File)
       WITH pr,f1,f2,count(DISTINCT touched) AS file_count
       WHERE file_count <= 15
       WITH f1,f2,count(DISTINCT pr) AS shared
       WHERE shared >= 2
       RETURN f1.path,f2.path,shared""")
    for path1, path2, shared in rows:
        db.run("""MATCH (a:File {path:$a}),(b:File {path:$b})
           MERGE (a)-[r:CO_CHANGED_WITH]->(b) SET r.shared_prs=$shared
           MERGE (b)-[s:CO_CHANGED_WITH]->(a) SET s.shared_prs=$shared""",
               {"a": path1, "b": path2, "shared": int(shared)})


def blast_radius(paths: list[str], limit: int = 12) -> dict[str, Any]:
    """Files likely affected by changing ``paths``.

    Co-change candidates come from the graph; they are ranked by a real graph
    algorithm (FalkorDB PageRank over filtered CO_CHANGED_WITH edges, networkx
    fallback).
    """
    scores, algorithm = _file_pagerank()
    rows = db.run(
        """
        MATCH (seed:File)<-[:TOUCHES]-(pr:PullRequest)-[t:TOUCHES]->(other:File)
        WHERE seed.path IN $paths AND NOT other.path IN $paths
          AND NOT toLower(coalesce(pr.title,'')) CONTAINS 'release'
          AND NOT toLower(coalesce(pr.title,'')) CONTAINS 'bump'
          AND NOT (pr.title STARTS WITH 'v' AND substring(pr.title,1,1) IN ['0','1','2','3','4','5','6','7','8','9'])
        WITH seed, other, pr
        MATCH (pr)-[:TOUCHES]->(touched:File)
        WITH seed, other, pr, count(DISTINCT touched) AS file_count
        WHERE file_count <= 15
        WITH seed, other, count(DISTINCT pr) AS shared
        WHERE shared >= 2
        RETURN seed.path, other.path, shared
        ORDER BY shared DESC LIMIT $limit
        """,
        {"paths": paths, "limit": limit * 3},
    )
    cands: dict[str, dict] = {}
    for seed_path, p, shared in rows:
        entry = cands.setdefault(
            p,
            {
                "path": p,
                "shared_prs": int(shared),
                "via": [seed_path],
                "pagerank": round(scores.get(p, 0.0), 6),
            },
        )
        if seed_path not in entry["via"]:
            entry["via"].append(seed_path)
    ranked = sorted(
        cands.values(), key=lambda x: (x["pagerank"], x["shared_prs"]), reverse=True
    )[:limit]
    related_paths = [item["path"] for item in ranked]
    trace_rows = db.run(
        """MATCH (seed:File)<-[r1:TOUCHES]-(pr:PullRequest)-[r2:TOUCHES]->(other:File)
           WHERE seed.path IN $paths AND other.path IN $related
             AND NOT toLower(coalesce(pr.title,'')) CONTAINS 'release'
             AND NOT toLower(coalesce(pr.title,'')) CONTAINS 'bump'
             AND NOT (pr.title STARTS WITH 'v' AND substring(pr.title,1,1) IN ['0','1','2','3','4','5','6','7','8','9'])
           MATCH (pr)-[:TOUCHES]->(touched:File)
           WITH seed,r1,pr,r2,other,count(DISTINCT touched) AS file_count
           WHERE file_count <= 15
           RETURN seed,r1,pr,r2,other LIMIT $limit""",
        {"paths": paths, "related": related_paths, "limit": max(limit * 10, 20)},
    ) if related_paths else []
    return _pack(trace_rows, {"changed": paths, "files": ranked, "algorithm": algorithm})


def _person_betweenness() -> tuple[dict[str, float], str]:
    """Read scores persisted during ingestion; calculate only if absent."""
    stored = db.run("MATCH (p:Person) WHERE p.betweenness_score IS NOT NULL RETURN p.login,p.betweenness_score")
    scores = {str(login): float(score) for login, score in stored if login and not _is_bot_login(str(login))}
    if scores:
        algorithm = "falkordb:algo.betweenness(Person,COLLABORATES_WITH) (stored)"
        return scores, algorithm
    return refresh_connector_scores()


def refresh_connector_scores() -> tuple[dict[str, float], str]:
    """Refresh collaboration edges and persist one centrality snapshot."""
    _write_collaboration_edges()
    algorithm = "falkordb:algo.betweenness(Person,COLLABORATES_WITH)"
    try:
        rows = db.run("""CALL algo.betweenness({nodeLabels:['Person'], relationshipTypes:['COLLABORATES_WITH']})
                       YIELD node, score RETURN node.login, score""")
        scores = {str(login): float(score) for login, score in rows if login and not _is_bot_login(str(login))}
        if not scores:
            raise RuntimeError("betweenness returned no people")
    except Exception as err:
        print(f"[connectors] FalkorDB betweenness unavailable; storing deterministic fallback: {err}")
        scores = _person_collab_betweenness()
        algorithm = "networkx:betweenness(person-collab fallback, seed=42)"
    people = db.run("MATCH (p:Person) WHERE NOT p.login ENDS WITH '[bot]' RETURN p.login")
    for (login,) in people:
        if login and not _is_bot_login(str(login)):
            scores.setdefault(str(login), 0.0)
    payload = [{"login": login, "score": score} for login, score in scores.items()]
    if payload:
        db.run("UNWIND $rows AS row MATCH (p:Person {login:row.login}) SET p.betweenness_score=row.score, p.betweenness_algorithm=$algorithm", {"rows": payload, "algorithm": algorithm})
    return scores, algorithm + " (stored)"


def _is_bot_login(login: str) -> bool:
    name = login.lower()
    return name.endswith("[bot]") or name in {"copilot", "github-actions", "dependabot", "renovate", "greenkeeper"}


def _valid_pr_numbers() -> list[int]:
    """Merged PRs usable for ownership and collaboration signals."""
    valid = []
    for number, title, file_count in db.run("""MATCH (pr:PullRequest)-[:TOUCHES]->(f:File)
       RETURN pr.number,pr.title,count(DISTINCT f)"""):
        text = (title or "").lower()
        version = text.startswith("v") and len(text) > 1 and text[1].isdigit()
        if file_count <= 15 and "release" not in text and "bump" not in text and not version:
            valid.append(number)
    return valid


def _write_collaboration_edges() -> None:
    """Refresh reciprocal weighted collaboration edges, excluding automation."""
    valid_prs = _valid_pr_numbers()
    shared_rows = db.run("""MATCH (p1:Person)-[:AUTHORED|REVIEWED]->(pr1:PullRequest)-[:TOUCHES]->(f:File)
       <-[:TOUCHES]-(pr2:PullRequest)<-[:AUTHORED|REVIEWED]-(p2:Person)
       WHERE pr1.number IN $valid_prs AND pr2.number IN $valid_prs
         AND p1.login < p2.login AND NOT p1.login ENDS WITH '[bot]' AND NOT p2.login ENDS WITH '[bot]'
         AND NOT toLower(coalesce(pr1.title,'')) CONTAINS 'release' AND NOT toLower(coalesce(pr1.title,'')) CONTAINS 'bump'
         AND NOT toLower(coalesce(pr2.title,'')) CONTAINS 'release' AND NOT toLower(coalesce(pr2.title,'')) CONTAINS 'bump'
         AND NOT (pr1.title STARTS WITH 'v' AND substring(pr1.title,1,1) IN ['0','1','2','3','4','5','6','7','8','9'])
         AND NOT (pr2.title STARTS WITH 'v' AND substring(pr2.title,1,1) IN ['0','1','2','3','4','5','6','7','8','9'])
         AND toLower(p1.login) <> 'copilot' AND toLower(p1.login) <> 'github-actions'
         AND toLower(p1.login) <> 'dependabot' AND toLower(p1.login) <> 'renovate' AND toLower(p1.login) <> 'greenkeeper'
         AND toLower(p2.login) <> 'copilot' AND toLower(p2.login) <> 'github-actions'
         AND toLower(p2.login) <> 'dependabot' AND toLower(p2.login) <> 'renovate' AND toLower(p2.login) <> 'greenkeeper'
       RETURN p1.login,p2.login,count(DISTINCT f)""", {"valid_prs": valid_prs})
    review_rows = db.run("""MATCH (reviewer:Person)-[:REVIEWED]->(pr:PullRequest)<-[:AUTHORED]-(author:Person)
       WHERE pr.number IN $valid_prs AND reviewer.login <> author.login AND NOT reviewer.login ENDS WITH '[bot]' AND NOT author.login ENDS WITH '[bot]'
         AND NOT toLower(coalesce(pr.title,'')) CONTAINS 'release' AND NOT toLower(coalesce(pr.title,'')) CONTAINS 'bump'
         AND NOT (pr.title STARTS WITH 'v' AND substring(pr.title,1,1) IN ['0','1','2','3','4','5','6','7','8','9'])
         AND toLower(reviewer.login) <> 'copilot' AND toLower(reviewer.login) <> 'github-actions'
         AND toLower(reviewer.login) <> 'dependabot' AND toLower(reviewer.login) <> 'renovate' AND toLower(reviewer.login) <> 'greenkeeper'
         AND toLower(author.login) <> 'copilot' AND toLower(author.login) <> 'github-actions'
         AND toLower(author.login) <> 'dependabot' AND toLower(author.login) <> 'renovate' AND toLower(author.login) <> 'greenkeeper'
       RETURN reviewer.login,author.login,count(DISTINCT pr)""", {"valid_prs": valid_prs})
    weights: dict[tuple[str, str], int] = {}
    for login1, login2, count in shared_rows:
        key = (login1, login2)
        weights[key] = weights.get(key, 0) + int(count)
    for login1, login2, count in review_rows:
        key = tuple(sorted((login1, login2)))
        weights[key] = weights.get(key, 0) + int(count)
    db.run("MATCH (:Person)-[r:COLLABORATES_WITH]->(:Person) DELETE r")
    for (login1, login2), weight in weights.items():
        if weight <= 0:
            continue
        db.run("""MATCH (a:Person {login:$a}),(b:Person {login:$b})
           MERGE (a)-[r:COLLABORATES_WITH]->(b) SET r.weight=$weight
           MERGE (b)-[s:COLLABORATES_WITH]->(a) SET s.weight=$weight""",
               {"a": login1, "b": login2, "weight": weight})


def _person_collab_betweenness() -> dict[str, float]:
    import networkx as nx
    g = nx.Graph()
    rows = db.run("""MATCH (p1:Person)-[r:COLLABORATES_WITH]->(p2:Person)
                    WHERE p1.login < p2.login RETURN p1.login,p2.login,r.weight""")
    for login1, login2, weight in rows:
        if not _is_bot_login(login1) and not _is_bot_login(login2):
            g.add_edge(login1, login2, weight=weight)
    if len(g.nodes) < 2:
        return {}
    return nx.betweenness_centrality(g, weight='weight', seed=42)


# --------------------------------------------------------------------------
# reviewers
# --------------------------------------------------------------------------
def suggest_reviewers(paths: list[str], exclude: str | None = None, limit: int = 5) -> dict[str, Any]:
    """Rank reviewers by recent authoring + reviewing on the files and their
    co-change neighbours, excluding the PR author. Boost with connector score."""
    if not paths:
        return _pack([], {"reviewers": []}, {"note": "no paths given"})

    neighbours = [f["path"] for f in blast_radius(paths, limit=8)["data"]["files"]]
    scope = list(dict.fromkeys(paths + neighbours))

    connector_scores, connector_algorithm = _person_betweenness()
    valid_prs = _valid_pr_numbers()

    rows = db.run(
        """
        MATCH (f:File)<-[:TOUCHES]-(pr:PullRequest)
        WHERE f.path IN $scope AND pr.number IN $valid_prs
        MATCH (p:Person)-[rel:AUTHORED|REVIEWED]->(pr)
        WHERE NOT p.login ENDS WITH '[bot]'
          AND toLower(p.login) <> 'copilot' AND toLower(p.login) <> 'github-actions'
          AND toLower(p.login) <> 'dependabot' AND toLower(p.login) <> 'renovate' AND toLower(p.login) <> 'greenkeeper'
        RETURN p, rel, pr, f
        """,
        {"scope": scope, "valid_prs": valid_prs},
    )
    scores: dict[str, float] = {}
    last: dict[str, str] = {}
    detail: dict[str, dict] = {}
    for p, rel, pr, f in ((r[0], r[1], r[2], r[3]) for r in rows):
        login = p.properties.get("login")
        if not login or login == exclude or _is_bot_login(login):
            continue
        base = 1.0 if rel.relation == "AUTHORED" else 0.6
        scores[login] = scores.get(login, 0.0) + base * _recency_weight(pr.properties.get("merged_at"))
        merged = pr.properties.get("merged_at") or ""
        if merged > last.get(login, ""):
            last[login] = merged
        info = detail.setdefault(login, {"login": login, "files": set(), "authored": 0, "reviewed": 0})
        info["files"].add(f.properties.get("path"))
        if rel.relation == "AUTHORED":
            info["authored"] += 1
        else:
            info["reviewed"] += 1

    # Blend recency score with connector score (betweenness)
    max_conn = max(connector_scores.values()) if connector_scores else 1.0
    for login in scores:
        conn = connector_scores.get(login, 0.0)
        scores[login] += conn / max(max_conn, 0.001) * 2.0  # connector bonus up to 2.0

    reviewers = sorted(
        (
            {
                "login": k,
                "score": round(scores[k], 4),
                "connector_score": round(connector_scores.get(k, 0.0), 6),
                "last_active": last.get(k, ""),
                "authored": detail[k]["authored"],
                "reviewed": detail[k]["reviewed"],
                "files": len(detail[k]["files"]),
            }
            for k in scores
        ),
        key=lambda x: x["score"],
        reverse=True,
    )[:limit]
    return _pack(rows, {
        "paths": paths,
        "reviewers": reviewers,
        "connector_algorithm": connector_algorithm,
    })


def connectors(limit: int = 10) -> dict[str, Any]:
    """People who bridge the most areas of the codebase (betweenness)."""
    scores, algorithm = _person_betweenness()
    ranked = [(k, v) for k, v in sorted(scores.items(), key=lambda kv: kv[1], reverse=True) if not k.endswith('[bot]')][:limit]
    rows = db.run("MATCH (p:Person) WHERE NOT p.login ENDS WITH '[bot]' RETURN p")
    return _pack(rows, {"algorithm": algorithm, "people": [
        {"login": login, "score": round(score, 6)} for login, score in ranked
    ]})


def neighborhood(path: str, limit: int = 40, owner: str = "") -> dict[str, Any]:
    """Small subgraph around a file for the UI."""
    rows = db.run(
        """
        MATCH (f:File {path: $path})<-[touch:TOUCHES]-(pr:PullRequest)
        OPTIONAL MATCH (d:Decision)-[affects:AFFECTS]->(f)
        OPTIONAL MATCH (p:Person {login: $owner})-[contrib:AUTHORED|REVIEWED]->(pr)
        RETURN f, touch, pr, d, affects, p, contrib
        LIMIT $limit
        """,
        {"path": path, "owner": owner, "limit": limit},
    )
    return _pack(rows, {"path": path})


def bus_factor_neighborhood(path: str, owner: str, pr_numbers: list[int]) -> dict[str, Any]:
    """Trace every qualifying PR in the denominator and the owner's numerator edges."""
    rows = db.run(
        """
        MATCH (pr:PullRequest)-[t:TOUCHES]->(f:File {path:$path})
        WHERE pr.number IN $numbers
        OPTIONAL MATCH (p:Person {login:$owner})-[c:AUTHORED|REVIEWED]->(pr)
        RETURN p,c,pr,t,f
        """,
        {"path": path, "owner": owner, "numbers": pr_numbers},
    ) if pr_numbers else []
    return _pack(rows, {"path": path, "owner": owner, "supporting_prs": pr_numbers})


# --------------------------------------------------------------------------
# registry used by the agents and MCP server
# --------------------------------------------------------------------------
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "search_decisions": {
        "fn": search_decisions,
        "description": "Search decision rationales and PR/issue discussions by text, then follow related graph evidence.",
        "params": {"query": "string (words to search for)"},
    },
    "find_file": {
        "fn": find_file,
        "description": "Fuzzy-match a file path in the repository graph. Use when you only know part of a path.",
        "params": {"query": "string (part of a path)"},
    },
    "file_history": {
        "fn": file_history,
        "description": "Merged PRs that touched a file, newest first, with authors and churn.",
        "params": {"path": "string (exact file path from find_file)"},
    },
    "decisions_for_file": {
        "fn": decisions_for_file,
        "description": "Recorded decisions and their rationale attached to a file, with the PR/issue where they were made.",
        "params": {"path": "string (exact file path)"},
    },
    "pr_details": {
        "fn": pr_details,
        "description": "A pull request with its issues, reviewers, and extracted decisions.",
        "params": {"number": "integer PR number"},
    },
    "issue_details": {
        "fn": issue_details,
        "description": "An issue, its comments, and the PRs that reference it.",
        "params": {"number": "integer issue number"},
    },
    "who_knows_file": {
        "fn": who_knows_file,
        "description": "People ranked by recent authoring and reviewing on a file or its directory.",
        "params": {"path": "string (exact file path)"},
    },
    "related_files": {
        "fn": related_files,
        "description": "Files that most often change together with a file, via shared PRs.",
        "params": {"path": "string (exact file path)"},
    },
    "blast_radius": {
        "fn": blast_radius,
        "description": "Files likely to be affected by changing the given files, ranked by a graph algorithm over the PR-file structure.",
        "params": {"paths": "array of file paths"},
    },
    "suggest_reviewers": {
        "fn": suggest_reviewers,
        "description": "Suggested reviewers for changes to the given files, ranked by recent authoring and reviewing, excluding one login.",
        "params": {"paths": "array of file paths", "exclude": "login to exclude (optional)"},
    },
}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tool by name with JSON-ish arguments."""
    spec = TOOL_SPECS.get(name)
    if not spec:
        return {"error": f"unknown tool '{name}'", "data": None, "trace": {"nodes": [], "edges": []}}
    try:
        return spec["fn"](**arguments)
    except TypeError as err:
        return {"error": f"bad arguments for {name}: {err}", "data": None, "trace": {"nodes": [], "edges": []}}
    except Exception as err:  # pragma: no cover - defensive
        return {"error": f"{name} failed: {err}", "data": None, "trace": {"nodes": [], "edges": []}}
