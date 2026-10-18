"""Run the five Ask examples end to end and compare two dry-run Guardian passes."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent
import config
import db
import graph_tools as gt
import guardian


def source_refs(decision_id: str) -> list[str]:
    rows = db.run(
        "MATCH (d:Decision {id:$id})-[:DECIDED_IN]->(s) RETURN labels(s), s.number",
        {"id": decision_id},
    )
    return [f"{'PR' if labels and labels[0] == 'PullRequest' else 'issue'} #{number}" for labels, number in rows]


def main() -> int:
    config.DRY_RUN = True
    examples = agent.example_questions()
    rows = db.run(
        """MATCH (d:Decision)-[:AFFECTS]->(f:File)
           WHERE size(coalesce(d.rationale,'')) > 80 AND f.path STARTS WITH 'src/'
           WITH d, collect(DISTINCT f.path) AS paths WHERE size(paths)=1
           MATCH (d)-[:DECIDED_IN]->(pr:PullRequest)
           WHERE NOT toLower(coalesce(pr.title,'')) CONTAINS 'release'
             AND NOT toLower(coalesce(pr.title,'')) CONTAINS 'bump'
             AND NOT (pr.title STARTS WITH 'v' AND substring(pr.title,1,1) IN ['0','1','2','3','4','5','6','7','8','9'])
           RETURN d.id, d.summary ORDER BY size(coalesce(d.rationale,'')) DESC LIMIT 10"""
    )
    ids_by_question = {agent.question_for_decision(summary): str(decision_id) for decision_id, summary in rows}
    summaries_by_id = {str(decision_id): str(summary) for decision_id, summary in rows}
    failures = []
    for index, question in enumerate(examples, 1):
        expected_id = ids_by_question.get(question)
        expected = next((m for m in gt.search_decisions(question, limit=10)["data"]["matches"] if m.get("id") == expected_id), {})
        result = agent.ask(question)
        answer = result["answer"].strip()
        citations = [f"{'PR' if kind.lower() in {'pr','pullrequest'} else 'issue'} #{number}" for kind, number in re.findall(r"\b(PR|PullRequest|issue|Issue)\s+#(\d+)\b", answer, re.I)]
        expected_source = ", ".join(source_refs(expected_id or "")) or "no source link"
        source_summary = expected.get("summary") or summaries_by_id.get(expected_id or "", "expected decision not found")
        source_ok = bool(expected_id) and expected_id in {str(n.get("props", {}).get("id", n.get("id", ""))) for n in result["trace"]["nodes"]}
        # Decisions in the graph are keyed as Decision:<repo>#<number>-<ordinal>.
        source_ok = source_ok or any(expected_id in str(n.get("id", "")) for n in result["trace"]["nodes"])
        if not source_ok:
            failures.append(f"Ask {index}: expected source decision absent from trace")
        print(f"\nASK {index}: {question}")
        print(f"Expected source decision: {source_summary} ({expected_source})")
        print(f"Cited references: {', '.join(dict.fromkeys(citations)) or '(none)'}")
        print("Answer:", answer)
        print(f"Trace: {len(result['trace']['nodes'])} nodes / {len(result['trace']['edges'])} edges; expected decision {'PASS' if source_ok else 'FAIL'}")

    guardian._mcp_mode = "local"
    runs = [guardian._guard_impl(2, config.GUARD_REPO) for _ in range(2)]
    def signature(result: dict) -> tuple:
        return (
            tuple(result.get("changed_files", [])),
            tuple((c.get("summary"), bool(c.get("conflict"))) for c in result.get("decision_checks", [])),
            tuple(result.get("reviewers", [])),
            result.get("drafted_comment"),
        )
    same = signature(runs[0]) == signature(runs[1])
    print("\nGUARDIAN PR #2 dry-run comparison:")
    for number, result in enumerate(runs, 1):
        print(f"Run {number}: files={result.get('changed_files')}; checks={[(c['summary'], c['conflict']) for c in result.get('decision_checks', [])]}; reviewers={result.get('reviewers')}")
    print("Drafts identical:", same)
    print("Conflict verdicts identical:", [c.get("conflict") for c in runs[0].get("decision_checks", [])] == [c.get("conflict") for c in runs[1].get("decision_checks", [])])
    if not same:
        failures.append("Guardian dry-run output differed between runs")
    print("\nFAILURES:", failures or "none")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
