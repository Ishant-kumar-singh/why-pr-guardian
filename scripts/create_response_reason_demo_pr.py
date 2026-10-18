"""Create a draft GUARD_REPO PR that reverses graph decision #7437."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from github_client import GitHub


BRANCH = "why-demo-response-reason-optional-7437"
PATH = "src/requests/models.py"


def main() -> int:
    repo = config.GUARD_REPO
    if not repo:
        print("GUARD_REPO is unset; refusing to write anywhere.")
        return 2
    gh = GitHub()
    if not gh.token:
        print("GITHUB_TOKEN is required to create a PR on GUARD_REPO.")
        return 2

    owner = repo.split("/", 1)[0]
    metadata = gh.get(f"/repos/{repo}")
    if not metadata:
        print(f"GUARD_REPO {repo} is unavailable.")
        return 1
    base = metadata["default_branch"]
    pulls = gh.get(
        f"/repos/{repo}/pulls",
        {"state": "all", "head": f"{owner}:{BRANCH}", "base": base},
    ) or []
    if pulls:
        pr = pulls[0]
        print(f"Existing PR #{pr['number']}: {pr['html_url']}")
        return 0

    existing_ref = gh.get(f"/repos/{repo}/git/ref/heads/{quote(BRANCH, safe='')}")
    if existing_ref:
        print(f"Branch {BRANCH} already exists without a matching PR; refusing to overwrite it.")
        return 1

    base_ref = gh.get(f"/repos/{repo}/git/ref/heads/{quote(base, safe='')}")
    base_sha = base_ref["object"]["sha"]
    base_commit = gh.get(f"/repos/{repo}/git/commits/{base_sha}")
    file_data = gh.get(f"/repos/{repo}/contents/{PATH}", {"ref": base})
    if not base_commit or not file_data:
        print(f"Could not read {repo}:{base}/{PATH}.")
        return 1

    source = base64.b64decode(file_data["content"]).decode("utf-8")
    before, marker, response = source.partition("class Response:")
    if not marker or "reason: str" not in response:
        print("Expected Response.reason: str in the GUARD_REPO base file; no branch created.")
        return 1
    response = response.replace("reason: str", "reason: Optional[str]", 1)
    if "Optional," not in before:
        before = before.replace("    Literal,\n", "    Literal,\n    Optional,\n", 1)
    changed = before + marker + response

    client = gh._client
    headers = gh._headers()
    blob = client.post(
        f"/repos/{repo}/git/blobs",
        json={"content": changed, "encoding": "utf-8"},
        headers=headers,
    )
    blob.raise_for_status()
    tree = client.post(
        f"/repos/{repo}/git/trees",
        json={
            "base_tree": base_commit["tree"]["sha"],
            "tree": [{"path": PATH, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]}],
        },
        headers=headers,
    )
    tree.raise_for_status()
    commit = client.post(
        f"/repos/{repo}/git/commits",
        json={
            "message": "Demo: restore optional Response.reason typing",
            "tree": tree.json()["sha"],
            "parents": [base_sha],
        },
        headers=headers,
    )
    commit.raise_for_status()
    ref = client.post(
        f"/repos/{repo}/git/refs",
        json={"ref": f"refs/heads/{BRANCH}", "sha": commit.json()["sha"]},
        headers=headers,
    )
    ref.raise_for_status()
    pr = client.post(
        f"/repos/{repo}/pulls",
        json={
            "title": "Demo: restore Optional typing for Response.reason",
            "head": BRANCH,
            "base": base,
            "draft": True,
            "body": (
                "This demo reverses graph decision #7437: `Response.reason` was made `str`, "
                "with the rationale that urllib3 supplies an empty string when the status line "
                "omits it. This patch restores `Optional[str]` in `src/requests/models.py`.\n\n"
                "Decision PR: https://github.com/psf/requests/pull/7437\n"
                "Decision issue endpoint: https://github.com/psf/requests/issues/7437\n\n"
                "Created as a draft for the Why? Guardian dry-run demonstration."
            ),
        },
        headers=headers,
    )
    pr.raise_for_status()
    result = pr.json()
    cache = config.CACHE_DIR / "guardian-demo-pr.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"number": result["number"], "url": result["html_url"]}), encoding="utf-8")
    print(f"Created branch: {BRANCH}")
    print(f"Changed file: {PATH} (Response.reason: str -> Optional[str])")
    print(f"Draft PR #{result['number']}: {result['html_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
