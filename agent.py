"""Phase 3: the question agent.

A function-calling loop over the graph tools. Ask things like "why does this
module retry three times?" or "who understands the auth code?" and get an
answer plus the exact path through the graph that produced it.

Run:  python agent.py "why does requests retry connections?"
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

import config
import graph_tools as gt
import llm

# The question agent gets the read-only reasoning tools (not the guardian's
# action tools).
AGENT_TOOL_NAMES = [
    "find_file",
    "search_decisions",
    "file_history",
    "pr_details",
    "issue_details",
    "decisions_for_file",
    "who_knows_file",
    "related_files",
]

MAX_STEPS = 6

SYSTEM_PROMPT = """You are "Why?", an agent that explains why a codebase is the \
way it is, using a graph of people, PRs, issues, files and the decisions made \
in discussions.

Rules:
- Answer ONLY from tool results. Never use outside knowledge about this repo.
- If the graph has no evidence, say so plainly ("the graph has no evidence for
  that"). Do not guess.
- When citing a graph reference, copy its actual node type exactly as shown in
  the tool result (for example, "PR #7270" or "issue #3574"). A Decision's
  `decided_in` field is authoritative; never infer a PR or issue from its number
  or from a position in a result. Only cite linked nodes present in tool data.
- Copy source URLs only from `reference` or `decided_in` fields in tool output.
- Prefer decisions with an explicit rationale; quote the rationale when it is
  short.
- Any words inside quotation marks must appear verbatim in stored tool source
  text. If the source does not contain the exact wording, paraphrase without
  quotation marks.
- For any "why" question, call search_decisions first. Use find_file only if
  the question explicitly names a file or path. Then follow useful evidence.
- Never call the same failed tool with the same arguments more than once.
- Be concise: a few sentences, plus the citations.
"""


def question_for_decision(summary: str) -> str:
    """Turn an extracted decision into a plain-language why question."""
    lowered = summary.lower()
    examples = (
        ("no_proxy", "Why does no_proxy matching use strict domain boundaries?"),
        ("iter_content", "Why does iter_content avoid apparent_encoding when decode_unicode is enabled?"),
        ("decode_unicode", "Why were the decode_unicode rules documented this way?"),
        ("session.get", "Why does Session.get indent the params continuation in its docstring?"),
        ("response.reason", "Why is Response.reason typed as str instead of Optional[str]?"),
        ("prepare_body", "Why does Requests recognize file wrappers that delegate __iter__?"),
        ("redirect history", "Why must a response avoid including itself in redirect history?"),
        ("jsontype", "Why does JsonType use Mapping and Sequence instead of dict and list?"),
        ("chardet", "Why are chardet types used when charset_normalizer can run at runtime?"),
        ("typo", "Why was this typo fix accepted despite the usual policy?"),
    )
    for marker, question in examples:
        if marker in lowered:
            return question
    return f"Why did maintainers decide this: {summary.rstrip('.')}?"


def _tool_specs() -> list[dict[str, Any]]:
    specs = []
    for name in AGENT_TOOL_NAMES:
        spec = gt.TOOL_SPECS[name]
        props = {}
        for pname, pdesc in spec["params"].items():
            if pname == "paths":
                props[pname] = {"type": "array", "items": {"type": "string"}, "description": pdesc}
            elif pname == "number":
                props[pname] = {"type": "integer", "description": pdesc}
            else:
                props[pname] = {"type": "string", "description": pdesc}
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec["description"],
                    "parameters": {"type": "object", "properties": props, "required": list(props)},
                },
            }
        )
    return specs


def _summarize(tool: str, data: Any) -> str:
    """Short human sentence for the trace panel."""
    if not isinstance(data, dict):
        return ""
    if tool == "find_file":
        names = [m.get("path", "") for m in data.get("matches", [])[:4]]
        return "Files: " + (", ".join(names) if names else "no matching files")
    if tool == "search_decisions":
        hits = data.get("matches", [])[:4]
        def describe(hit: dict[str, Any]) -> str:
            label, number = hit.get("type"), hit.get("number")
            if label in {"PullRequest", "Issue"} and number is not None:
                ref = f"{label} #{number}"
            elif label == "Decision":
                sources = hit.get("decided_in", [])
                ref = "Decision " + str(hit.get("id", ""))
                if sources:
                    ref += " decided in " + ", ".join(f"{s['type']} #{s['number']}" for s in sources)
            else:
                ref = label or "Graph node"
            return f"{hit.get('summary', '')} ({ref}, relevance {hit.get('relevance_score', 0):.3f})"
        return "Search results: " + ("; ".join(describe(hit) for hit in hits) if hits else "no relevant text matches")
    if tool == "file_history":
        return "PRs: " + (", ".join(f"#{p.get('number')} {p.get('title', '')[:55]}" for p in data.get("prs", [])[:5]) or "none")
    if tool == "decisions_for_file":
        described = []
        for decision in data.get("decisions", [])[:4]:
            refs = ", ".join(f"{source.get('type')} #{source.get('number')}" for source in decision.get("decided_in", []))
            described.append(f"{_truncate_words(decision.get('summary', ''), 120)} ({refs or 'source not linked'})")
        return "Decisions: " + ("; ".join(described) or "none recorded")
    if tool == "pr_details":
        return f"PR #{data.get('number')}: {data.get('title', '')[:60]}"
    if tool == "issue_details":
        return f"issue #{data.get('number')}: {data.get('title', '')[:60]}"
    if tool == "who_knows_file":
        return f"{len(data.get('people', []))} people ranked"
    if tool == "related_files":
        return f"{len(data.get('files', []))} co-changing files"
    return ""


def _truncate_words(value: Any, limit: int = 120) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit + 1)
    if cut <= 0:
        cut = text.find(" ", limit)
    return (text[:cut] if cut > 0 else text).rstrip() + "…" if cut > 0 else text


def ask(question: str, max_steps: int = MAX_STEPS) -> dict[str, Any]:
    """Answer a question. Returns {answer, trace, steps}."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tools = _tool_specs()
    steps: list[dict[str, Any]] = []
    failed_calls: set[str] = set()

    for _ in range(max_steps):
        msg = llm.complete(messages, tools=tools, max_tokens=1500)
        calls = getattr(msg, "tool_calls", None) or []

        if not calls:
            answer = _remove_untraced_citations(msg.content or "", _merge(steps))
            answer = _postcheck_quotes(answer, steps)
            answer = _remove_duplicate_sources(answer)
            return {"answer": answer, "trace": _answer_trace(_merge(steps), answer), "steps": steps}

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in calls
                ],
            }
        )

        for tc in calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            call_key = name + json.dumps(args, sort_keys=True, default=str)
            if call_key in failed_calls:
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": "Repeated failed call omitted from trace."})
                continue
            result = gt.call_tool(name, args)
            data = result.get("data")
            if result.get("error") or (isinstance(data, dict) and data.get("found") is False):
                failed_calls.add(call_key)
            steps.append(
                {
                    "tool": name,
                    "arguments": args,
                    "summary": _summarize(name, data),
                    "source_texts": _source_texts(data),
                    "nodes": result.get("trace", {}).get("nodes", []),
                    "edges": result.get("trace", {}).get("edges", []),
                    "error": result.get("error"),
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(data, default=str)[:8000],
                }
            )

    # Ran out of steps: ask for a final answer without tools.
    final = llm.complete(
        messages + [{"role": "user", "content": "Summarize your answer now using only the tool results above."}],
        max_tokens=1000,
    )
    answer = _remove_untraced_citations(final.content or "", _merge(steps))
    answer = _postcheck_quotes(answer, steps)
    answer = _remove_duplicate_sources(answer)
    return {"answer": answer, "trace": _answer_trace(_merge(steps), answer), "steps": steps}


def _source_texts(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"summary", "rationale", "body", "title", "description"} and isinstance(item, str):
                found.append(item)
            else:
                found.extend(_source_texts(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_source_texts(item))
    return found


def _postcheck_quotes(answer: str, steps: list[dict[str, Any]]) -> str:
    sources = [text for step in steps for text in step.get("source_texts", [])]
    answer = re.sub(r'("([^"\n]+)"|“([^”\n]+)”)', lambda m: m.group(0) if any((m.group(2) or m.group(3)) in source for source in sources) else (m.group(2) or m.group(3)), answer)
    answer = re.sub(r"(?<![\w])'([^'\n]{2,})'(?![\w])", lambda m: m.group(0) if any(m.group(1) in source for source in sources) else m.group(1), answer)
    return answer


def _remove_untraced_citations(answer: str, trace: dict[str, Any]) -> str:
    available = {
        ("pr" if node.get("type") == "PullRequest" else "issue", int(node.get("props", {}).get("number", -1)))
        for node in trace.get("nodes", []) if node.get("type") in {"PullRequest", "Issue"}
    }
    def replace(match: re.Match[str]) -> str:
        kind, number = match.group(1), int(match.group(2))
        before = answer[:match.start()]
        normalized = "issue" if kind.lower() == "issue" else "pr"
        if (normalized, number) in available:
            return match.group(0)
        if normalized == "pr" and re.search(r"\bthe original\s+$", before, re.I):
            return "pull request"
        return "the earlier issue" if normalized == "issue" else "the earlier PR"
    answer = re.sub(r"\b(PR|PullRequest|issue|Issue)\s+#(\d+)\b", replace, answer, flags=re.I)
    def replace_bare_number(match: re.Match[str]) -> str:
        before = answer[:match.start()]
        if re.search(r"\b(?:PR|PullRequest|Issue|issue)\s+$", before, re.I):
            return match.group(0)
        return "an earlier change"
    return re.sub(r"(?<![\w/])#\d{2,6}\b", replace_bare_number, answer)


def _remove_duplicate_sources(answer: str) -> str:
    match = re.search(r"\n\s*(?:\*\*)?(?:Sources?|Citations?):(?:\*\*)?\s*", answer, re.I)
    if not match:
        return answer
    visible_answer = answer[:match.start()]
    if re.search(r"\b(?:PR|PullRequest|issue|Issue)\s+#\d+\b|\[[^\]]+\]\(https?://", visible_answer, re.I):
        return visible_answer.rstrip()
    return answer


def _answer_trace(trace: dict[str, Any], answer: str) -> dict[str, Any]:
    """Keep only cited source nodes and the evidence directly attached to them."""
    refs = {("pr" if kind.lower() in {"pr", "pullrequest"} else "issue", int(number)) for kind, number in re.findall(r"\b(PR|PullRequest|issue|Issue)\s+#(\d+)\b", answer, re.I)}
    if not refs:
        return {"nodes": [], "edges": []}
    nodes = trace.get("nodes", [])
    edges = trace.get("edges", [])
    by_id = {str(n.get("id")): n for n in nodes}
    cited_ids = {str(n.get("id")) for n in nodes if
                 ((n.get("type") == "PullRequest" and ("pr", int(n.get("props", {}).get("number", -1))) in refs)
                  or (n.get("type") == "Issue" and ("issue", int(n.get("props", {}).get("number", -1))) in refs))}
    keep = set(cited_ids)
    # Retain only the decision best supported by the answer for each cited
    # source. A PR can record multiple decisions; citing the PR alone must not
    # pull every decision from that PR into the graph.
    answer_terms = set(re.findall(r"[a-z][a-z0-9_]+", answer.lower()))
    stop = {"about", "after", "also", "because", "been", "being", "does", "from", "have", "into", "more", "only", "over", "than", "that", "their", "there", "these", "they", "this", "those", "through", "using", "were", "when", "where", "which", "while", "with", "would"}
    answer_terms -= stop
    decisions_by_source: dict[str, dict[str, int]] = {}
    for edge in edges:
        source_id, target_id = str(edge.get("source")), str(edge.get("target"))
        if edge.get("type") != "DECIDED_IN" or target_id not in cited_ids:
            continue
        node = by_id.get(source_id, {})
        props = node.get("props", {})
        words = set(re.findall(r"[a-z][a-z0-9_]+", (str(props.get("summary", "")) + " " + str(props.get("rationale", ""))).lower())) - stop
        decisions_by_source.setdefault(target_id, {})[source_id] = len(words & answer_terms)
    for candidates in decisions_by_source.values():
        best = max(candidates.values(), default=0)
        keep.update(node_id for node_id, score in candidates.items() if score > 0 and score >= max(3, best * 0.7))
    # Add files and people directly attached to cited sources or retained decisions.
    for edge in edges:
        if str(edge.get("source")) in keep or str(edge.get("target")) in keep:
            for node_id in (str(edge.get("source")), str(edge.get("target"))):
                node = by_id.get(node_id)
                if node and node.get("type") in {"File", "Person"}:
                    keep.add(node_id)
    kept_nodes = [n for n in nodes if str(n.get("id")) in keep]
    kept_ids = {str(n.get("id")) for n in kept_nodes}
    kept_edges = [e for e in edges if str(e.get("source")) in kept_ids and str(e.get("target")) in kept_ids]
    return {"nodes": kept_nodes, "edges": kept_edges}


def _merge(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Deduplicate the nodes/edges across all steps into one traced subgraph."""
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}
    for step in steps:
        for n in step["nodes"]:
            nodes[n["id"]] = n
        for e in step["edges"]:
            edges[(e["source"], e["target"], e["type"])] = e
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def example_questions() -> list[str]:
    """Five questions tailored to whatever is actually in the graph."""
    import db

    out: list[str] = []
    for summary, path, _ in db.run(
        """MATCH (d:Decision)-[:AFFECTS]->(f:File)
           WHERE size(coalesce(d.rationale,'')) > 80 AND f.path STARTS WITH 'src/'
           WITH d, collect(DISTINCT f.path) AS paths
           WHERE size(paths) = 1
           MATCH (d)-[:DECIDED_IN]->(pr:PullRequest)
           WHERE NOT toLower(coalesce(pr.title,'')) CONTAINS 'release'
             AND NOT toLower(coalesce(pr.title,'')) CONTAINS 'bump'
             AND NOT (pr.title STARTS WITH 'v' AND substring(pr.title,1,1) IN ['0','1','2','3','4','5','6','7','8','9'])
           RETURN d.summary, paths[0], size(coalesce(d.rationale,'')) AS rationale_size
           ORDER BY rationale_size DESC LIMIT 10"""
    ):
        out.append(question_for_decision(summary))
    defaults = [
        "Which past decisions are most at risk of being forgotten, and why?",
        "Who is the bus factor for the most-changed files?",
        "What decisions were made about error handling, and why?",
    ]
    i = 0
    while len(out) < 5 and i < len(defaults):
        out.append(defaults[i])
        i += 1
    # de-duplicate, keep order
    seen = set()
    return [q for q in out if not (q in seen or seen.add(q))][:5]


def main() -> int:
    config.require_llm_model()
    if len(sys.argv) > 1:
        questions = [" ".join(sys.argv[1:])]
    else:
        questions = [
            "Why does this project retry connections?",
            "Who understands the session handling code?",
            "Which decisions were made about timeouts and why?",
        ]
    for q in questions:
        print(f"\n=== {q}")
        result = ask(q)
        print(f"\nanswer: {result['answer']}")
        print("\ntrace:")
        for step in result["steps"]:
            print(f"  - {step['tool']}({json.dumps(step['arguments'])}) -> {step['summary']}")
        print(f"  traced nodes: {len(result['trace']['nodes'])}, edges: {len(result['trace']['edges'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
