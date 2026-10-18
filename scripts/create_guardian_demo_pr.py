"""Create an idempotent draft PR on GUARD_REPO that reverses decision #7425."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from github_client import GitHub


def main() -> int:
    repo = config.GUARD_REPO
    if not repo:
        print("GUARD_REPO must be explicitly configured; refusing to write anywhere else.")
        return 2
    branch = "why-guardian-demo-hooks-undo"
    gh = GitHub()
    if not gh.token:
        print("GITHUB_TOKEN is required to create a PR on GUARD_REPO.")
        return 2

    repo_data = gh.get(f"/repos/{repo}")
    if not repo_data:
        print(f"GUARD_REPO {repo} not found or not accessible.")
        return 1
    base = repo_data["default_branch"]
    pulls = gh.get(f"/repos/{repo}/pulls", {"state": "all", "head": f"{repo.split('/')[0]}:{branch}", "base": base}) or []
    if pulls:
        (config.CACHE_DIR / "guardian-demo-pr.json").write_text(json.dumps({"number": pulls[0]["number"], "url": pulls[0]["html_url"]}), encoding="utf-8")
        print(pulls[0]["html_url"])
        print(f"PR #{pulls[0]['number']}: {pulls[0]['title']}")
        return 0

    ref = gh.get(f"/repos/{repo}/git/ref/heads/{quote(base, safe='')}")
    if not ref:
        print(f"Could not read {repo}:{base}.")
        return 1
    parent_sha = ref["object"]["sha"]
    parent = gh.get(f"/repos/{repo}/git/commits/{parent_sha}")
    file_data = gh.get(f"/repos/{repo}/contents/src/requests/sessions.py", {"ref": base})
    if not parent or not file_data:
        print("Could not read the base commit or src/requests/sessions.py.")
        return 1
    content = base64.b64decode(file_data["content"]).decode("utf-8")
    if "HooksInputType" not in content:
        print("The current sessions.py no longer contains HooksInputType; choose a fresh decision before creating a demo PR.")
        return 1
    changed = content.replace("HooksInputType", "HooksType")
    if changed == content:
        print("No change generated.")
        return 1

    client = gh._client
    headers = gh._headers()
    blob = client.post(f"/repos/{repo}/git/blobs", json={"content": changed, "encoding": "utf-8"}, headers=headers)
    if blob.status_code >= 400:
        print(f"GitHub blob creation failed: {blob.status_code} {blob.text[:200]}")
        return 1
    tree = client.post(
        f"/repos/{repo}/git/trees",
        json={"base_tree": parent["tree"]["sha"], "tree": [{"path": "src/requests/sessions.py", "mode": "100644", "type": "blob", "sha": blob.json()["sha"]}]},
        headers=headers,
    )
    if tree.status_code >= 400:
        print(f"GitHub tree creation failed: {tree.status_code} {tree.text[:200]}")
        return 1
    commit = client.post(
        f"/repos/{repo}/git/commits",
        json={"message": "Demo: reverse the hook input typing decision", "tree": tree.json()["sha"], "parents": [parent_sha]},
        headers=headers,
    )
    if commit.status_code >= 400:
        print(f"GitHub commit creation failed: {commit.status_code} {commit.text[:200]}")
        return 1
    created_ref = client.post(f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit.json()["sha"]}, headers=headers)
    if created_ref.status_code >= 400:
        print(f"GitHub branch creation failed: {created_ref.status_code} {created_ref.text[:200]}")
        return 1
    pr = client.post(
        f"/repos/{repo}/pulls",
        json={
            "title": "Demo: reverse the Session.request hook typing decision",
            "head": branch,
            "base": base,
            "draft": True,
            "body": "This draft deliberately reverses the decision recorded in upstream PR #7425: Session.request accepts HooksInputType | None, not HooksType. The change is limited to src/requests/sessions.py so Why? can trace the affected file and explain the prior choice. Created for a DRY_RUN guardian demonstration.",
        },
        headers=headers,
    )
    if pr.status_code >= 400:
        print(f"GitHub PR creation failed: {pr.status_code} {pr.text[:250]}")
        return 1
    data = pr.json()
    (config.CACHE_DIR / "guardian-demo-pr.json").write_text(json.dumps({"number": data["number"], "url": data["html_url"]}), encoding="utf-8")
    print(data["html_url"])
    print(f"PR #{data['number']}: {data['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
