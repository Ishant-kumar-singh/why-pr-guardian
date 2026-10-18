# Guardian Demo

The demo PR is [#2 on `Ishant-kumar-singh/requests`](https://github.com/Ishant-kumar-singh/requests/pull/2). It changes only `src/requests/models.py`, from `Response.reason: str` to `Response.reason: Optional[str]`. The graph confirms that decision #7437 is attached to this file. The decision links to upstream [PR #7437](https://github.com/psf/requests/pull/7437); there is no separate Issue node for this decision, so the Guardian links only to the PR source.

## Run the checked demo

Keep `DRY_RUN=true` in `.env`, with `GUARD_REPO`, `GITHUB_TOKEN`, and `LLM_MODEL` set.

```powershell
.venv\Scripts\python.exe scripts\create_response_reason_demo_pr.py
.venv\Scripts\python.exe scripts\run_guardian_demo.py 2
```

The create script uses the GitHub API and is idempotent for this branch. The Guardian runner prints the changed file, all decisions found, the five top-overlap checks, blast radius, reviewer scores, the draft comment, and graph counts. It saves the full JSON result, including the traced subgraph, to `.cache/guardian-demo-output.json`. The checked run returned 10 decisions for the file, found #7437 to conflict, ranked reviewers with FalkorDB betweenness over collaboration edges, and stayed in dry-run mode.

## Stream it in the UI

```powershell
.venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`, leave Guardian selected, enter `2`, and click **Check PR**. The changed file appears first; file decisions, conflict checks, blast radius, and reviewer recommendations stream into the steps panel. At completion, the UI shows the evidence summary, trace graph, and drafted comment. No comment or reviewer request is posted.

## No-conflict output path

To see the non-conflict presentation without opening a second remote PR, run:

```powershell
.venv\Scripts\python.exe scripts\run_no_conflict_fixture.py
```

This fixture uses the live PR files and live graph, but replaces only the LLM conflict verdict with a clearly labeled no-conflict fixture verdict. It checks that changed files, checked decisions, one-line reasons, and the graph remain visible when no conflict is reported. It does not post or modify a GitHub PR.

## What the graph contains

The Guardian graph is built from the relationships returned by the run and capped at 15 nodes. It uses left-to-right Dagre layout when the CDN loads and a compact breadth-first fallback otherwise. The graph panel resizes after the active tab becomes visible and after window/container resizing.

## 90-second screen-recording script

| Time | Action and narration |
|---|---|
| 0:00–0:05 (5s) | Open fork PR #2. “This changes `Response.reason` back to `Optional[str]`.” |
| 0:05–0:25 (20s) | Run Guardian. Show the verdict and checked decisions. “The patch conflicts with the decision recorded in PR #7437.” |
| 0:25–0:40 (15s) | Show the trace graph from the changed file to the decision and source PR. |
| 0:40–0:55 (15s) | Show the drafted comment, then the posted review on GitHub. Mention that reviewer requests may be skipped for non-collaborators. |
| 0:55–1:20 (25s) | Ask, “Why is Response.reason typed as str?” Show the cited PR and traced graph. |
| 1:20–1:30 (10s) | Close: “A graph lets the Guardian follow the links from a changed file to the decision and people behind it.” |

### Avoid showing

- Keep the Bus Factor tab to a brief glance.
- Do not show error states, credentials, or unrelated browser tabs.

### Pre-recording checklist

- Server and FalkorDB are running; the Guardian page loads.
- `.env` has `DRY_RUN=true` for a walkthrough that should not post.
- Fork PR #2 is open and the decision graph is loaded.
- Browser zoom is 100%; choose a window size that keeps the Guardian result and graph visible.
