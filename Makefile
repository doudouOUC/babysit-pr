.PHONY: install-claude-code install-codex test clean

CLAUDE_CODE_TARGET := $(HOME)/.claude/skills/babysit-pr
CODEX_TARGET := .codex/skills/babysit-pr

install-claude-code:
	@echo "Installing babysit-pr skill for Claude Code..."
	mkdir -p $(CLAUDE_CODE_TARGET)/scripts
	cp claude-code/SKILL.md $(CLAUDE_CODE_TARGET)/
	cp claude-code/scripts/poll.py $(CLAUDE_CODE_TARGET)/scripts/
	cp claude-code/scripts/test_poll.py $(CLAUDE_CODE_TARGET)/scripts/
	cp shared/scripts/dingtalk_notify.py $(CLAUDE_CODE_TARGET)/scripts/
	cp shared/scripts/test_dingtalk_notify.py $(CLAUDE_CODE_TARGET)/scripts/
	@echo "Installed to $(CLAUDE_CODE_TARGET)"

install-codex:
	@echo "Installing babysit-pr skill for Codex..."
	mkdir -p $(CODEX_TARGET)/scripts $(CODEX_TARGET)/references $(CODEX_TARGET)/agents
	cp codex/SKILL.md $(CODEX_TARGET)/
	cp codex/scripts/gh_pr_watch.py $(CODEX_TARGET)/scripts/
	cp codex/scripts/test_gh_pr_watch.py $(CODEX_TARGET)/scripts/
	cp codex/references/heuristics.md $(CODEX_TARGET)/references/
	cp codex/references/github-api-notes.md $(CODEX_TARGET)/references/
	cp codex/agents/openai.yaml $(CODEX_TARGET)/agents/
	cp shared/scripts/dingtalk_notify.py $(CODEX_TARGET)/scripts/
	cp shared/scripts/test_dingtalk_notify.py $(CODEX_TARGET)/scripts/
	@echo "Installed to $(CODEX_TARGET)"

test:
	python3 -m unittest shared/scripts/test_dingtalk_notify.py
	python3 -m unittest claude-code/scripts/test_poll.py
	@echo "All tests passed."

clean:
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
