"""Exercise the no-conflict output path without creating another GitHub PR."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import guardian


def main() -> int:
    if not config.DRY_RUN:
        print("Set DRY_RUN=true before running this no-write fixture.")
        return 2

    original = guardian.check_decision_conflict

    def fixture_no_conflict(decision: dict, patch: str) -> dict:
        summary = decision.get("summary", "this recorded decision")
        return {
            "data": {
                "conflict": False,
                "reason": f"Fixture verdict: the patch does not undo '{summary}'.",
                "decision_id": decision.get("id"),
            },
            "trace": {"nodes": [], "edges": []},
        }

    guardian.check_decision_conflict = fixture_no_conflict
    try:
        result = guardian.guard(2, config.GUARD_REPO)
    finally:
        guardian.check_decision_conflict = original

    if result.get("error"):
        print("No-conflict fixture failed:", result["error"])
        return 1
    print("NO-CONFLICT OUTPUT FIXTURE (only conflict verdict is simulated; PR/files/graph are live)")
    print("Changed files:", ", ".join(result["changed_files"]))
    print(result["decision_check_note"])
    for item in result["decision_checks"]:
        print(f"  NO CONFLICT {item['decision_id']}: {item['reason']}")
    print("Drafted comment:", result["drafted_comment"] or "none")
    print("Reviewers:", result["reviewers"] or "none (no conflict)")
    print("Trace:", len(result["trace"]["nodes"]), "nodes,", len(result["trace"]["edges"]), "edges")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
