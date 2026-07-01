# babysit-pr

Autonomous PR babysitter skill for AI coding agents. Polls GitHub PRs for review comments, CI failures, and merge conflicts — then acts on them: fixes code, replies to reviewers, reruns flaky CI, resolves threads, and sends DingTalk notifications.

## Supported Platforms

| Platform | Polling Model | Install Target |
|----------|--------------|----------------|
| **Claude Code** | Single-invocation poll + `/loop` cron scheduling | `~/.claude/skills/babysit-pr/` |
| **Codex** | Continuous `--watch` mode (JSONL streaming) | `.codex/skills/babysit-pr/` |

Both platforms share the same core behaviors:

- **Autonomous comment handling** — reads reviewer comments, verifies claims against the code, decides agree/disagree/partially agree/uncertain, and acts accordingly
- **Model judgment rubric** — classifies comments into Blocker / Dispute / Decision-level / Critical bug / Nit
- **Bot auto-reply** — replies to bot reviewers without user approval
- **Dedup guard** — GraphQL-based check prevents duplicate replies across sessions
- **Post-fix obligations** — push → reply threads → summary comment → resolve threads → report counts → DingTalk notify
- **CI failure triage** — classifies PR-code-related vs infra/flake, auto-reruns flakes
- **DingTalk notifications** — sends fix/decision/blocker notifications via webhook robot or OpenClaw

## Installation

```bash
# Clone
git clone https://github.com/doudouOUC/babysit-pr.git
cd babysit-pr

# Install for Claude Code
make install-claude-code

# Install for Codex
make install-codex
```

## Directory Structure

```
babysit-pr/
├── README.md
├── Makefile
├── shared/
│   └── scripts/
│       ├── dingtalk_notify.py          # DingTalk notification helper
│       └── test_dingtalk_notify.py
├── claude-code/
│   ├── SKILL.md                        # Claude Code skill instructions
│   └── scripts/
│       ├── poll.py                     # Stateful single-invocation poller
│       └── test_poll.py
└── codex/
    ├── SKILL.md                        # Codex skill instructions
    ├── scripts/
    │   ├── gh_pr_watch.py              # Continuous watcher (--watch / --once)
    │   └── test_gh_pr_watch.py
    ├── references/
    │   ├── heuristics.md               # CI classification checklist
    │   └── github-api-notes.md         # GitHub CLI/API reference
    └── agents/
        └── openai.yaml                 # Codex agent config
```

## Platform Differences

| Feature | Claude Code | Codex |
|---------|------------|-------|
| Polling | `poll.py` + `/loop` cron | `gh_pr_watch.py --watch` |
| Scheduling | `CronCreate` / `CronDelete` | In-process continuous loop |
| State file | `~/.claude/state/babysit-pr/` | `/tmp/codex-babysit-pr-*.json` |
| Approval-aware suspend | Yes (suspends after first human APPROVED) | No (always active) |
| Post-approval squash suggestion | Yes | No |

## Prerequisites

- **GitHub CLI** (`gh`) — authenticated via `gh auth login`
- **Python 3.9+**
- **DingTalk notifications** (optional) — webhook robot (recommended) or OpenClaw CLI. See [DingTalk setup](#dingtalk-notifications) below.

## Usage

### Claude Code

```bash
# One-shot poll
/babysit-pr 4432

# Continuous polling (every 30 minutes)
/loop 30m /babysit-pr 4432
```

### Codex

```bash
# One-shot snapshot
python3 .codex/skills/babysit-pr/scripts/gh_pr_watch.py --pr 4432 --once

# Continuous watch
python3 .codex/skills/babysit-pr/scripts/gh_pr_watch.py --pr 4432 --watch
```

## Configuration

### DingTalk Notifications

Two transport options are available. The script auto-selects based on which env vars are set.

#### Option A: Webhook Robot (recommended, zero dependency)

No external tools required — uses Python stdlib only.

1. **Create a custom robot** in your DingTalk group:
   - Group Settings → Smart Group Assistant → Add Robot → Custom
   - Security: choose **Sign** (recommended) or **Custom Keywords**
   - Copy the **Webhook URL** (contains `access_token=xxx`) and the **signing secret** (`SECxxx`)

2. **Set environment variables**
   ```bash
   # Required: the access_token part from the webhook URL
   export BABYSIT_PR_DINGTALK_WEBHOOK_TOKEN="your_access_token_here"

   # Optional: signing secret (required if robot uses Sign security)
   export BABYSIT_PR_DINGTALK_WEBHOOK_SECRET="SECxxx"
   ```

3. **Test**
   ```bash
   # Dry-run (preview without sending)
   python3 shared/scripts/dingtalk_notify.py \
     --title "test / 测试" --text "hello" --dry-run

   # Real send
   python3 shared/scripts/dingtalk_notify.py \
     --title "test / 测试" --text "Setup works! / 设置成功！"
   ```

| Variable | Required | Description |
|----------|----------|-------------|
| `BABYSIT_PR_DINGTALK_WEBHOOK_TOKEN` | Yes | DingTalk robot access_token |
| `BABYSIT_PR_DINGTALK_WEBHOOK_SECRET` | No | HMAC-SHA256 signing secret (`SECxxx`) |

> **Rate limit**: DingTalk custom robots allow 20 messages/minute. The skill's "at most one notification per event type per poll cycle" stays within this.

#### Option B: OpenClaw CLI (internal users)

For organizations with [OpenClaw](https://openclaw.alibaba-inc.com/) infrastructure.

1. **Install OpenClaw CLI**
   ```bash
   openclaw --version   # verify installation
   ```

2. **Verify channel**
   ```bash
   openclaw channel list   # should show dingtalk-connector
   ```

3. **Test**
   ```bash
   python3 shared/scripts/dingtalk_notify.py \
     --title "test / 测试" --text "hello" --dry-run
   ```

| Variable | Default | Description |
|----------|---------|-------------|
| `BABYSIT_PR_DINGTALK_OPENCLAW_BIN` | `openclaw` | OpenClaw binary path |
| `BABYSIT_PR_DINGTALK_OPENCLAW_CHANNEL` | `dingtalk-connector` | DingTalk channel name |
| `BABYSIT_PR_DINGTALK_OPENCLAW_TARGET` | `079458` | DingTalk user/group ID |

#### No DingTalk?

If neither webhook token nor OpenClaw is configured, the skill still works — DingTalk notifications are skipped with `status: skipped`, and all other features (polling, comment handling, CI triage) function normally.

## Testing

```bash
# Shared scripts
python3 -m unittest shared/scripts/test_dingtalk_notify.py

# Claude Code scripts
python3 -m unittest claude-code/scripts/test_poll.py

# Codex scripts (requires pytest)
python3 -m pytest codex/scripts/test_gh_pr_watch.py
```

## License

MIT
