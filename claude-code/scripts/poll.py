#!/usr/bin/env python3
"""Poll a GitHub PR for new review comments / reviews / CI failures since the
last invocation.

Stateful: persists last-seen comment + review + check-run IDs to
``~/.claude/state/babysit-pr/<owner-repo>-<pr>.json`` so subsequent runs only
report what's new. Designed for the babysit-pr skill — comment + CI polling;
no auto-fix (that's the model's call after reading the failure).

Usage:
    poll.py [--pr <num|url>] [--repo <owner/repo>] [--json]

Defaults:
    --pr  : auto-detect from current branch via ``gh pr view``
    --repo: auto-detect from current repo via ``gh repo view``
    --json: emit raw JSON instead of human-readable markdown summary
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".claude" / "state" / "babysit-pr"

# NOTE: severity is intentionally NOT classified by the script.
# Reviewers tag comments inconsistently ([Critical], [BLOCKER], "must fix",
# "blocker:", or no tag at all when severity is implied by content). A regex
# heuristic produces false positives (comments DISCUSSING criticality) AND
# false negatives (comments that are factually blocking but unlabelled).
#
# The skill's contract is: this script returns the raw new items; the model
# reads the bodies + surrounding code + PR description and forms its own
# severity judgment. See SKILL.md "How to interpret output" for the
# decision rubric.


def gh(args: list[str]) -> str:
    """Run ``gh`` and return stdout. Raise on non-zero exit."""
    res = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise SystemExit(
            f"gh {' '.join(args)} failed (rc={res.returncode}): {res.stderr.strip()}"
        )
    return res.stdout


def resolve_repo(explicit: str | None) -> str:
    if explicit:
        return explicit
    out = gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    return out.strip()


def resolve_pr(explicit: str | None, repo: str) -> int:
    """Resolve PR number from explicit arg or current branch."""
    if explicit:
        # Accept full URL or number.
        m = re.search(r"/pull/(\d+)", explicit)
        if m:
            return int(m.group(1))
        if explicit.isdigit():
            return int(explicit)
        raise SystemExit(f"--pr {explicit!r}: not a number or PR URL")
    # Auto-detect from current branch.
    try:
        out = gh(["pr", "view", "--repo", repo, "--json", "number", "-q", ".number"])
        return int(out.strip())
    except SystemExit:
        raise SystemExit(
            "Could not auto-detect PR from current branch. Pass --pr explicitly."
        )


def parse_workflow_run_id(html_url: str | None) -> int | None:
    """Extract the workflow run id from a check-run's ``html_url``.

    GitHub Actions check-run URLs look like
    ``https://github.com/<owner>/<repo>/actions/runs/<run-id>/job/<job-id>``.
    The run id is what ``gh run rerun <run-id> --failed`` expects, so we
    surface it in the report — saves the model from re-fetching just to
    extract it. Returns None for non-Actions checks (external apps like
    Codecov, SonarCloud, etc. whose URLs don't follow this pattern).
    """
    if not html_url:
        return None
    m = re.search(r"/actions/runs/(\d+)", html_url)
    return int(m.group(1)) if m else None


def parse_job_id(html_url: str | None) -> int | None:
    """Extract the Actions job id from a check-run's ``html_url``.

    Check-run URLs look like
    ``https://github.com/<owner>/<repo>/actions/runs/<run-id>/job/<job-id>``.
    The job id lets the model pull THAT job's log directly via
    ``repos/<repo>/actions/jobs/<job-id>/logs`` as soon as the job fails —
    no need to wait for the whole workflow run to finish (which is what
    ``gh run view --log-failed`` requires). Returns None for non-Actions
    checks (external apps like Codecov whose URLs don't carry a job id).
    """
    if not html_url:
        return None
    m = re.search(r"/job/(\d+)", html_url)
    return int(m.group(1)) if m else None


def fetch_failed_checks(repo: str, head_sha: str) -> list[dict[str, Any]]:
    """Fetch check-runs for the PR's HEAD commit and return only the failed ones.

    Uses ``check-runs`` (per-attempt records) rather than ``check-suites`` so
    a rerun produces a NEW id — re-reporting a re-failed check is the
    correct behavior (it tells the model "this is the Nth attempt, escalate
    judgment"). Pagination is enabled because some enterprise repos run
    >100 jobs per PR.

    Filters to conclusions that warrant action: ``failure``, ``cancelled``,
    ``timed_out``, ``action_required``, ``startup_failure``. ``success``,
    ``skipped``, ``neutral``, and ``stale`` are not reported; ``null``
    (still running) is also skipped — wait for the next poll.
    """
    raw = gh(["api", "--paginate", f"repos/{repo}/commits/{head_sha}/check-runs"])
    failed: list[dict[str, Any]] = []
    # `--paginate` concatenates JSON objects from each page (each page is
    # ``{"total_count": N, "check_runs": [...]}``). Walk them with
    # ``raw_decode`` so brace counting is correct even with deeply nested
    # check-run output bodies — a regex-based split would mis-fire on
    # inner objects.
    decoder = json.JSONDecoder()
    idx = 0
    pages: list[dict[str, Any]] = []
    while idx < len(raw):
        # Skip whitespace between pages.
        while idx < len(raw) and raw[idx].isspace():
            idx += 1
        if idx >= len(raw):
            break
        try:
            obj, end = decoder.raw_decode(raw, idx)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            pages.append(obj)
        idx = end
    for parsed in pages:
        for c in parsed.get("check_runs", []):
            if c.get("status") != "completed":
                continue
            conclusion = c.get("conclusion")
            if conclusion not in (
                "failure",
                "cancelled",
                "timed_out",
                "action_required",
                "startup_failure",
            ):
                continue
            output = c.get("output") or {}
            failed.append(
                {
                    # Per-attempt id — rerun mints a new one, so dedup by
                    # this is the right behavior for "have I told the
                    # user about THIS specific failure attempt yet?"
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "conclusion": conclusion,
                    "app_slug": (c.get("app") or {}).get("slug"),
                    "started_at": c.get("started_at"),
                    "completed_at": c.get("completed_at"),
                    "html_url": c.get("html_url"),
                    # Workflow run id is what `gh run rerun --failed`
                    # accepts. Surfacing it here means the model can
                    # rerun without an extra round-trip to find the id.
                    "workflow_run_id": parse_workflow_run_id(c.get("html_url")),
                    # Job id + its logs endpoint let the model diagnose THIS
                    # job the moment it fails — `gh api <logs_endpoint>` pulls
                    # the single-job log without waiting for the whole
                    # workflow run to finish (which `gh run view --log-failed`
                    # requires). Both are parsed from the check-run html_url,
                    # so this costs no extra API call. None for external-app
                    # checks (Codecov, etc.) whose URLs carry no job id.
                    "job_id": parse_job_id(c.get("html_url")),
                    "logs_endpoint": (
                        f"repos/{repo}/actions/jobs/{job_id}/logs"
                        if (job_id := parse_job_id(c.get("html_url"))) is not None
                        else None
                    ),
                    "run_attempt": c.get("run_attempt"),
                    # output.title is usually a short label like "Tests failed";
                    # output.summary is the markdown body GitHub renders on the
                    # check page — can be megabytes for chatty test reporters.
                    # Truncate the summary at the script boundary so the
                    # markdown report stays scannable; full body is one
                    # `gh run view --log-failed <run-id>` away.
                    "output_title": output.get("title"),
                    "output_summary": (output.get("summary") or "")[:500],
                }
            )
    return failed


def fetch_pr_state(repo: str, pr: int) -> dict[str, Any]:
    """Fetch PR meta + reviews + line comments + top-level issue comments + CI.

    GitHub exposes three comment surfaces for a PR:
      1. Reviews — overall review submissions with a state (APPROVED /
         CHANGES_REQUESTED / COMMENTED). From `gh pr view --json reviews`.
      2. Line-level review comments — inline comments anchored to a file:line
         within a review. From `gh api repos/{repo}/pulls/{pr}/comments`.
      3. Top-level issue comments — free-form conversation comments on the PR
         itself (no file/line anchor). PRs are issues for this purpose, so
         the endpoint is `gh api repos/{repo}/issues/{pr}/comments`.

    Plus a fourth surface for CI:
      4. Check-runs for the HEAD SHA — per-attempt CI job records. Only
         FAILED ones are returned (success is noise; still-running gets
         picked up next poll).

    All four are diffed independently and rendered in their own sections.
    """
    pr_view = json.loads(
        gh(
            [
                "pr",
                "view",
                str(pr),
                "--repo",
                repo,
                "--json",
                "state,title,mergeable,isDraft,reviews,headRefOid",
            ]
        )
    )
    line_comments = json.loads(
        gh(["api", "--paginate", f"repos/{repo}/pulls/{pr}/comments"])
    )
    issue_comments = json.loads(
        gh(["api", "--paginate", f"repos/{repo}/issues/{pr}/comments"])
    )
    head_sha = pr_view.get("headRefOid") or ""
    failed_checks = fetch_failed_checks(repo, head_sha) if head_sha else []
    return {
        "meta": {
            "state": pr_view.get("state"),
            "title": pr_view.get("title"),
            "mergeable": pr_view.get("mergeable"),
            "isDraft": pr_view.get("isDraft"),
            "head_sha": head_sha,
        },
        "failed_checks": failed_checks,
        "reviews": [
            {
                "id": r.get("id"),
                "author": (r.get("author") or {}).get("login"),
                "state": r.get("state"),
                "submittedAt": r.get("submittedAt"),
                "body": (r.get("body") or "").strip(),
            }
            for r in pr_view.get("reviews", [])
        ],
        "issue_comments": [
            {
                "id": c.get("id"),
                "author": (c.get("user") or {}).get("login"),
                # author_association tells the model whether the comment
                # comes from a maintainer (OWNER/MEMBER/COLLABORATOR), an
                # external contributor, a first-time submitter, or a bot.
                # Heavily informs how to weight a "consider X instead" comment.
                "author_association": c.get("author_association"),
                "body": (c.get("body") or ""),
                "created_at": c.get("created_at"),
                "html_url": c.get("html_url"),
            }
            for c in issue_comments
        ],
        "comments": [
            {
                "id": c.get("id"),
                "author": (c.get("user") or {}).get("login"),
                "author_association": c.get("author_association"),
                "file": c.get("path"),
                "line": c.get("line") or c.get("original_line"),
                # diff_hunk gives the model the surrounding code context
                # without needing a separate file read. Critical for judging
                # whether a "this looks wrong" comment is actually pointing
                # at a real bug vs misreading the diff.
                "diff_hunk": c.get("diff_hunk"),
                "body": (c.get("body") or ""),
                "created_at": c.get("created_at"),
                "html_url": c.get("html_url"),
            }
            for c in line_comments
        ],
    }


def state_path(repo: str, pr: int) -> Path:
    safe = repo.replace("/", "-")
    return STATE_DIR / f"{safe}-{pr}.json"


def load_state(path: Path) -> dict[str, Any]:
    """Load prior state, defaulting missing keys for forward-compatibility.

    Keys that were added after the initial release (``issue_comments_seen``,
    ``checks_seen``, ``poll_count``) default to empty list / 0 so the first
    poll after upgrade surfaces ALL existing items as "new" — which is the
    right baseline, since the model hasn't seen them yet either.
    """
    default = {
        "comments_seen": [],
        "reviews_seen": [],
        "issue_comments_seen": [],
        "checks_seen": [],
        "poll_count": 0,
    }
    if not path.exists():
        return default
    try:
        loaded = json.loads(path.read_text())
        for k, v in default.items():
            loaded.setdefault(k, v)
        return loaded
    except (json.JSONDecodeError, OSError):
        return default


def save_state(path: Path, current: dict[str, Any], prior: dict[str, Any], had_new_items: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    poll_count = prior.get("poll_count", 0) + (1 if had_new_items else 0)
    payload = {
        "comments_seen": [c["id"] for c in current["comments"]],
        "reviews_seen": [r["id"] for r in current["reviews"]],
        "issue_comments_seen": [c["id"] for c in current["issue_comments"]],
        "checks_seen": [c["id"] for c in current["failed_checks"]],
        "poll_count": poll_count,
        "updated_at": int(time.time()),
        "meta": current["meta"],
    }
    path.write_text(json.dumps(payload, indent=2))


def diff(prior: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    seen_c = set(prior.get("comments_seen", []))
    seen_r = set(prior.get("reviews_seen", []))
    seen_ic = set(prior.get("issue_comments_seen", []))
    seen_ck = set(prior.get("checks_seen", []))
    return {
        "new_comments": [c for c in current["comments"] if c["id"] not in seen_c],
        "new_reviews": [r for r in current["reviews"] if r["id"] not in seen_r],
        "new_issue_comments": [
            c for c in current["issue_comments"] if c["id"] not in seen_ic
        ],
        "new_failed_checks": [
            c for c in current["failed_checks"] if c["id"] not in seen_ck
        ],
    }


def deterministic_signals(
    reviews: list[dict[str, Any]],
    meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return the deterministic signals worth flagging without judgment.

    GitHub gives us a few unambiguous binding states:
      - Review with `state == CHANGES_REQUESTED` — reviewer explicitly blocked
      - `meta.mergeable == CONFLICTING` — branch can't merge until conflicts resolved

    Everything else (severity tags, perceived dispute, decision impact)
    requires reading the comment + code, which is the model's job.
    """
    out: list[dict[str, Any]] = []
    for r in reviews:
        if r.get("state") == "CHANGES_REQUESTED":
            out.append(
                {
                    "kind": "changes_requested",
                    "author": r.get("author"),
                    "submittedAt": r.get("submittedAt"),
                    "body_preview": (r.get("body") or "")[:200],
                }
            )
    if meta and meta.get("mergeable") == "CONFLICTING":
        out.append(
            {
                "kind": "merge_conflict",
                "summary": (
                    "PR has merge conflicts with its base branch. Must resolve "
                    "before merge."
                ),
            }
        )
    return out


def render_markdown(repo: str, pr: int, current: dict[str, Any], delta: dict[str, Any], poll_count: int = 0) -> str:
    """Render new items WITHOUT model-level judgment.

    The script does NOT classify severity. It only reports:
      - what's new
      - the deterministic CHANGES_REQUESTED signal from review state
      - merge conflicts (mergeable == CONFLICTING)
    Severity / dispute / decision-significance is the model's call after
    reading the comment body + surrounding code.
    """
    out: list[str] = []
    meta = current["meta"]
    new_c = delta["new_comments"]
    new_r = delta["new_reviews"]
    new_ic = delta["new_issue_comments"]
    new_ck = delta["new_failed_checks"]
    # Pass meta so merge-conflict signals surface even when there are no
    # new comments — a long-pending PR with conflict-on-base still needs
    # action every poll.
    signals = deterministic_signals(new_r, meta)

    out.append(f"### PR {repo}#{pr}: {meta.get('title', '?')}")
    mode_tag = "CRITICAL-ONLY" if poll_count > 5 else "FULL"
    out.append(
        f"**State**: {meta.get('state')} | **Mergeable**: {meta.get('mergeable')} | "
        f"**Draft**: {meta.get('isDraft')} | **Poll**: #{poll_count} ({mode_tag})"
    )

    # Surface deterministic blocking signals BEFORE the no-new-items short
    # circuit, so a PR with conflict-but-no-new-comments still gets flagged.
    if signals:
        out.append(
            f"\n#### 🚫 {len(signals)} deterministic blocking signal(s)"
        )
        for s in signals:
            if s["kind"] == "changes_requested":
                out.append(
                    f"- **{s['author']}** submitted `CHANGES_REQUESTED` "
                    f"({s['submittedAt']}): {s['body_preview']}"
                )
            elif s["kind"] == "merge_conflict":
                out.append(f"- 🔀 **Merge conflict** — {s['summary']}")
                out.append(
                    "  - Resolve via: `git fetch origin && git rebase "
                    "origin/<base>` (or `git merge origin/<base>` if rebase "
                    "is undesirable), resolve conflicts, push."
                )

    if not new_c and not new_r and not new_ic and not new_ck:
        out.append("\n_No new comments, reviews, or CI failures since last poll._")
        return "\n".join(out)

    if new_r:
        out.append(f"\n#### {len(new_r)} new review(s)")
        for r in new_r:
            body_first_line = (r.get("body") or "").splitlines()[0:1]
            body_str = body_first_line[0] if body_first_line else "(empty)"
            out.append(
                f"- **{r['author']}** — `{r['state']}` ({r['submittedAt']}): {body_str}"
            )

    if new_ic:
        out.append(f"\n#### {len(new_ic)} new top-level comment(s)")
        for c in new_ic:
            assoc = c.get("author_association") or ""
            assoc_tag = f" *({assoc})*" if assoc and assoc != "NONE" else ""
            first_line = (c.get("body") or "").splitlines()[0] if c.get("body") else ""
            short = first_line[:140] + ("…" if len(first_line) > 140 else "")
            out.append(
                f"- **{c['author']}**{assoc_tag} ({c['created_at']}): {short}"
            )

    if new_c:
        out.append(f"\n#### {len(new_c)} new line-level comment(s)")
        for c in new_c:
            assoc = c.get("author_association") or ""
            assoc_tag = f" *({assoc})*" if assoc and assoc != "NONE" else ""
            first_line = (c.get("body") or "").splitlines()[0] if c.get("body") else ""
            short = first_line[:120] + ("…" if len(first_line) > 120 else "")
            out.append(
                f"- **{c['author']}**{assoc_tag} at `{c['file']}:{c['line']}`: {short}"
            )

    if new_ck:
        # Group by workflow_run_id so the model sees at a glance which
        # workflows have failures — same workflow with multiple failed
        # jobs gets one `gh run rerun <id> --failed` (if all flake) or
        # one fix decision (if any are PR-related). The model does the
        # classification per SKILL.md rubric.
        out.append(f"\n#### 🤖 {len(new_ck)} new failed CI check(s)")
        for c in new_ck:
            run_id = c.get("workflow_run_id")
            attempt = c.get("run_attempt")
            attempt_tag = (
                f" (attempt #{attempt})" if attempt and attempt > 1 else ""
            )
            title = c.get("output_title") or c.get("name")
            summary = (c.get("output_summary") or "").strip()
            summary_first_line = summary.splitlines()[0] if summary else ""
            short = summary_first_line[:180] + (
                "…" if len(summary_first_line) > 180 else ""
            )
            run_id_tag = f" (workflow run `{run_id}`)" if run_id else ""
            out.append(
                f"- **{c['name']}** — `{c['conclusion']}`{attempt_tag} "
                f"[{c.get('app_slug') or '?'}]{run_id_tag}"
            )
            if title and title != c["name"]:
                out.append(f"  - {title}")
            if short:
                out.append(f"  - {short}")
            if c.get("html_url"):
                out.append(f"  - {c['html_url']}")
            # Surface the direct single-job log command. This works as soon
            # as the job fails — no need to wait for the whole workflow run
            # to finish (the constraint on `gh run view --log-failed`).
            logs_endpoint = c.get("logs_endpoint")
            if logs_endpoint:
                out.append(f"  - logs: `gh api {logs_endpoint}`")

    out.append(
        "\n_Severity / dispute / decision-impact judgments are NOT made by "
        "this script. Read the full comment bodies + surrounding code (use "
        "`--json` for full bodies + diff_hunk) and form your own assessment. "
        "For CI failures, classify each as PR-code-related (fix) vs "
        "infra/flake (rerun via `gh run rerun <id> --failed`) per the "
        "SKILL.md rubric._"
    )

    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pr", help="PR number or URL; auto-detected if omitted")
    p.add_argument("--repo", help="owner/repo; auto-detected if omitted")
    p.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    p.add_argument(
        "--reset-state",
        action="store_true",
        help="discard prior state for this PR before polling (full re-baseline)",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="report ALL items (not just new since last poll); does not modify state file",
    )
    p.add_argument(
        "--exclude-author",
        nargs="*",
        default=[],
        help="filter out comments/reviews from these GitHub logins",
    )
    args = p.parse_args()

    repo = resolve_repo(args.repo)
    pr = resolve_pr(args.pr, repo)
    sp = state_path(repo, pr)

    if args.reset_state and sp.exists():
        sp.unlink()

    # --full: use empty prior so everything appears "new"; don't save state
    prior = {} if args.full else load_state(sp)
    current = fetch_pr_state(repo, pr)
    delta = diff(prior, current)
    if not args.full:
        had_new_items = bool(
            delta["new_comments"] or delta["new_reviews"]
            or delta["new_issue_comments"] or delta["new_failed_checks"]
        )
        save_state(sp, current, prior, had_new_items)

    if args.exclude_author:
        excluded = set(args.exclude_author)
        delta["new_reviews"] = [r for r in delta["new_reviews"] if r.get("author") not in excluded]
        delta["new_comments"] = [c for c in delta["new_comments"] if c.get("author") not in excluded]
        delta["new_issue_comments"] = [c for c in delta["new_issue_comments"] if c.get("author") not in excluded]

    if args.full:
        poll_count = 0
    else:
        poll_count = prior.get("poll_count", 0) + (1 if had_new_items else 0)

    if args.json:
        print(
            json.dumps(
                {
                    "repo": repo,
                    "pr": pr,
                    "meta": current["meta"],
                    "poll_count": poll_count,
                    "new_reviews": delta["new_reviews"],
                    "new_issue_comments": delta["new_issue_comments"],
                    "new_line_comments": delta["new_comments"],
                    "new_failed_checks": delta["new_failed_checks"],
                    # Only deterministic signals — review state ==
                    # CHANGES_REQUESTED. Severity/dispute classification
                    # of comment bodies is the model's job, not the script's.
                    "deterministic_signals": deterministic_signals(
                        delta["new_reviews"], current["meta"]
                    ),
                    "state_file": str(sp),
                },
                indent=2,
            )
        )
    else:
        print(render_markdown(repo, pr, current, delta, poll_count))

    return 0


if __name__ == "__main__":
    sys.exit(main())
