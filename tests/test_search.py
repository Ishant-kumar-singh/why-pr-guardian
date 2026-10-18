"""Live FalkorDB retrieval regression checks for example questions/decisions.

Run with ``python tests/test_search.py`` to print the full PASS/FAIL matrix, or
with ``pytest -q -s tests/test_search.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent
import db
import graph_tools


def _best_decisions() -> list[dict[str, str]]:
    rows = db.run("""MATCH (d:Decision)-[:AFFECTS]->(f:File)
        WHERE size(coalesce(d.rationale,'')) > 80 AND f.path STARTS WITH 'src/'
        WITH d, collect(DISTINCT f.path) AS paths WHERE size(paths)=1
        RETURN d.id, d.summary, size(coalesce(d.rationale,'')) AS rationale_size
        ORDER BY rationale_size DESC LIMIT 40""")
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for decision_id, summary, _ in rows:
        if decision_id and decision_id not in seen:
            seen.add(decision_id)
            found.append({"id": str(decision_id), "summary": str(summary or "")})
        if len(found) == 10:
            break
    return found


def _top_ids(question: str) -> list[str]:
    return [item["id"] for item in graph_tools.search_decisions(question, limit=5)["data"]["matches"][:3]]


def _cases() -> list[tuple[str, str, str]]:
    best = _best_decisions()
    examples = agent.example_questions()
    question_source = {
        agent.question_for_decision(item["summary"]): item["id"] for item in best
    }
    cases: list[tuple[str, str, str]] = []
    for question in examples:
        source_id = question_source.get(question)
        if source_id is None:
            raise AssertionError(f"Could not identify source Decision for example: {question}")
        cases.append(("example", question, f"Decision:{source_id}"))
    for item in best:
        cases.append(("top decision", agent.question_for_decision(item["summary"]), f"Decision:{item['id']}"))
    return cases


def test_source_decision_ranks_in_top_three(capsys) -> None:
    rows = []
    for kind, question, expected_id in _cases():
        top = _top_ids(question)
        passed = expected_id in top
        rows.append(("PASS" if passed else "FAIL", kind, expected_id, question, ", ".join(top)))

    headers = ("RESULT", "SET", "SOURCE DECISION", "QUESTION", "TOP 3 IDs")
    widths = [9, 13, 42, 82, 100]
    print(" | ".join(value.ljust(width) for value, width in zip(headers, widths)))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(value[:width].ljust(width) for value, width in zip(row, widths)))

    failures = [row for row in rows if row[0] == "FAIL"]
    assert not failures, f"{len(failures)} source decisions did not rank in the top 3"


if __name__ == "__main__":
    test_source_decision_ranks_in_top_three(None)
