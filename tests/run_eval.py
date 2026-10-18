"""Evaluation runner for the question agent.

Load eval_questions.json, run each question through the agent, check whether
the expected PRs/issues/people appear in the trace, and report pass/fail.

Run:  python tests/run_eval.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent


def main() -> int:
    eval_file = Path(__file__).parent / "eval_questions.json"
    questions = json.loads(eval_file.read_text())
    
    print("Running evaluation questions...")
    print("=" * 60)
    
    passed = 0
    total = 0
    
    for item in questions:
        question = item["question"]
        print(f"\nQ: {question}")
        
        try:
            result = agent.ask(question, max_steps=6)
            answer = result["answer"]
            trace = result["trace"]
            
            print(f"A: {answer[:200]}{'...' if len(answer) > 200 else ''}")
            print(f"Traced: {len(trace['nodes'])} nodes, {len(trace['edges'])} edges")
            
            # Check expected results (when filled in)
            checks = []
            if item.get("expected_prs"):
                pr_numbers = {n["props"].get("number") for n in trace["nodes"] if n["type"] == "PullRequest"}
                for expected in item["expected_prs"]:
                    checks.append(("PR", expected, expected in pr_numbers))
            
            if item.get("expected_issues"):
                issue_numbers = {n["props"].get("number") for n in trace["nodes"] if n["type"] == "Issue"}
                for expected in item["expected_issues"]:
                    checks.append(("Issue", expected, expected in issue_numbers))
            
            if item.get("expected_people"):
                people = {n["props"].get("login") for n in trace["nodes"] if n["type"] == "Person"}
                for expected in item["expected_people"]:
                    checks.append(("Person", expected, expected in people))
            
            if item.get("expected_files"):
                files = {n["props"].get("path") for n in trace["nodes"] if n["type"] == "File"}
                for expected in item["expected_files"]:
                    checks.append(("File", expected, expected in files))

            if item.get("expected_decisions"):
                decisions = {n.get("label") for n in trace["nodes"] if n["type"] == "Decision"}
                for expected in item["expected_decisions"]:
                    checks.append(("Decision", expected, expected in decisions))
            
            if checks:
                for check_type, value, found in checks:
                    status = "✓" if found else "✗"
                    print(f"  {status} Expected {check_type} {value}: {'found' if found else 'missing'}")
                if all(c[2] for c in checks):
                    passed += 1
                total += 1
            else:
                print(f"  (no assertions; see note: {item.get('note')})")
        
        except Exception as err:
            print(f"  ERROR: {err}")
    
    print("\n" + "=" * 60)
    if total > 0:
        print(f"Passed {passed}/{total} questions")
        eval_exit = 0 if passed == total else 1
    else:
        print("No assertions to check; fill in expected values in eval_questions.json")
        eval_exit = 0

    guardian_file = Path(__file__).parent / "guardian_eval_cases.json"
    guardian_cases = json.loads(guardian_file.read_text(encoding="utf-8"))
    print("\nGuardian cases (manual placeholders; fill after checking by hand):")
    for item in guardian_cases:
        print(f"  - {item['case']}: PR {item['pr_number']}")
        for key, value in item.items():
            if key.startswith("expected_"):
                print(f"      {key}: {value}")
    return eval_exit


if __name__ == "__main__":
    raise SystemExit(main())
