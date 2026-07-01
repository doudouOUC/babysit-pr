---
name: babysit-pr
description: Babysit a GitHub pull request after creation by continuously polling review comments, CI checks/workflow runs, and mergeability state until the PR is merged/closed or user help is required. Diagnose failures, retry likely flaky failures up to 3 times, auto-fix/push branch-related issues when appropriate, and keep watching open PRs so fresh review feedback is surfaced promptly. Use when the user asks Codex to monitor a PR, watch CI, handle review comments, or keep an eye on failures and feedback on an open PR.
---

# PR Babysitter

## Objective
Babysit a PR persistently until one of these terminal outcomes occurs:

- The PR is merged or closed.
- A situation requires user help (for example CI infrastructure issues, repeated flaky failures after retry budget is exhausted, permission problems, or ambiguity that cannot be resolved safely).
- Optional handoff milestone: the PR is currently green + mergeable + review-clean. Treat this as a progress state, not a watcher stop, so late-arriving review comments are still surfaced promptly while the PR remains open.

Do not stop merely because a single snapshot returns `idle` while checks are still pending.

## Inputs
Accept any of the following:

- No PR argument: infer the PR from the current branch (`--pr auto`)
- PR number
- PR URL

## Core Workflow

1. When the user asks to "monitor"/"watch"/"babysit" a PR, start with the watcher's continuous mode (`--watch`) unless you are intentionally doing a one-shot diagnostic snapshot.
2. Run the watcher script to snapshot PR/review/CI state (or consume each streamed snapshot from `--watch`).
3. Inspect the `actions` list in the JSON response.
4. If `diagnose_ci_failure` is present, inspect failed run logs and classify the failure.
5. If the failure is likely caused by the current branch, patch code locally, commit, and push. Do not patch random flaky tests, CI infrastructure, dependency outages, runner issues, or other failures that are unrelated to the branch.
6. If `process_review_comment` is present, inspect surfaced review items and apply the **Autonomous comment handling** rubric below to decide how to act on each one.
7. If a review item is actionable, patch code locally, commit, push, reply to the thread, resolve it, and post a top-level summary comment — see the full post-fix obligations in "Review Comment Handling" below.
8. For bot-authored comments, auto-reply without user approval per the bot policy below. For human comments, form your own judgment (agree / partially agree / disagree / uncertain) and act accordingly — only defer to the user when genuinely uncertain.
9. If the failure is likely flaky/unrelated and `retry_failed_checks` is present, rerun failed jobs with `--retry-failed-now`.
10. If both actionable review feedback and `retry_failed_checks` are present, prioritize review feedback first; a new commit will retrigger CI, so avoid rerunning flaky checks on the old SHA unless you intentionally defer the review change.
11. On every loop, look for newly surfaced review feedback before acting on CI failures or mergeability state, then verify mergeability / merge-conflict status (for example via `gh pr view`) alongside CI.
12. After any push or rerun action, immediately return to step 1 and continue polling on the updated SHA/state.
13. If you had been using `--watch` before pausing to patch/commit/push, relaunch `--watch` yourself in the same turn immediately after the push (do not wait for the user to re-invoke the skill).
14. Repeat polling until `stop_pr_closed` appears or a user-help-required blocker is reached. A green + review-clean + mergeable PR is a progress milestone, not a reason to stop the watcher while the PR is still open.
15. Maintain terminal/session ownership: while babysitting is active, keep consuming watcher output in the same turn; do not leave a detached `--watch` process running and then end the turn as if monitoring were complete.

## Commands

### One-shot snapshot

```bash
python3 .codex/skills/babysit-pr/scripts/gh_pr_watch.py --pr auto --once
```

### Continuous watch (JSONL)

```bash
python3 .codex/skills/babysit-pr/scripts/gh_pr_watch.py --pr auto --watch
```

### Trigger flaky retry cycle (only when watcher indicates)

```bash
python3 .codex/skills/babysit-pr/scripts/gh_pr_watch.py --pr auto --retry-failed-now
```

### Explicit PR target

```bash
python3 .codex/skills/babysit-pr/scripts/gh_pr_watch.py --pr <number-or-url> --once
```

## CI Failure Classification
Use `gh` commands to inspect failed runs before deciding to rerun.

- `gh run view <run-id> --json jobs,name,workflowName,conclusion,status,url,headSha`
- `gh api repos/<owner>/<repo>/actions/runs/<run-id>/jobs -X GET -f per_page=100`
- `gh api repos/<owner>/<repo>/actions/jobs/<job-id>/logs > /tmp/codex-gh-job-<job-id>-logs.zip`
- `gh run view <run-id> --log-failed` as a fallback after the overall workflow run is complete

`gh run view --log-failed` is workflow-run scoped and may not expose failed-job logs until the overall run finishes. For faster diagnosis, poll the run's jobs first and, as soon as a specific job has failed, fetch that job's logs directly from the Actions job logs endpoint. The watcher includes a `failed_jobs` list with each failed job's `job_id` and `logs_endpoint` when GitHub exposes one.

Prefer treating failures as branch-related when failed-job logs point to changed code (compile/test/lint/typecheck/snapshots/static analysis in touched areas).

Prefer treating failures as flaky/unrelated when logs show transient infra/external issues (timeouts, runner provisioning failures, registry/network outages, GitHub Actions infra errors).

Do not attempt to fix flaky/unrelated failures by changing tests, build scripts, CI configuration, dependency pins, or infrastructure-adjacent code unless the logs clearly connect the failure to the PR branch. For flaky/unrelated failures, rerun only when the watcher recommends `retry_failed_checks`; otherwise wait or stop for user help.

If classification is ambiguous, perform one manual diagnosis attempt before choosing rerun.

Read `.codex/skills/babysit-pr/references/heuristics.md` for a concise checklist.

## Review Comment Handling

The watcher surfaces review items from:

- PR issue comments
- Inline review comments
- Review submissions (COMMENT / APPROVED / CHANGES_REQUESTED)

It intentionally surfaces Codex reviewer bot feedback (for example comments/reviews from `chatgpt-codex-connector[bot]`) in addition to human reviewer feedback. Most unrelated bot noise should still be ignored.
For safety, the watcher only auto-surfaces trusted human review authors (for example repo OWNER/MEMBER/COLLABORATOR, plus the authenticated operator) and approved review bots such as Codex.
On a fresh watcher state file, existing pending review feedback may be surfaced immediately (not only comments that arrive after monitoring starts). This is intentional so already-open review comments are not missed.
If a code review comment/thread is already marked as resolved in GitHub, treat it as non-actionable and safely ignore it unless new unresolved follow-up feedback appears.

### Model judgment rubric (REQUIRED after each poll)

After the watcher surfaces new items, classify each new comment / review into ONE OR MORE of:

| Category | Definition | When to surface to user |
| --- | --- | --- |
| **Merge conflict** | `mergeable == CONFLICTING`. Deterministic; no judgment needed. | **Auto-resolve immediately** via rebase + force-push. Surface to user only if semantic conflict. |
| **Blocker** | PR cannot land as-is. CHANGES_REQUESTED review, security/correctness bug in the diff, or maintainer (`OWNER`/`MEMBER`/`COLLABORATOR`) explicitly saying "must change before merge". | Always surface, prominently. |
| **Dispute** | Reviewer disagrees with approach or design. Phrased as "I'd push back on X", "this won't work because Y", "consider Z instead", "WDYT?". | Always surface with author + position summary. |
| **Decision-level suggestion** | Requires a deliberate accept/reject call (architectural, public API naming, scope change, tradeoff). | Surface; flag that user input is needed. |
| **Critical bug / security** | Genuinely broken code — wrong logic, race condition, security vulnerability, regression. Not style. | Surface; recommend immediate fix. |
| **Nit / style / preference** | "Could rename this", "minor formatting", "personal preference". Author may take or leave. | Batch into "non-blocking suggestions" summary. |
| **CI failure — PR-code-related** | Failed check whose root cause is in the diff. | Surface; propose fix; do NOT auto-rerun. |
| **CI failure — infra / flake** | Failed check unrelated to the diff (setup failure, network, OOM, runner issue). | Auto-rerun via `gh run rerun <id> --failed`. |

**How to make the judgment** — weigh these signals per comment:

1. **`author_association`**: `OWNER` / `MEMBER` / `COLLABORATOR` carry weight; `CONTRIBUTOR` / `NONE` / bots are advisory unless rigorous.
2. **The actual code (`diff_hunk`)**: comment about untouched code is less blocking than about changed code. Correctness > style on the same line.
3. **PR description / objective**: comments outside the PR's stated scope are usually deferrable; comments about the PR's objective being mis-implemented are blocking.
4. **Comment phrasing for disputes / decisions**: "I disagree", "wrong approach", "consider X instead", "WDYT?", "thoughts?" signal dispute / decision EVEN WITHOUT a severity tag.
5. **Repetition**: multiple reviewers raising the same concern → higher weight.
6. **Severity tags in body**: `[Critical]`, `[BLOCKER]`, `[Suggestion]`, `[nit]` are HINTS. Verify against content; do not trust tags blindly.

### Autonomous comment handling

After classifying all new comments, **act on them directly** instead of waiting for user direction. The model must form its own judgment — reviewer tags are hints, not commands.

**Decision process per comment:**

1. **Read the code the comment points at.** Understand the claim before deciding.
2. **Verify the claim independently.** Check whether the bug/issue actually exists by reading surrounding code, tracing call paths, reasoning about edge cases.
3. **Decide: agree, partially agree, disagree, or uncertain.**

| Judgment | Action |
| --- | --- |
| **Agree — real bug or clear improvement** | Fix the code directly, no user approval needed. Applies regardless of tag. |
| **Partially agree — valid concern but wrong fix** | Fix the underlying issue in a better way. Reply explaining what you did differently and why. |
| **Disagree — factual misread or not worth the churn** | Push back: reply on the PR thread explaining why. Don't fix. Don't ask the user. |
| **Uncertain — needs domain context you don't have** | Surface to the user with your analysis of both sides. This is the ONLY case where you wait. |

**What counts as "agree":**
- The reviewer points at genuinely broken code (wrong logic, race, missing error handling) and you can reproduce the reasoning.
- The reviewer's suggested fix is correct or close enough to adapt.
- The improvement is small, safe, and clearly better than the current code.

**What counts as "push back":**
- The reviewer's claim is based on a misread (e.g., they missed a guard clause upstream).
- The suggestion would make the code worse (unnecessary complexity, scope expansion beyond the PR's objective).
- The concern is valid in theory but doesn't apply here (e.g., "this could overflow" when the input is bounded).

### Dedup guard (REQUIRED before replying)

For each thread you intend to reply to, check whether the PR author (or the current operator) has already posted a reply. **If a reply already exists, skip that thread entirely** — do not post a duplicate, even if your judgment differs from the prior reply.

```bash
# Find unresolved threads with no reply from us:
gh api graphql -f query='{ repository(owner:"<owner>",name:"<repo>") { pullRequest(number:<pr>) { reviewThreads(first:100) { nodes { id isResolved comments(first:10) { nodes { author { login } } } } } } } }' \
  --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved==false) | select([.comments.nodes[].author.login] | any(. == "<pr_author>") | not) | .id'
```

Only reply to threads returned by this query (unresolved AND no prior reply from us). If the watcher later surfaces your own reply because the authenticated operator is treated as a trusted review author, treat it as already handled and do not reply again.

### Post-fix obligations (BLOCKING — do not return to user until complete)

After deciding, execute these steps IN ORDER for every fix commit:

1. `git add && git commit && git push` — push the fix commit (use `codex: address PR review feedback (#<n>)` message).
2. **Reply to EACH addressed thread on GitHub** — use `gh api repos/<owner>/<repo>/pulls/<pr>/comments -f body="..." -F in_reply_to=<comment_id>` for inline threads. Terse: "Fixed in <sha>." / "Not taking — <reason>." / "Deferring — <reason>."
3. **Post a top-level PR summary comment** — table of all actions (fixed / pushed back / deferred) with commit SHA.
4. **Resolve ALL replied threads** — query all unresolved threads via GraphQL, then batch-resolve every thread that has been replied to.
   ```bash
   # List unresolved threads:
   gh api graphql -f query='{ repository(owner:"<owner>",name:"<repo>") { pullRequest(number:<pr>) { reviewThreads(first:100) { nodes { id isResolved comments(first:1) { nodes { body path line } } } } } } }' --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved==false)'
   # Resolve each replied thread:
   gh api graphql -f query='mutation { resolveReviewThread(input:{threadId:"<id>"}) { thread { isResolved } } }'
   ```
5. **Report the thread count** to the user: "Resolved X/Y threads."
6. **Send a DingTalk fix notification** — only after the fix is actually pushed. One message per fix commit (summarize multiple addressed items in one message). See "DingTalk fix notifications" below for the exact command + reporting contract. A `failed`/`skipped` send is surfaced to the user but does NOT block.
7. Resume watching on the new SHA immediately. If `--watch` was running, restart it in the same turn.

**Skipping steps 2-6 after pushing code is the #1 failure mode of this skill.** The push is not the end — the PR hygiene IS the deliverable.

On each subsequent poll, report the remaining unresolved thread count. Target: only "pushed back" threads + new threads remain unresolved. If a "fixed" thread is still showing as unresolved, resolve it.

### DingTalk fix notifications

Send a DingTalk notification to keep the user informed of autonomous activity happening in background watches without forcing them to watch the terminal.

**When to send:**
- **Fix pushed**: a branch-related CI/lint/typecheck/test/build failure was fixed, committed, and pushed; or actionable review feedback was fixed, pushed, and the GitHub thread resolved.
- **Decision needed (no push)**: the model encounters items classified as "uncertain" or "Decision-level suggestion" that require user input. These are NOT code pushes — they are prompts for the user to make a call. Send immediately when such items are identified, do not wait for a fix commit.
- **Blocker / Dispute surfaced**: a reviewer submitted CHANGES_REQUESTED, raised a dispute, or flagged a critical bug that the model cannot resolve autonomously. Send so the user is aware even if they are not watching the terminal.

**When NOT to send** (low-value noise):
- Plain polling snapshots with no new items, green/ready milestones, mergeability changes with no conflict.
- Flaky reruns (`gh run rerun --failed`) — no branch change, no decision needed.
- Nit / style comments that the model handled autonomously (agreed and fixed, or batched as non-blocking).

Send **at most one notification per event type per poll cycle**. If one commit fixes multiple review comments or CI failures, summarize them in one message. If multiple decision-needed items appear in one poll, batch them into one notification.

**Bilingual requirement**: every DingTalk notification MUST include both English and Chinese (中文). Write the English section first, then a `---` separator, then the Chinese section. This ensures all team members can read the notification regardless of language preference.

**Formatting rules**: write the message body to a temp file and pass via `--text-file`. Each field MUST be on its own line. Use blank lines between sections. Numbered items each get their own line. This is critical for readability in DingTalk's message card.

**Fix pushed notification:**
```bash
cat > /tmp/babysit-pr-msg.txt << 'MSGEOF'
[EN]
Problems:
- <what failed, one per line>

Fixed:
- <what changed, one per line>

How: <approach/tests>
Rejected: <items + reasons, or "none">
Decisions: <needed user decision, or "none">
Commit: <sha>
<PR URL>

---

[中文]
问题:
- <失败原因，每条一行>

修复:
- <改动内容，每条一行>

方式: <修复方法/测试>
拒绝: <拒绝项+原因，或"无">
待决策: <需用户决定的事项，或"无">
提交: <sha>
<PR URL>
MSGEOF
python3 .codex/skills/babysit-pr/scripts/dingtalk_notify.py \
  --title "PR #<n> fix pushed / PR #<n> 修复已推送" \
  --text-file /tmp/babysit-pr-msg.txt
```

**Decision needed notification (no push):**
```bash
cat > /tmp/babysit-pr-msg.txt << 'MSGEOF'
[EN]
Items needing decision: <count>

1) <reviewer> at <file:line>:
   <summary of what they're asking>

2) <reviewer> at <file:line>:
   <summary>

Recommended action: <what the model suggests>
<PR URL>

---

[中文]
需要决策的项: <数量>

1) <审查者> 在 <file:line>:
   <问题摘要>

2) <审查者> 在 <file:line>:
   <摘要>

建议操作: <模型的建议>
<PR URL>
MSGEOF
python3 .codex/skills/babysit-pr/scripts/dingtalk_notify.py \
  --title "PR #<n> needs your decision / PR #<n> 需要你的决策" \
  --text-file /tmp/babysit-pr-msg.txt
```

**Blocker surfaced notification:**
```bash
cat > /tmp/babysit-pr-msg.txt << 'MSGEOF'
[EN]
Blocker: <reviewer> submitted CHANGES_REQUESTED
Reason: <summary>
Action needed: <what must change>
<PR URL>

---

[中文]
阻塞: <审查者> 提交了 CHANGES_REQUESTED
原因: <摘要>
需要操作: <需要改什么>
<PR URL>
MSGEOF
python3 .codex/skills/babysit-pr/scripts/dingtalk_notify.py \
  --title "PR #<n> blocked / PR #<n> 被阻塞" \
  --text-file /tmp/babysit-pr-msg.txt
```

**Transport selection**: the script auto-selects the transport based on env vars:
- If `BABYSIT_PR_DINGTALK_WEBHOOK_TOKEN` is set → uses DingTalk webhook robot (zero dependency, recommended for open-source users). Optionally set `BABYSIT_PR_DINGTALK_WEBHOOK_SECRET` for HMAC signing.
- Otherwise → falls back to OpenClaw CLI (channel `dingtalk-connector`, target `079458`). Overridable via `BABYSIT_PR_DINGTALK_OPENCLAW_BIN`, `BABYSIT_PR_DINGTALK_OPENCLAW_CHANNEL`, `BABYSIT_PR_DINGTALK_OPENCLAW_TARGET`.

Both transports use the same `--text-file` message format and bilingual requirement. Use `--dry-run` to preview the message and active transport without sending.

Inspect the helper's JSON `status`:
- `sent` → note it in the user-facing report and continue.
- `skipped` / `failed` → surface the delivery problem AND the `setup_hint` field (if present) to the user. The hint contains the exact env vars and setup URL. Show it **once per session** on the first failure — don't repeat on every poll. Do NOT treat notification failure as a hard blocker unless the user asked for guaranteed delivery.

### Auto-replying to bot reviewers (no user approval needed)

Apply this policy to **bot-authored comments / reviews only** (login matches `*[bot]` / `copilot-pull-request-reviewer` / known auto-reviewers):

| Your judgment | Action |
| --- | --- |
| "ignore" / "defer" / "judgment errored" | Post a brief reply directly via `gh api` — do not ask the user. Reply explains reasoning so the PR thread has a record. |
| "agree, will fix" | Post a brief acknowledgment, then fix as normal per the post-fix obligations above. |

Human reviewers use the full autonomous decision process above — not this table. Bot misjudgment cost is near-zero (the bot won't escalate), so auto-reply is safe.

**Reply format (keep terse):**
- Lead with the verdict: `Thanks — won't take this one.` / `Thanks — agreed, will fix.` / `Thanks — deferring.`
- One sentence per declined suggestion explaining why.
- Prefix with `[codex]` so it is clear the response is automated.

After posting, mention in your user-facing report that you replied so the user can intervene if they disagree.

## Git Safety Rules

- Work only on the PR head branch.
- Avoid destructive git commands.
- Do not switch branches unless necessary to recover context.
- Before editing, check for unrelated uncommitted changes. If present, stop and ask the user.
- After each successful fix, commit and `git push`, then re-run the watcher.
- If you interrupted a live `--watch` session to make the fix, restart `--watch` immediately after the push in the same turn.
- Do not run multiple concurrent `--watch` processes for the same PR/state file; keep one watcher session active and reuse it until it stops or you intentionally restart it.
- A push is not a terminal outcome; continue the monitoring loop unless a strict stop condition is met.

Commit message defaults:

- `codex: fix CI failure on PR #<n>`
- `codex: address PR review feedback (#<n>)`

## Monitoring Loop Pattern
Use this loop in a live Codex session:

1. Run `--once`.
2. Read `actions`.
3. First check whether the PR is now merged or otherwise closed; if so, report that terminal state and stop polling immediately.
4. Check CI summary, new review items, and mergeability/conflict status.
5. Diagnose CI failures and classify branch-related vs flaky/unrelated. If the overall run is still pending but `failed_jobs` already includes a failed job, fetch that job's logs and diagnose immediately instead of waiting for the whole workflow run to finish. Patch only when the failure is branch-related.
6. For each surfaced review item from another author, apply the autonomous comment handling rubric: read the code, verify the claim, decide (agree / partially agree / disagree / uncertain). If actionable, patch/commit/push, reply to threads, post summary comment, and resolve threads per the post-fix obligations. If uncertain, surface to the user. For bot comments, auto-reply per the bot policy. Run the dedup guard before every reply.
7. Process actionable review comments before flaky reruns when both are present; if a review fix requires a commit, push it and skip rerunning failed checks on the old SHA.
8. Retry failed checks only when `retry_failed_checks` is present and you are not about to replace the current SHA with a review/CI fix commit. Do not make code changes for unrelated flakes or infrastructure failures just to get CI green.
9. If you pushed a commit, complete ALL post-fix obligations (reply threads → summary comment → resolve threads → report counts) before continuing. Report the action briefly and continue polling (do not stop). Only defer to the user when genuinely uncertain about a comment — not as a default.
10. After a review-fix push, proactively restart continuous monitoring (`--watch`) in the same turn unless a strict stop condition has already been reached.
11. If everything is passing, mergeable, not blocked on required review approval, and there are no unaddressed review items, report that the PR is currently ready to merge but keep the watcher running so new review comments are surfaced quickly while the PR remains open.
12. If blocked on a user-help-required issue (infra outage, exhausted flaky retries, unclear reviewer request, permissions), report the blocker and stop.
13. Otherwise sleep according to the polling cadence below and repeat.

When the user explicitly asks to monitor/watch/babysit a PR, prefer `--watch` so polling continues autonomously in one command. Use repeated `--once` snapshots only for debugging, local testing, or when the user explicitly asks for a one-shot check.
Do not stop to ask the user whether to continue polling; continue autonomously until a strict stop condition is met or the user explicitly interrupts.
Do not hand control back to the user after a review-fix push just because a new SHA was created; restarting the watcher and re-entering the poll loop is part of the same babysitting task.
If a `--watch` process is still running and no strict stop condition has been reached, the babysitting task is still in progress; keep streaming/consuming watcher output instead of ending the turn.

## Polling Cadence
Keep review polling aggressive and continue monitoring even after CI turns green:

- While CI is not green (pending/running/queued or failing): poll every 1 minute.
- After CI turns green: keep polling at the base cadence while the PR remains open so newly posted review comments are surfaced promptly instead of waiting on a long green-state backoff.
- Reset the cadence immediately whenever anything changes (new commit/SHA, check status changes, new review comments, mergeability changes, review decision changes).
- If CI stops being green again (new commit, rerun, or regression): stay on the base polling cadence.
- If any poll shows the PR is merged or otherwise closed: stop polling immediately and report the terminal state.

## Stop Conditions (Strict)
Stop only when one of the following is true:

- PR merged or closed (stop as soon as a poll/snapshot confirms this).
- User intervention is required and Codex cannot safely proceed alone.

Keep polling when:

- `actions` contains only `idle` but checks are still pending.
- CI is still running/queued.
- Review state is quiet but CI is not terminal.
- CI is green but mergeability is unknown/pending.
- CI is green and mergeable, but the PR is still open and you are waiting for possible new review comments or merge-conflict changes.
- The PR is green but blocked on review approval (`REVIEW_REQUIRED` / similar); continue polling at the base cadence and surface any new review comments without asking for confirmation to keep watching.

## Output Expectations
Provide concise progress updates while monitoring and a final summary that includes:

- During long unchanged monitoring periods, avoid emitting a full update on every poll; summarize only status changes plus occasional heartbeat updates.
- Treat push confirmations, intermediate CI snapshots, ready-to-merge snapshots, and review-action updates as progress updates only; do not emit the final summary or end the babysitting session unless a strict stop condition is met.
- A user request to "monitor" is not satisfied by a couple of sample polls; remain in the loop until a strict stop condition or an explicit user interruption.
- A review-fix commit + push is not a completion event; immediately resume live monitoring (`--watch`) in the same turn and continue reporting progress updates.
- When CI first transitions to all green for the current SHA, emit a one-time celebratory progress update (do not repeat it on every green poll). Preferred style: `🚀 CI is all green! 33/33 passed. Still on watch for review approval.`
- Do not send the final summary while a watcher terminal is still running unless the watcher has emitted/confirmed a strict stop condition; otherwise continue with progress updates.

- Final PR SHA
- CI status summary
- Mergeability / conflict status
- Fixes pushed
- Flaky retry cycles used
- Remaining unresolved failures or review comments

## References

- Heuristics and decision tree: `.codex/skills/babysit-pr/references/heuristics.md`
- GitHub CLI/API details used by the watcher: `.codex/skills/babysit-pr/references/github-api-notes.md`
