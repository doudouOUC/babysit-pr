---
name: babysit-pr
description: Poll a GitHub PR for new comments (line-level + top-level conversation), reviews, AND failing CI checks since the last invocation, with persistent state across sessions. Use when the user asks to watch / poll / check / babysit a PR, monitor review feedback, surface new comments since the last check, watch CI for failures, or set up periodic polling of a PR. Auto-detects PR number from current branch and repo from `gh repo view`. Stateful — only reports what is new since the last invocation. Combine with the `/loop` skill for continuous polling. After running, the model reads new comment bodies + surrounding code AND failed-check job logs, then forms its own judgment: which comments are blocking / nits, and which CI failures are PR-code-related (fix) vs infra/flake (auto-rerun via `gh run rerun --failed`). The script does NOT auto-classify severity beyond the deterministic CHANGES_REQUESTED / CONFLICTING signals.
---

# babysit-pr

Comment / review / CI polling for a GitHub PR with stateful new-since-last-check semantics.

## When to invoke

Trigger this skill when the user asks any of:
- "watch PR #N", "babysit PR #N", "monitor PR #N"
- "any new review comments on #N?"
- "what's new on the PR since last time?"
- "check PR #N for blockers"
- "any CI failures on the PR?"
- "watch the build on this PR"
- "poll my PR"

Or when the user wants periodic checking — combine with `/loop`:
```
/loop 20m /babysit-pr 4432
```

## What it does

Single invocation:

1. Resolves PR (explicit `--pr <num|url>` OR auto-detect from current branch via `gh pr view`)
2. Resolves repo (explicit `--repo <owner/repo>` OR auto-detect via `gh repo view`)
3. Fetches FOUR PR surfaces:
   - **Reviews** — overall review submissions with state (APPROVED / CHANGES_REQUESTED / COMMENTED), via `gh pr view --json reviews`
   - **Line-level review comments** — inline comments anchored to file:line within a review, via `gh api repos/<repo>/pulls/<pr>/comments`
   - **Top-level conversation comments** — free-form comments on the PR itself with no file/line anchor, via `gh api repos/<repo>/issues/<pr>/comments` (PRs are issues for this purpose)
   - **Failing CI check-runs for the HEAD SHA** — per-attempt records from `gh api repos/<repo>/commits/<sha>/check-runs`, filtered to conclusions worth acting on (`failure`, `cancelled`, `timed_out`, `action_required`, `startup_failure`). Each carries `workflow_run_id` (parsed from the URL) so the model can `gh run rerun <id> --failed` without a round-trip to find the id.
4. Loads prior IDs from `~/.claude/state/babysit-pr/<owner-repo>-<pr>.json` (separate seen-set per surface)
5. Computes set diff per surface → only **new** items since last invocation. Check-runs are diffed by per-attempt id, so a rerun that still fails reports as a NEW failure — the model can see `run_attempt: 2+` and escalate judgment.
6. Surfaces TWO deterministic signals (both binding, both flagged regardless of whether there are new comments):
   - Any review with state == `CHANGES_REQUESTED` (reviewer explicitly blocked merge).
   - PR `mergeable` state == `CONFLICTING` (branch can't merge until rebased / merged with base).

   The script does NOT pattern-match `[Critical]` / `[Suggestion]` / "must fix" tags in comment bodies, nor classify CI failures as flake vs real. Reviewers tag inconsistently (false positives + false negatives), and CI failures need the model to read the job log + cross-check against `gh pr diff --name-only`. Both judgments live in "How to interpret output" below.
7. Saves new state file (atomic overwrite of seen IDs across all four surfaces)
8. Renders a Markdown report (default) or JSON (`--json`)

## Invocation

The script lives at `scripts/poll.py`. Always run it with `python3` and pass any flags the user supplied. Do NOT call `gh` directly — the script already encapsulates all `gh` calls and handles state.

```bash
python3 ~/.claude/skills/babysit-pr/scripts/poll.py [--pr <num|url>] [--repo <owner/repo>] [--json] [--reset-state] [--full] [--exclude-author <login> ...]
```

Common patterns:

| User intent | Command |
| --- | --- |
| "Watch PR #4432 in this repo" | `python3 ~/.claude/skills/babysit-pr/scripts/poll.py --pr 4432` |
| "Check the current branch's PR" | `python3 ~/.claude/skills/babysit-pr/scripts/poll.py` (auto-detects) |
| "Watch PR #4432 in QwenLM/qwen-code" | `python3 ~/.claude/skills/babysit-pr/scripts/poll.py --pr 4432 --repo QwenLM/qwen-code` |
| "Re-baseline (forget prior seen)" | `python3 ~/.claude/skills/babysit-pr/scripts/poll.py --pr 4432 --reset-state` |
| "Full report (read-only, no state mutation)" | `python3 ~/.claude/skills/babysit-pr/scripts/poll.py --pr 4432 --full` |
| "Give me JSON for further processing" | `python3 ~/.claude/skills/babysit-pr/scripts/poll.py --pr 4432 --json` |

**Auto-exclude own comments:** Before every poll, resolve the PR author's login and pass `--exclude-author` to filter out our own replies. This prevents self-replies from flooding the poll output:
```bash
PR_AUTHOR=$(gh pr view <pr> --repo <repo> --json author --jq .author.login)
python3 ~/.claude/skills/babysit-pr/scripts/poll.py --pr <pr> --exclude-author "$PR_AUTHOR"
```
Do this automatically on every invocation — do not ask the user.

## How to interpret output

The Markdown output has up to six sections:

1. **Header**: PR meta (state, mergeable, draft).
2. **🚫 Deterministic blocking signal(s)** (only if present): two kinds the script asserts as blocking without judgment:
   - Each `CHANGES_REQUESTED` review (reviewer explicitly used GitHub's mechanism to gate the PR).
   - 🔀 **Merge conflict** when `meta.mergeable == CONFLICTING`. Surfaced even when there are no new comments — a stale PR can develop a conflict at any time as base advances. The script suggests `git fetch origin && git rebase origin/<base>` or merge.
3. **New review(s)**: each new review with author, state, submittedAt, first body line.
4. **New top-level comment(s)**: each new conversation comment (no file anchor) with author, `author_association` tag (e.g. `OWNER`, `MEMBER`, `COLLABORATOR`, `CONTRIBUTOR`), timestamp, and first 140 chars.
5. **New line-level comment(s)**: each new inline comment with author, `author_association`, file:line, and first 120 chars.
6. **🤖 New failed CI check(s)**: each new failed check-run with name, conclusion (`failure` / `cancelled` / `timed_out` / `action_required` / `startup_failure`), app slug (e.g. `github-actions`, `codecov`), workflow run id (for `gh run rerun`), attempt number (`#2+` means a rerun also failed), short output title + first line of `output.summary`, the `html_url` to the job page, and a `logs: gh api <logs_endpoint>` line for direct single-job log access (Actions checks only; absent for external-app checks with no parseable `job_id`). The script intentionally does NOT classify these as flake vs real — that requires reading the job log + the PR diff, which is the model's job.

The output ends with a one-line reminder that severity / dispute / decision-impact judgments are the model's responsibility, not the script's.

### Model judgment rubric (REQUIRED after running the script)

After the script lists what is new, **read the full body of each new item** and decide which buckets it falls into. Use `--json` to get full bodies + `diff_hunk` (the surrounding code) without re-fetching.

For each new comment / review, classify into ONE OR MORE of:

| Category | Definition | When to surface to user |
| --- | --- | --- |
| **Merge conflict** | `meta.mergeable == CONFLICTING`. Deterministic; no judgment needed. Branch can't merge until conflicts resolved against base. | **Auto-resolve immediately**: fetch the base branch, rebase the PR's commits onto it (using `--onto` to target only the PR's own commits if the branch has orphaned history), resolve any file-level conflicts, and force-push. Report what was resolved. If a conflict requires semantic judgment (both sides changed the same logic), surface to the user instead. |
| **Blocker** | The PR cannot land as-is without addressing this. CHANGES_REQUESTED review, security/correctness bug pointed at the diff, or maintainer (`OWNER`/`MEMBER`/`COLLABORATOR`) explicitly saying "this must change before merge". | Always surface, prominently. |
| **Dispute** | Reviewer disagrees with an approach or design decision in the PR. Often phrased as "I'd push back on X", "this won't work because Y", "consider Z instead", "have you thought about W?", or a question that implicitly challenges a choice. May not have any severity tag. | Always surface with author + position summary. |
| **Decision-level suggestion** | Suggestion that requires the PR author to make a deliberate accept/reject call (vs. a nit the author can take or leave). Often architectural, naming choices that affect public API, scope expansion / contraction, or tradeoff articulation. | Surface; flag that user input is needed. |
| **Critical bug / security issue** | Reviewer points at code that is genuinely broken — wrong logic, race condition, security vulnerability, regression. Not a style preference. | Surface; recommend immediate fix. |
| **Nit / style / preference** | "Could rename this", "minor formatting", "personal preference". Author may take or leave. | Mention in passing or batch into "non-blocking suggestions" summary. |
| **CI failure — PR-code-related** | A failed check whose root cause is in the diff: lint/format/typecheck failure, test failure in a touched file, build failure with a stack frame pointing at touched code, snapshot mismatch on a touched component, etc. | Surface; propose fix; **do NOT auto-rerun** — the rerun will fail again and burn a CI cycle. |
| **CI failure — infra / flake** | A failed check whose root cause is not in the diff: setup-phase failure (npm install, docker pull, action checkout), network timeout, OOM, runner image issue, `cancelled` from external pre-emption, test in an untouched file that's known-flaky, `startup_failure` / `timed_out` conclusions. | **Auto-rerun via `gh run rerun <workflow_run_id> --failed`** — do not ask the user; same blast-radius logic as auto-reply-to-bot. Tell the user what you reran and why. |

### How to make the judgment

For each comment, weigh:

1. **`author_association`**: `OWNER` / `MEMBER` / `COLLABORATOR` carry weight; `CONTRIBUTOR` / `NONE` / bots are advisory unless the comment content is rigorous. A bot suggesting "[Critical]" is not automatically a blocker — read the underlying claim.
2. **The actual code (`diff_hunk`)**: a "this looks wrong" comment about untouched code is less blocking than the same comment on changed code. A comment about correctness is more blocking than a comment about style on the same line.
3. **PR description / objective**: comments asking for scope outside the PR's stated objective are usually deferrable; comments about the PR's stated objective being mis-implemented are blocking.
4. **Comment phrasing for disputes / decisions**: look for "I disagree", "this won't work", "wrong approach", "consider X instead", "open question:", "WDYT?", "thoughts?", "have you considered", or any question that proposes a contrary direction. These are signals of dispute / decision-level suggestion EVEN WITHOUT a severity tag.
5. **Repetition**: if multiple reviewers raise the same concern, weight it higher.
6. **Severity tags in body**: `[Critical]`, `[BLOCKER]`, `[Suggestion]`, `[nit]` are HINTS from the reviewer about how seriously they meant the comment. Treat as evidence, not as authoritative classification — verify against the underlying content.

### CI failure triage (REQUIRED when the report has failed checks)

For each new failed check the script surfaces, decide PR-code-related vs infra/flake. The cost asymmetry is the same as auto-reply to bots: a wrong "rerun" costs one CI cycle (the failure will resurface as a NEW attempt next poll, with `run_attempt: 2+`, telling you to stop reruning and look closer); a wrong "fix" costs nothing if you're right but wastes thinking time if the failure was flake all along.

**Step 1 — gather context for each failed check** (don't skip this):

```bash
# What files did this PR touch?
gh pr diff <pr> --name-only

# Fastest path — pull THIS job's log the moment it fails.
# The report gives you `logs_endpoint` per failed check; it resolves to a
# single job's log and works even while the rest of the workflow run is
# still going (unlike `gh run view --log-failed`, which is run-scoped and
# may stay empty until the whole run finishes).
gh api repos/<owner>/<repo>/actions/jobs/<job_id>/logs   # == the report's logs_endpoint

# Fallback — whole-run failed logs once the run has finished:
gh run view <workflow_run_id> --log-failed
```

Both `workflow_run_id` and `logs_endpoint` (with its `job_id`) are already in
the script's report — no need to re-fetch. Prefer `logs_endpoint` for fast,
per-job diagnosis; fall back to `gh run view --log-failed` only when a check
has no `job_id` (external-app checks like Codecov) or you want the aggregated
run view.

**Step 2 — classify each failure**. Signals for PR-code-related:
- Test file in the failure stack appears in `gh pr diff --name-only`.
- Lint / format / typecheck job failed; these run on the diff, so failure ≈ diff.
- Build failure with a stack pointing at touched code.
- Same test failed in BOTH attempt #1 and attempt #2 (not a flake — escalate).
- The PR's stated objective explicitly touches the failing area (e.g. PR adds a new route and the integration tests for `/that-route` fail).

Signals for infra/flake:
- Failure during a setup step (`actions/checkout`, `actions/setup-node`, `npm install`, `docker pull`, registry timeouts).
- Network / DNS / TLS errors in the log.
- `cancelled` conclusion (someone or some external timeout pre-empted the runner).
- `startup_failure` / `timed_out` conclusions (rarely PR-correlated).
- Test in a file the PR didn't touch AND no shared dep with touched files; especially if the same test passed on attempt #1 of an earlier commit.
- OOM / disk-full / "no space left on device" / "runner went offline".
- Codecov / SonarCloud / external app checks failing with infra errors (their own service availability, not the diff).

Mixed signals → treat as PR-code-related and propose fix. Cheaper to be cautious here than to ignore a real regression.

**Step 3 — act**, grouped by `workflow_run_id` (one workflow can have multiple failed jobs):
- **Workflow where ALL failures are flake/infra**: run `gh run rerun <workflow_run_id> --failed` immediately, do not ask. Tell the user in the report what you reran and the reasoning per failed job.
- **Workflow with ANY PR-code-related failure**: do NOT rerun (you'd burn a CI cycle and confuse the diff). Surface as a fix proposal to the user; wait for direction.
- **Same job failed on attempt ≥ 2** (i.e. you reran it last poll and it failed again): stop calling it flake. Read the log more carefully and escalate to "real failure, needs investigation".

**Step 4 — surface the outcome** to the user in the same report as comment classifications. Be explicit about which workflows you rerun and which you held back. Example:

> 🤖 **CI**: 3 failed checks this poll.
> - Reran `ci.yml` (run 12345) — both failures were `actions/checkout` network timeouts (flake).
> - **Held back rerun on `lint.yml` (run 12346)** — `eslint` failure points at `packages/cli/src/serve/server.ts:1503` (in the PR diff). Looks like a missing semicolon I introduced; propose fix.

When there are no new items, the script prints `_No new comments, reviews, or CI failures since last poll._` and exits 0.

## Reporting back to the user

After running the script:

- **If output is "no new comments"**, relay that one-liner verbatim and stop. Do not embellish.

- **If there ARE new items**, do this in order:
  1. Echo the script's "what's new" sections (header + 5 sections) — they're already concise.
  2. Run the script again with `--json` (no extra API cost; same diff with full bodies + diff_hunk) and read every new comment in full.
  3. Apply the **Model judgment rubric** above. For each new item, decide if it's a Blocker / Dispute / Decision-level / Critical bug / Nit.
  4. Write a "What needs your attention" section, in this priority order:
     - **🔀 Merge conflict** (if `meta.mergeable == CONFLICTING`): **auto-resolve immediately**. Fetch the base branch, identify the PR's own commits (`git log origin/<base>..HEAD`), rebase them onto the updated base (`git rebase --onto origin/<base> <first-pr-commit>^ HEAD`), resolve file-level conflicts (keep both sides for independent additions, prefer the base for formatting-only changes), and force-push with `--force-with-lease`. Report what files conflicted and how they were resolved. If a conflict requires semantic judgment (both sides changed the same logic in incompatible ways), surface to the user instead of guessing.
     - **CI — PR-code-related failures**: held-back reruns + fix proposals, per the CI triage section above. Cite job name + file:line + 1-line root-cause summary.
     - **CI — auto-reruns triggered**: which workflow runs you reran (`gh run rerun <id> --failed`) and one-line reasoning per failure. Mention so the user can stop you if they disagree before the rerun completes.
     - **Blockers**: every CHANGES_REQUESTED review + every comment your judgment flagged as blocking. Cite author + file:line + 1-2-line summary of the concern.
     - **Disputes / decision-level**: reviewer is pushing back or asking the user to make a call. Cite author + position + your read of what direction they're proposing.
     - **Critical bugs / security**: code-level issues that need immediate fix.
     - **Non-blocking suggestions**: 1-line bullet per item; user can scan and ignore.
  5. End with a recommended next action. Examples:
     - "Merge conflict on retry.ts — rebase against origin/main first, then address wenshao's CHANGES_REQUESTED on test coverage."
     - "wenshao requested changes on retry.ts:306 (no cap on persistent retries) — this is a blocker. Address by adding cap, then push."
     - "Reran ci.yml (flaky setup), held back lint.yml (real eslint error in server.ts:1503) — fix proposal below."
     - "No blockers, 2 nits, all CI green — safe to ignore unless you want them in this PR."

### Autonomous comment handling

After classifying all new human-reviewer comments, **act on them directly** instead of waiting for user direction. The model must form its own judgment on each comment — reviewer tags like `[Critical]` are hints, not commands.

**Decision process per comment:**

1. **Read the code the comment points at.** Understand what the reviewer is claiming before deciding.
2. **Verify the claim independently.** Check whether the bug/issue actually exists by reading the surrounding code, tracing call paths, or reasoning about edge cases. Don't reflexively accept — a `[Critical]` tag from a reviewer doesn't make a correct claim.
3. **Decide: agree, partially agree, or push back.**

| Judgment | Action |
| --- | --- |
| **Agree — real bug or clear improvement** | Fix the code directly, no user approval needed. Applies regardless of tag (Critical, Suggestion, nit). |
| **Partially agree — valid concern but wrong fix** | Fix the underlying issue in a better way. Reply explaining what you did differently and why. |
| **Disagree — factual misread or not worth the churn** | Push back: reply on the PR thread explaining why. Don't fix. Don't ask the user — form your own position. |
| **Uncertain — needs domain context you don't have** | Surface to the user with your analysis of both sides. This is the only case where you wait. |

**What counts as "agree":**
- The reviewer points at code that is genuinely broken (wrong logic, race, missing error handling) and you can reproduce the reasoning.
- The reviewer's suggested fix is correct or close enough to adapt.
- The improvement is small, safe, and clearly better than the current code.

**What counts as "push back":**
- The reviewer's claim is based on a misread of the code (e.g., they missed a guard clause upstream).
- The suggestion would make the code worse (unnecessary complexity, scope expansion beyond the PR's objective).
- The concern is valid in theory but doesn't apply here (e.g., "this could overflow" when the input is bounded).

**Before replying, check for existing replies (REQUIRED dedup guard):**

For each thread you intend to reply to, query whether the PR author (or the current bot user) has already posted a reply in that thread. Use the GraphQL reviewThreads query with `comments(first:10)` and check if any comment's `author.login` matches the PR author. **If a reply already exists, skip that thread entirely** — do not post a duplicate, even if your judgment differs from the prior reply. This prevents the #1 failure mode of cross-session duplicate replies.

```bash
# Check if thread already has a reply from us:
gh api graphql -f query='{ repository(owner:"<owner>",name:"<repo>") { pullRequest(number:<pr>) { reviewThreads(first:100) { nodes { id isResolved comments(first:10) { nodes { author { login } } } } } } } }' \
  --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved==false) | select([.comments.nodes[].author.login] | any(. == "<pr_author>") | not) | .id'
```

Only reply to threads returned by this query (unresolved AND no prior reply from us).

**After deciding, reply to EVERY unreplied comment thread individually:**
- **Agree / fixed**: reply briefly acknowledging the fix (e.g., "Agreed, fixed in <commit>.").
- **Partially agree**: reply explaining what you did differently and why.
- **Push back**: reply with your reasoning — factual, terse, no hedging.
- **Defer**: reply explaining why it's deferred (scope, follow-up, needs design discussion) so the reviewer knows it wasn't ignored.

**After all comments are processed, execute these steps IN ORDER — do not stop after pushing code:**

1. `git add && git commit && git push` — push the fix commit.
2. **Reply to EACH addressed thread on GitHub** — use `gh api repos/<owner>/<repo>/pulls/<pr>/comments -f body="..." -F in_reply_to=<comment_id>` for inline threads. Terse: "Fixed in <sha>." / "Not taking — <reason>." / "Deferring — <reason>."
3. **Post a top-level PR summary comment** — table of all actions (fixed / pushed back / deferred) with commit SHA.
4. **Resolve ALL replied threads** — query all unresolved threads via GraphQL, then batch-resolve every thread that has been replied to (fixed, pushed back, or deferred). A reply IS the resolution — leaving threads open after replying creates noise for the reviewer.
   ```bash
   # List unresolved threads:
   gh api graphql -f query='{ repository(owner:"<owner>",name:"<repo>") { pullRequest(number:<pr>) { reviewThreads(first:100) { nodes { id isResolved comments(first:1) { nodes { body path line } } } } } } }' --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved==false)'
   # Resolve each replied thread:
   gh api graphql -f query='mutation { resolveReviewThread(input:{threadId:"<id>"}) { thread { isResolved } } }'
   ```
5. **Report the thread count** to the user: "Resolved X/Y threads."
6. **Send a DingTalk fix notification** — only after the fix is actually pushed. One message per fix commit (summarize multiple addressed items in one message). See "DingTalk fix notifications" below for the exact command + reporting contract. A `failed`/`skipped` send is surfaced to the user but does NOT block — keep going.

Skipping steps 2-5 after pushing code is the #1 failure mode of this skill. The push is not the end — the PR hygiene IS the deliverable.

### Incremental-first polling (default)

**Default to incremental mode** (no `--full` flag). On the first invocation for a PR (no state file exists), the incremental mode naturally reports ALL items as "new" — this IS the initial full scan, and it saves state so subsequent polls only report deltas.

This avoids the duplicate-reply problem: `--full` reports all items on every poll, which combined with the autonomous comment handling causes the model to re-process and re-reply to already-handled threads across sessions.

**Reserve `--full` for read-only audits** where the user explicitly asks "show me everything on this PR" and does NOT want autonomous action. When `--full` is used, **do NOT trigger autonomous comment handling** (no replies, no fixes, no thread resolution) — only report and classify. The `--full` flag does NOT modify the state file.

On each incremental poll, the model's job is:

1. Process genuinely new items (comments, reviews, CI failures) that appeared since the last poll.
2. Apply the dedup guard (check for existing replies) before replying to any thread.
3. Surface deterministic blockers (CHANGES_REQUESTED, merge conflicts) regardless of whether they are "new".

### Post-fix obligations (BLOCKING — do not return to user until complete)

**Every `git push` of fix commits MUST be followed by steps 2-5 from "Autonomous comment handling" above.** This is not optional cleanup — unresolved threads block reviewers from approving, and missing summary comments force them to re-read the entire diff.

Checklist (same as above, repeated for emphasis):
- [ ] Reply to each addressed thread individually on GitHub
- [ ] Post top-level summary comment with fix table
- [ ] Query unresolved threads via GraphQL → batch-resolve all fixed ones
- [ ] Report resolved/unresolved counts to user
- [ ] Send the DingTalk fix notification (one per fix commit)

**On each subsequent poll**, report the remaining unresolved thread count. The target is: only "pushed back" threads + new threads remain unresolved. If a "fixed" thread is still showing as unresolved, resolve it.

### DingTalk fix notifications

After the model **actually fixes something and the fix is pushed** to the PR branch, send a DingTalk notification via the helper script. This keeps the user informed of autonomous fixes happening in background polls without forcing them to watch the terminal.

**When to send:**
- A branch-related CI/lint/typecheck/test/build failure was fixed, committed, and pushed.
- Actionable review feedback was fixed, pushed, and the GitHub thread resolved.

**When NOT to send** (these are not pushed fixes):
- Plain polling snapshots, green/ready milestones, mergeability changes.
- Flaky reruns (`gh run rerun --failed`) — no branch change.
- Diagnostics, classifications, or suggested replies that didn't change the branch.

Send **at most one notification per pushed fix commit**. If one commit fixes multiple review comments or CI failures, summarize them in one message.

```bash
python3 ~/.claude/skills/babysit-pr/scripts/dingtalk_notify.py \
  --title "PR #<n> fix pushed" \
  --text "Problems: <what failed>. Fixed: <what changed>. How: <approach/tests>. Rejected: <items + reasons, or none>. Decisions: <needed user decision, or none>. Commit: <sha>. <PR URL>"
```

The helper sends through the locally configured OpenClaw DingTalk route (channel `dingtalk-connector`, target `079458`). Overridable via env: `BABYSIT_PR_DINGTALK_OPENCLAW_BIN`, `BABYSIT_PR_DINGTALK_OPENCLAW_CHANNEL`, `BABYSIT_PR_DINGTALK_OPENCLAW_TARGET`. Use `--dry-run` to preview the message without sending.

Inspect the helper's JSON `status`:
- `sent` → note it in the user-facing report and continue.
- `skipped` / `failed` → surface the delivery problem to the user, then continue babysitting. Do NOT silently swallow a notification failure, but do NOT treat it as a hard blocker unless the user asked for guaranteed delivery.

### Post-approval cleanup

When the PR receives its first `APPROVED` review and all CHANGES_REQUESTED are dismissed:
- If there are review-fix commits on top of the original PR commits (e.g., "fix: address wenshao review round N"), suggest squashing them into the original commits before merge: "There are N fix commits that could be squashed before merge. Want me to squash?"
- Do NOT auto-squash — the user may want the fix history preserved for audit.
- If the user agrees, interactive rebase is not available in this environment; instead, create a new branch from the base, cherry-pick the original commits, apply the fixes as amendments, and force-push.

### Auto-replying to bot reviewers (no user approval needed)

After classifying new items, apply this policy to **bot-authored comments / reviews only**:

| Author class | Your judgment | Action |
| --- | --- | --- |
| Bot (login matches `*[bot]` / `copilot-pull-request-reviewer` / known auto-reviewers) | "ignore" / "defer" / "judgment errored" | **Post a brief reply directly via `gh api` — do not ask the user.** Reply explains your reasoning so the PR thread has a public record of why the suggestion wasn't taken. |
| Bot | "agree, will fix" | Post a brief acknowledgment, then propose the fix to the user as normal. |

Human reviewers are handled by the "Autonomous comment handling" section above — not this table. This table is bot-only because a misjudged "ignore" against a bot has zero cost (the bot won't escalate), while humans are handled with the full verify-then-act workflow.

**How to post the reply:**
- Top-level review summary → reply on the issue comment thread:
  `gh api repos/<owner>/<repo>/issues/<pr>/comments -f body="..."`
- Inline review comment → reply in the same thread:
  `gh api repos/<owner>/<repo>/pulls/<pr>/comments -f body="..." -F in_reply_to=<comment_id>`

**Reply format (keep terse):**
- Lead with the verdict: `Thanks — won't take this one.` / `Thanks — agreed, will fix.` / `Thanks — deferring.`
- One sentence per declined suggestion explaining why (factual misread / true nit but not worth the churn / out of scope / etc.).
- No emojis, no Co-Authored-By footer, no "🤖 Generated with..." attribution.

After posting, mention in your user-facing report that you replied so the user can intervene if they disagree before the bot picks it up on the next round.

## Approval-aware suspend (post-first-approval)

Once a PR has received at least one **human-authored** `APPROVED` review, **suspend** the comment-classification + bot-ack + fix-proposal workflow on subsequent polls. Bot approvals (login matching `*[bot]`) do NOT count toward the approval threshold — they lack the authority to gate merge and should not suppress the active-mode workflow. The cron-driven polling itself keeps running — only the per-poll behavior changes — until the PR is `MERGED` (or `CLOSED`), at which point the model recommends cancelling the cron via `CronDelete <job_id>` (the user can find it via `CronList`).

**Why suspend after first human approval?** Most repos require ≥2 approvals before merge. The second approval often arrives within minutes of the first as reviewers wake up to the same notification thread. Churning through bot/reviewer noise during that window risks (a) racing with the second reviewer's read of the discussion, (b) wasting the model's context on comments the second reviewer would have rendered moot, and (c) shipping a fix commit that invalidates the first reviewer's already-in-flight ack. Better to wait for the merge / second approval / changes-requested signal and then resume one batch at a time.

**At the start of every poll**, before applying the model judgment rubric, gather PR state:

```bash
gh pr view <pr> --repo <repo> --json state,reviews \
  --jq '{state,
         approvals: ([.reviews[] | select(.state == "APPROVED") | select(.author.login | test("\\[bot\\]$") | not)] | length),
         bot_approvals: ([.reviews[] | select(.state == "APPROVED") | select(.author.login | test("\\[bot\\]$"))] | length),
         changes_requested: ([.reviews[] | select(.state == "CHANGES_REQUESTED")] | length)}'
```

`approvals` counts only human reviewers; `bot_approvals` is reported for visibility but does NOT influence the suspend decision.

Then branch on PR state + human approval count (bot approvals are ignored for this decision):

| PR state / human approvals | Behavior |
| --- | --- |
| `state == MERGED` | Report "✅ merged at \<timestamp\>". Recommend cancelling the cron — invoke `CronList` to find the job id watching this PR (the prompt field will contain `/babysit-pr <pr>`), then `CronDelete <job_id>`. The skill's job is done — do not re-process unread comments, do not classify, do not auto-rerun CI. |
| `state == CLOSED` (not merged) | Report "❌ closed without merge". Same `CronList` → `CronDelete` recommendation. |
| `state == OPEN`, `changes_requested >= 1` | **Active mode** (CHANGES_REQUESTED overrides any approvals — the PR is gated until the requesting reviewer dismisses the block). Full classification + bot ack + fix proposals as today. |
| `state == OPEN`, `approvals >= 1` (human only), `changes_requested == 0` | **Suspended mode**: still echo the script's "what's new" header so the user sees activity counts, still surface deterministic blockers (merge conflicts, NEW failed CI checks worth auto-rerunning per the flake rubric — flake auto-rerun stays on, real-CI-failure surface stays on), but DO NOT do the per-comment rubric classification, DO NOT auto-reply to bots, DO NOT propose fixes. End the report with "_Suspended pending second approval / merge — N new comments since last poll, not classified._" The user can ask in natural language ("classify wenshao's comments anyway") to override per-poll. |
| `state == OPEN`, `approvals == 0` (human only; bot approvals don't count) | **Active mode**: full classification + bot ack + fix proposals, as today. A PR with only bot approvals stays in active mode. |

**Three things still surface even in suspended mode** (these override approval count because they're orthogonal to "are we waiting for a second reviewer"):

1. **Merge conflicts** (`meta.mergeable == CONFLICTING`). The base advancing during the suspend window is exactly the kind of thing the cron exists to catch. **Auto-resolve** via rebase + force-push (same procedure as active mode).
2. **CHANGES_REQUESTED appearing during suspend** — the new review reverses the approval-gating logic. Re-enter active mode immediately and classify.
3. **CI failures**. Auto-rerun flakes per the existing rubric (no user approval needed for rerun). Real CI failures surface as fix proposals — they may have just landed and would block merge regardless of approval count.

What does NOT surface in suspended mode: per-comment classification, bot acks, dispute / decision-level analysis, nits. Those resume on `state == MERGED` confirmation (where they're moot) or on a CHANGES_REQUESTED that flips back to active mode.

## State management

State lives at `~/.claude/state/babysit-pr/<owner-repo>-<pr>.json`. Schema:

```json
{
  "comments_seen": [3286687487, 3286980554, ...],
  "reviews_seen": ["PRR_kw...=", ...],
  "issue_comments_seen": [9876543210, ...],
  "checks_seen": [54321098, 54321099, ...],
  "updated_at": 1779692575,
  "meta": { "state": "OPEN", "title": "...", "mergeable": "MERGEABLE", "isDraft": false, "head_sha": "abc123..." }
}
```

`issue_comments_seen` was added when the skill was extended to top-level comments; `checks_seen` was added when CI polling landed. Older state files without these keys are forward-compatible — the script defaults missing keys to `[]`, so the first poll after upgrade reports all existing items as new.

**Check-run ids are per-attempt**, not per-check-name. A rerun mints a new id, so a re-failed check after rerun shows up as a NEW failure on the next poll (with `run_attempt: 2+` on the new record). That's intentional — it lets the model see "I reran this last time and it still failed; stop calling it flake."

This persists across Claude Code sessions — re-launching Claude does NOT reset the baseline. If the user wants a fresh baseline (e.g., switching reviewers, want to re-read everything), pass `--reset-state`.

## Combining with `/loop`

For periodic background polling:
```
/loop 30m /babysit-pr 4432
```

`/loop` handles the cron scheduling; this skill handles the per-poll classification. **Default to incremental mode** — the first fire reports everything (no prior state), subsequent fires report only deltas. Each fire produces either an incremental classification (active mode) or a suspended-mode summary.

For a read-only full audit (no autonomous action), pass `--full` explicitly:
```
/loop 30m /babysit-pr 4432 --full
```

**Cron durability**: When setting up recurring polls via `CronCreate`, use `durable: true` so the job survives session restarts and continues polling across Claude sessions. The 7-day auto-expiry still applies. Session-only cron is only appropriate for short-lived "watch for the next 30 minutes" use cases.

**When to stop the cron**: only when the PR is `MERGED` or `CLOSED`. Approval count alone does NOT stop the cron — see the "Approval-aware suspend" section above for why. The skill prints the recommended `CronDelete <job_id>` command on the first poll that observes a terminal PR state; the user invokes the cancel themselves so the action is visible in their session log rather than silently happening in the cron handler.

## Boundary with Codex's babysit-pr

The Codex variant at `~/Projects/claude-code4qwen-code/codex/.codex/skills/babysit-pr/` does CI watching, auto-fix flaky retries, and review-comment processing in one 869-line Python tool. This skill covers comment polling + CI failure surfacing + auto-rerun-on-flake, but stops short of auto-fixing PR-code-related CI failures — that's an editor-style action that needs the model in the driver's seat with the user's permission, not a background poller. If the user wants fully autonomous fix-and-retry loops, point them at the Codex variant; if they just want "tell me what broke and rerun the flakes", this skill is the right tool.

## Errors

- `gh not authenticated` → tell the user to `gh auth login`.
- `Could not auto-detect PR` → ask the user for `--pr <number>` explicitly.
- `Repo not detected` → run from inside the repo OR pass `--repo owner/name`.
