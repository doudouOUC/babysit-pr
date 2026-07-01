#!/usr/bin/env python3
"""Send DingTalk notifications for PR babysitting fix events."""

import argparse
import json
import os
import subprocess
import sys

OPENCLAW_BIN_ENV = "BABYSIT_PR_DINGTALK_OPENCLAW_BIN"
OPENCLAW_CHANNEL_ENV = "BABYSIT_PR_DINGTALK_OPENCLAW_CHANNEL"
OPENCLAW_TARGET_ENV = "BABYSIT_PR_DINGTALK_OPENCLAW_TARGET"
DEFAULT_OPENCLAW_BIN = "openclaw"
DEFAULT_OPENCLAW_CHANNEL = "dingtalk-connector"
DEFAULT_OPENCLAW_TARGET = "079458"
TIMEOUT_SECONDS = 60


def build_message(title, text):
    return f"{title}\n\n{text}"


def default_command_runner(cmd, timeout_seconds):
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as err:
        return 127, "", str(err)
    except subprocess.TimeoutExpired as err:
        stdout = err.stdout or ""
        stderr = err.stderr or "command timed out"
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return 124, stdout, stderr
    return proc.returncode, proc.stdout, proc.stderr


def parse_json_from_output(output):
    start = output.find("{")
    if start < 0:
        return None
    try:
        return json.loads(output[start:])
    except json.JSONDecodeError:
        return None


def send_openclaw_notification(
    title,
    text,
    env,
    command_runner=default_command_runner,
    timeout_seconds=TIMEOUT_SECONDS,
):
    binary = str(env.get(OPENCLAW_BIN_ENV, DEFAULT_OPENCLAW_BIN)).strip()
    channel = str(env.get(OPENCLAW_CHANNEL_ENV, DEFAULT_OPENCLAW_CHANNEL)).strip()
    target = str(env.get(OPENCLAW_TARGET_ENV, DEFAULT_OPENCLAW_TARGET)).strip()
    if not binary or not channel or not target:
        return {
            "status": "skipped",
            "transport": "openclaw",
            "reason": "missing OpenClaw binary, channel, or target",
        }

    cmd = [
        binary,
        "message",
        "send",
        "--channel",
        channel,
        "--target",
        target,
        "--message",
        build_message(title, text),
        "--json",
    ]
    code, stdout, stderr = command_runner(cmd, timeout_seconds)
    if code != 0:
        return {
            "status": "failed",
            "transport": "openclaw",
            "exit_code": code,
            "stderr": stderr.strip(),
            "stdout": stdout.strip(),
        }

    parsed = parse_json_from_output(stdout)
    result = {"status": "sent", "transport": "openclaw"}
    if isinstance(parsed, dict):
        payload = parsed.get("payload")
        if isinstance(payload, dict):
            message_result = payload.get("result")
            if isinstance(message_result, dict):
                result["message_id"] = message_result.get("messageId")
                result["conversation_id"] = message_result.get("conversationId")
    return result


def send_notification(
    title,
    text,
    env=os.environ,
    command_runner=default_command_runner,
    timeout_seconds=TIMEOUT_SECONDS,
):
    return send_openclaw_notification(
        title,
        text,
        env,
        command_runner=command_runner,
        timeout_seconds=timeout_seconds,
    )


def read_text(args):
    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as file:
            return file.read()
    if args.text:
        return args.text
    raise SystemExit("--text or --text-file is required")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Send a DingTalk notification for a babysit-pr fix event."
    )
    parser.add_argument("--title", required=True)
    parser.add_argument("--text")
    parser.add_argument("--text-file")
    parser.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    text = read_text(args)
    if args.dry_run:
        result = {
            "status": "dry_run",
            "message": build_message(args.title, text),
            "openclaw": {
                "binary": os.environ.get(OPENCLAW_BIN_ENV, DEFAULT_OPENCLAW_BIN),
                "channel": os.environ.get(
                    OPENCLAW_CHANNEL_ENV,
                    DEFAULT_OPENCLAW_CHANNEL,
                ),
                "target": os.environ.get(
                    OPENCLAW_TARGET_ENV,
                    DEFAULT_OPENCLAW_TARGET,
                ),
            },
        }
    else:
        result = send_notification(args.title, text, timeout_seconds=args.timeout)

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result["status"] == "failed":
        return 1
    if result["status"] == "skipped":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
