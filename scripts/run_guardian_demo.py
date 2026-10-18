"""Run the DRY_RUN Guardian demo PR and print its full trace."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import guardian
import server


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    config.require_llm_model()
    if not config.DRY_RUN:
        print("Set DRY_RUN=true in .env before running the demo script.")
        return 2
    pr_number = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    pr_file = config.CACHE_DIR / "guardian-demo-pr.json"
    if len(sys.argv) == 1 and pr_file.exists():
        pr_number = int(json.loads(pr_file.read_text(encoding="utf-8"))["number"])
    result = guardian.guard(pr_number, config.GUARD_REPO)
    result["trace"] = server._trace_graph(result.get("trace", {}))
    result["graph"] = result["trace"]
    rendered = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    (config.CACHE_DIR / "guardian-demo-output.json").write_text(rendered, encoding="utf-8")
    if result.get("error"):
        print(f"Guardian error: {result['error']}")
        print(f"Full JSON result: {config.CACHE_DIR / 'guardian-demo-output.json'}")
        return 1
    print(f"Guardian dry-run for PR #{result['pr_number']}: {result['title']}")
    print(f"Repository: {result['repo']} | Author: {result['author']} | MCP mode: {result['mcp_mode']}")
    print("Streamed step summary:")
    for step in result["steps"]:
        print(f"  {step['tool']}: {step['summary']}")
    print(f"Changed files: {', '.join(result['changed_files']) or '(none)'}")
    print(f"Decisions found ({len(result['decisions_found'])}):")
    for decision in result["decisions_found"]:
        refs = ", ".join(f"{('PR' if source.get('type') == 'PullRequest' else 'issue')} #{source.get('number')}" for source in decision.get("decided_in", []))
        print(f"  {decision['summary']} ({refs or 'source not linked'})")
    print(result["decision_check_note"])
    for check in result["decision_checks"]:
        print(f"  {'CONFLICT' if check['conflict'] else 'NO CONFLICT'} {check['summary']}: {check['reason']}")
        print("    Sources: " + (" · ".join(f"{('PR' if source.get('type') == 'PullRequest' else 'issue')} #{source.get('number')} {source.get('url')}" for source in check.get("decided_in", [])) or "none linked"))
        if check["conflict"]:
            decision = next((d for d in result["decisions_found"] if d["id"] == check["decision_id"]), {})
            print(f"    Quoted rationale: {decision.get('rationale', '')}")
    if result["blast_radius"]:
        print("Blast-radius files: " + ", ".join(item["path"] for item in result["blast_radius"]))
    print(f"Connector algorithm: {result['connector_algorithm']}")
    print(f"Reviewers (author excluded: {result['author']}):")
    for reviewer in result["reviewer_details"]:
        print(f"  {reviewer['login']}: score={reviewer['score']:.4f}, connector={reviewer['connector_score']:.6f}")
    if result["drafted_comment"]:
        print("Drafted comment (DRY_RUN; no GitHub write):\n" + result["drafted_comment"])
    else:
        print("No comment: no conflicting decision was found.")
    print(f"Trace graph: {len(result['graph']['nodes'])} nodes, {len(result['graph']['edges'])} edges")
    print(f"Full JSON result: {config.CACHE_DIR / 'guardian-demo-output.json'}")
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
