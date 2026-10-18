"""Print the strongest single-file decision candidates for Ask examples."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
import agent


def main() -> int:
    rows = db.run(
        """MATCH (d:Decision)-[:AFFECTS]->(f:File)
           WHERE size(coalesce(d.rationale,'')) > 80 AND f.path STARTS WITH 'src/'
           WITH d, collect(DISTINCT f.path) AS paths
           WHERE size(paths) = 1
           MATCH (d)-[:DECIDED_IN]->(pr:PullRequest)
           WHERE NOT toLower(coalesce(pr.title,'')) CONTAINS 'release'
             AND NOT toLower(coalesce(pr.title,'')) CONTAINS 'bump'
             AND NOT (pr.title STARTS WITH 'v' AND substring(pr.title,1,1) IN ['0','1','2','3','4','5','6','7','8','9'])
           RETURN d.summary,d.rationale,paths[0],pr.number,pr.title
           ORDER BY size(d.rationale) DESC LIMIT 10"""
    )
    print(f"Top unique decision candidates: {len(rows)}")
    for index, (summary, rationale, path, number, title) in enumerate(rows, 1):
        print(f"{index}. {summary}\n   File: {path}\n   PR: #{number} {title}\n   Why question: {agent.question_for_decision(summary)}\n   Rationale: {rationale[:220]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
