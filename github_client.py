"""Minimal GitHub REST client with an on-disk response cache.

Design goals:
  * Never pay the API twice for the same URL -- cache every raw response.
  * Respect pagination (Link header) and rate-limit headers (sleep until reset).
  * Treat 404 as "missing" (deleted users, deleted issues) instead of crashing.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlencode

import httpx

import config

CACHE_DIR: Path = config.CACHE_DIR / "gh"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str | None = None, timeout: float = 20.0) -> None:
        self.token = token or config.GITHUB_TOKEN
        if not self.token:
            print("[warn] GITHUB_TOKEN is not set; you only get 60 requests/hour.")
        self._client = httpx.Client(
            base_url=config.GITHUB_API,
            timeout=timeout,
            headers=self._headers(),
            follow_redirects=True,
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "why-pr-guardian",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    # -- low level ---------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        return CACHE_DIR / f"{digest}.json"

    def _get(self, url: str) -> tuple[Any, httpx.Response]:
        """GET with caching. Returns (json_or_None, response).

        The cache stores the response headers we care about (Link, for
        pagination) -- losing them would silently truncate pagination.
        """
        cache_file = self._cache_path(url)
        if cache_file.exists():
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            link = payload.get("link")
            headers = {"link": link} if link else {}
            return payload.get("data"), httpx.Response(
                payload.get("status", 200),
                request=httpx.Request("GET", url),
                headers=headers,
            )

        rate_waited = False
        for attempt in range(3):
            try:
                resp = self._client.get(url)
            except httpx.TimeoutException:
                if attempt < 2:
                    time.sleep(1)
                    continue
                raise GitHubError(f"GET {url} timed out after {attempt + 1} attempts")
            
            remaining = resp.headers.get("x-ratelimit-remaining")
            if resp.status_code in (403, 429) and remaining == "0":
                if rate_waited:
                    raise GitHubError(f"GitHub rate limit still active after a wait capped at 30s: GET {url}")
                reset = int(resp.headers.get("x-ratelimit-reset", "0"))
                wait = min(30, max(1, reset - int(time.time())))
                if wait > 30:
                    raise GitHubError(f"Rate limit wait would exceed 30s (need {wait}s)")
                print(f"[rate-limit] sleeping {wait}s until {time.strftime('%H:%M:%S', time.localtime(reset))}")
                time.sleep(wait)
                rate_waited = True
                continue
            if resp.status_code >= 500:
                if attempt < 2:
                    wait = min(2 ** attempt, 5)  # Cap exponential backoff
                    print(f"[retry] {resp.status_code} on {url}; sleeping {wait}s")
                    time.sleep(wait)
                    continue
            break

        if resp.status_code == 404:
            cache_file.write_text(
                json.dumps({"status": 404, "data": None}), encoding="utf-8"
            )
            return None, resp

        if resp.status_code >= 400:
            raise GitHubError(f"GET {url} -> {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        cache_file.write_text(
            json.dumps(
                {"status": resp.status_code, "data": data, "link": resp.headers.get("link")}
            ),
            encoding="utf-8",
        )
        return data, resp

    def _paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[list[Any]]:
        """Yield pages of a list endpoint, following the Link header."""
        params = dict(params or {})
        params.setdefault("per_page", 100)
        url = f"{path}?{urlencode(params)}"
        while url:
            data, resp = self._get(url)
            if data is None:
                return
            if isinstance(data, list):
                yield data
            # Parse the "next" link.
            nxt = None
            link = resp.headers.get("link")
            if link:
                for part in link.split(","):
                    seg = part.split(";")
                    if len(seg) == 2 and 'rel="next"' in seg[1]:
                        nxt = seg[0].strip().strip("<>")
            if not nxt:
                return
            url = nxt.replace(config.GITHUB_API + "/", "")

    def get_all(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        out: list[Any] = []
        for page in self._paginate(path, params):
            out.extend(page)
        return out

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = path
        if params:
            url = f"{path}?{urlencode(params)}"
        data, _ = self._get(url)
        return data

    # -- endpoints ---------------------------------------------------------
    def merged_prs(self, repo: str, limit: int) -> list[dict[str, Any]]:
        """Most recent merged PRs, newest first, up to ``limit``."""
        out: list[dict[str, Any]] = []
        for page in self._paginate(
            f"/repos/{repo}/pulls", {"state": "closed", "sort": "updated", "direction": "desc"}
        ):
            for pr in page:
                if pr.get("merged_at"):
                    out.append(pr)
                    if len(out) >= limit:
                        return out
        return out

    def pr_files(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.get_all(f"/repos/{repo}/pulls/{number}/files")

    def pr_commits(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.get_all(f"/repos/{repo}/pulls/{number}/commits")

    def pr_reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.get_all(f"/repos/{repo}/pulls/{number}/reviews")

    def pr_review_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.get_all(f"/repos/{repo}/pulls/{number}/comments")

    def issue(self, repo: str, number: int) -> dict[str, Any] | None:
        return self.get(f"/repos/{repo}/issues/{number}")

    def issue_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.get_all(f"/repos/{repo}/issues/{number}/comments")

    def pull(self, repo: str, number: int) -> dict[str, Any] | None:
        return self.get(f"/repos/{repo}/pulls/{number}")

    def pr_patch(self, repo: str, number: int) -> dict[str, Any] | None:
        """PR with file patches (used by the guardian)."""
        return self.get(f"/repos/{repo}/pulls/{number}")
