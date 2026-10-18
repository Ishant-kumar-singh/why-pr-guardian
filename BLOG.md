# Giving a code decision a path back to its source

When I first opened this project, the core question was simple: how can a pull request review remember why a piece of code was written a certain way? A line annotation tells me what the code currently says. A commit tells me who changed it. Neither necessarily tells me why. That reason can be buried in a review comment, an issue, or a discussion that happened months ago.

Why? stores that context in FalkorDB. It represents people, files, pull requests, issues, and extracted decisions as nodes. Relationships connect the decision to the PR or issue where it was made, and to the files it affects. The Guardian takes a new PR, reads its changed files, finds decisions attached to those files, and asks whether the patch appears to undo one. It can then trace related files and rank reviewers.

The graph is a small view of the files and decisions used in a run, not a picture of the whole repository. Ask uses the same graph tools. Search normalizes `Response.reason` to `response|reason`, returns relevance scores, and drops weak matches before following relationships.

## A real Guardian run

For the demo I created draft [PR #2 on my `Ishant-kumar-singh/requests` fork](https://github.com/Ishant-kumar-singh/requests/pull/2). It changes one file: `src/requests/models.py`. The edit changes `Response.reason: str` to `Response.reason: Optional[str]`. I checked the graph before choosing the example: decision #7437 is attached to that exact file and linked to upstream [PR #7437](https://github.com/psf/requests/pull/7437).

There is no separate Issue node for #7437 in the graph. The result cites the one actual source node: upstream PR #7437, using its PR URL.

I first ran the Guardian with `DRY_RUN=true`. It found ten decisions attached to `src/requests/models.py` and checked the top five concurrently, ranked by patch text overlap plus graph proximity. Its result said `checked 5 of 10`; the #7437 conflict was the only conflict in that set. Each other checked decision had a one-line reason explaining why the type-only patch did not affect it.

The model marked #7437 as a conflict. The stored rationale explains that urllib3 supplies an empty string when no reason phrase is present in the status line. That means a `Response` created by Requests can avoid `None` checks, though manual construction may differ. The Guardian drafted a comment quoting the rationale and linking only the actual PR source node. The live result is recorded below after the GitHub check.

The blast radius contained seven related paths, including `src/requests/sessions.py`, `src/requests/_types.py`, and `tests/test_requests.py`. FalkorDB betweenness ranked `nateprewitt`, `jorenham`, and `sigmavirus24` among the top reviewers, with scores shown. It excluded PR author `Ishant-kumar-singh`. The final graph shows the PR, changed file, five checked decisions, the conflicting decision's source PR, the top reviewers, and co-change evidence, capped at 15 nodes.

For the requested live check, I temporarily set `DRY_RUN=false` and reran Guardian on PR #2. GitHub accepted the review comment; [open the posted review](https://github.com/Ishant-kumar-singh/requests/pull/2#pullrequestreview-5557864275). The reviewer request was rejected with HTTP 422 because GitHub permits requests only for repository collaborators. I checked the fork's collaborator list: it contains only `Ishant-kumar-singh`, who is the PR author and is excluded from reviewer suggestions. No reviewer request was recorded. I restored `DRY_RUN=true` immediately after the run.

The Guardian run reports `local` MCP mode and queried FalkorDB through the in-process tool path.

Three failures shaped the build. Betweenness returned zero because I treated collaboration edges as directed; scoring the undirected paths fixed it. The Guardian could hang on a model call, so I added a timeout. My first file counts were inflated by joins between PRs, authors, and reviews; I changed the calculation to distinct PR sets and audited `sessions.py` against cached GitHub data.

## Keeping an empty result useful

A no-conflict result must show what it checked, not just a green label. The UI now lists changed files, checked decisions, and one reason per check. I exercised that path with live PR files and graph data while replacing only the conflict verdict with a fixture verdict. It is a display test, not a second model judgment about PR #2.

The API sends graph nodes and edges with Guardian results. The page waits for the active tab before creating Cytoscape, then resizes and fits it. Each tab keeps its graph data, so returning to Ask does not require repeating the question.

Search splits dotted names, joins terms with FalkorDB’s OR syntax, scores matches, and filters weak candidates. In a live test, #7437 ranked highly for `Response.reason`. I asked `Why is Response.reason typed as str?` and got an answer citing #7437 with a traced graph of decisions, files, PRs, issues, and people. The display caps at 15 nodes.

I value that I can follow PR #2 from the changed annotation in `src/requests/models.py` to decision #7437 and its rationale. When the chain is missing, the tool can show the gap instead of inventing a reason.

## Counts and limits

The Bus Factor page audits `src/requests/sessions.py`. FalkorDB counted ten distinct merged PRs, matching the ten in cached GitHub data across the 150 PRs examined. Counting distinct PRs avoids inflated totals from joins to authors, reviewers, or files.

There are limits. The graph covers 150 merged PRs, not the full history. Extracted decisions depend on the discussion and model. Conflict checks can be wrong, and this run checked only the five strongest matches. Connector scores describe this dataset, not engineering skill. A drafted comment still needs human review.
