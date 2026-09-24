"""Minimal GitHub REST client: just the PR and CI endpoints the agent needs."""

from __future__ import annotations

import os

import httpx

TOKEN = os.environ.get("GITHUB_TOKEN", "")

_client = httpx.AsyncClient(
    base_url="https://api.github.com",
    headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "repo-agent-bot",
        **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}),
    },
    timeout=30,
    follow_redirects=True,  # httpx drops Authorization on cross-origin redirects (job logs -> signed URL)
)


class GitHubError(Exception):
    pass


async def _req(method: str, route: str, **kw) -> httpx.Response:
    r = await _client.request(method, route, **kw)
    if r.is_error:
        raise GitHubError(f"GitHub {method} {route}: {r.status_code} {r.text[:300]}")
    return r


async def get_pr(repo: str, n: int) -> dict:
    return (await _req("GET", f"/repos/{repo}/pulls/{n}")).json()


async def get_pr_files(repo: str, n: int) -> list[dict]:
    return (await _req("GET", f"/repos/{repo}/pulls/{n}/files", params={"per_page": 100})).json()


async def create_pr(repo: str, title: str, body: str, head: str, base: str, draft: bool) -> dict:
    payload = {"title": title, "body": body, "head": head, "base": base, "draft": draft}
    return (await _req("POST", f"/repos/{repo}/pulls", json=payload)).json()


async def post_review(repo: str, n: int, body: str) -> dict:
    # COMMENT only: the bot never approves or blocks merges on its own.
    return (await _req("POST", f"/repos/{repo}/pulls/{n}/reviews", json={"body": body, "event": "COMMENT"})).json()


async def get_check_runs(repo: str, sha: str) -> list[dict]:
    r = await _req("GET", f"/repos/{repo}/commits/{sha}/check-runs", params={"per_page": 50})
    return r.json()["check_runs"]


async def get_job_log(repo: str, job_id: int) -> str:
    # For GitHub Actions, a check-run id is the job id.
    return (await _req("GET", f"/repos/{repo}/actions/jobs/{job_id}/logs")).text
