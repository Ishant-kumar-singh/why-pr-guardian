# Hackathon submission

## Project title

Why? — the PR Guardian that knows the reasons

## One-line pitch

An evidence-backed Guardian checks new pull requests against the decisions that shaped the code.

## Track

Track 1: Agents that Act on Connected Data

## Description

Pull request reviews often lose the reasoning behind a code choice. A future change can quietly reverse it because that context lives in old PR discussions, issues, and review comments. Why? loads a public repository into FalkorDB and extracts decisions with links to their source PRs or issues and affected files. The Guardian reads the files changed by a new pull request, finds relevant decisions, and checks the strongest matches against the patch. It reports conflicts with the source rationale, lists related files, and suggests reviewers using collaboration data. It drafts a review comment with source links and avoids notifying suggested reviewers through mentions. The Ask agent answers questions about code by searching for candidate decisions and following graph relationships. The page shows the answer or Guardian result beside a compact trace graph, so reviewers can inspect which files, decisions, PRs, issues, and people contributed to the result. Bus Factor provides a separate view of contributor concentration and connector scores. GitHub writes are off by default and limited to the configured fork.

## How FalkorDB is used

FalkorDB stores the repository entities and their relationships. MERGE-based ingestion keeps nodes and edges stable across reruns. Cypher traversals connect changed files to decisions and their PR or issue sources, while full-text indexes locate candidate decisions. PageRank over co-changed files estimates blast radius; stored betweenness over collaboration edges ranks reviewers. The stdio MCP server exposes the same graph tools, with an in-process fallback used in the checked demo.

## Tech stack

Python, FastAPI, FalkorDB, Cypher, MCP, GitHub REST API, OpenAI-compatible LLM endpoint, and Cytoscape.js.

## Links

- Repository URL: **[ADD REPOSITORY URL]**
- Demo video URL: **[ADD DEMO VIDEO URL]**
- Blog URL: **[ADD PUBLISHED BLOG URL]**
