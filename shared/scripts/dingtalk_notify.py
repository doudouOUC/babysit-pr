#!/usr/bin/env python3
"""Send DingTalk notifications for PR babysitting fix events.

Supports two transports (auto-selected by env vars):
  1. Webhook robot (zero dependency, recommended) — set BABYSIT_PR_DINGTALK_WEBHOOK_TOKEN
  2. OpenClaw CLI (internal) — fallback when webhook token is not set
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

WEBHOOK_TOKEN_ENV = "BABYSIT_PR_DINGTALK_WEBHOOK_TOKEN"
WEBHOOK_SECRET_ENV = "BABYSIT_PR_DINGTALK_WEBHOOK_SECRET"
WEBHOOK_URL_BASE = "https://oapi.dingtalk.com/robot/send"

OPENCLAW_BIN_ENV = "BABYSIT_PR_DINGTALK_OPENCLAW_BIN"
OPENCLAW_CHANNEL_ENV = "BABYSIT_PR_DINGTALK_OPENCLAW_CHANNEL"
OPENCLAW_TARGET_ENV = "BABYSIT_PR_DINGTALK_OPENCLAW_TARGET"
DEFAULT_OPENCLAW_BIN = "openclaw"
DEFAULT_OPENCLAW_CHANNEL = "dingtalk-connector"
DEFAULT_OPENCLAW_TARGET = "079458"

TIMEOUT_SECONDS = 60


def build_message(title, text):
    return f"{title}\n\n{text}"


def mask_token(token):
    if not token or len(token) <= 6:
        return "***"
    return token[:3] + "..." + token[-3:]


# ---------------------------------------------------------------------------
# Webhook transport
# ---------------------------------------------------------------------------

def compute_sign(timestamp_ms, secret):
    string_to_sign = f"{timestamp_ms}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return urllib.parse.quote_plus(base64.b64encode(hmac_code).decode("utf-8"))


def default_http_poster(url, data, timeout):
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace") if err.fp else ""
        return err.code, body
    except urllib.error.URLError as err:
        return -1, str(err.reason)
    except Exception as err:
        return -1, str(err)


def send_webhook_notification(
    title,
    text,
    env,
    http_poster=default_http_poster,
    timeout_seconds=TIMEOUT_SECONDS,
):
    token = str(env.get(WEBHOOK_TOKEN_ENV, "")).strip()
    if not token:
        return {
            "status": "skipped",
            "transport": "webhook",
            "reason": "BABYSIT_PR_DINGTALK_WEBHOOK_TOKEN not set",
        }

    url = f"{WEBHOOK_URL_BASE}?access_token={token}"
    secret = str(env.get(WEBHOOK_SECRET_ENV, "")).strip()
    if secret:
        timestamp_ms = int(time.time() * 1000)
        sign = compute_sign(timestamp_ms, secret)
        url += f"&timestamp={timestamp_ms}&sign={sign}"

    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": build_message(title, text)},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    status_code, body = http_poster(url, data, timeout_seconds)

    parsed = None
    try:
        parsed = json.loads(body) if body else None
    except (json.JSONDecodeError, TypeError):
        pass

    errcode = parsed.get("errcode") if isinstance(parsed, dict) else None
    errmsg = parsed.get("errmsg", "") if isinstance(parsed, dict) else body

    if errcode == 0:
        return {"status": "sent", "transport": "webhook"}

    return {
        "status": "failed",
        "transport": "webhook",
        "http_status": status_code,
        "errcode": errcode,
        "errmsg": str(errmsg),
    }


# ---------------------------------------------------------------------------
# OpenClaw transport
# ---------------------------------------------------------------------------

def default_command_runner(cmd, timeout_seconds):
    try:
        proc = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=timeout_seconds,
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
        binary, "message", "send",
        "--channel", channel, "--target", target,
        "--message", build_message(title, text), "--json",
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


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def send_notification(
    title,
    text,
    env=os.environ,
    command_runner=default_command_runner,
    http_poster=default_http_poster,
    timeout_seconds=TIMEOUT_SECONDS,
):
    if env.get(WEBHOOK_TOKEN_ENV):
        return send_webhook_notification(
            title, text, env, http_poster=http_poster, timeout_seconds=timeout_seconds,
        )
    return send_openclaw_notification(
        title, text, env, command_runner=command_runner, timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def read_text(args):
    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as file:
            return file.read()
    if args.text:
        return args.text
    raise SystemExit("--text or --text-file is required")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Send a DingTalk notification for a babysit-pr event."
    )
    parser.add_argument("--title", required=True)
    parser.add_argument("--text")
    parser.add_argument("--text-file")
    parser.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    text = read_text(args)
    if args.dry_run:
        webhook_token = os.environ.get(WEBHOOK_TOKEN_ENV, "")
        if webhook_token:
            result = {
                "status": "dry_run",
                "transport": "webhook",
                "message": build_message(args.title, text),
                "webhook": {
                    "url_base": WEBHOOK_URL_BASE,
                    "token": mask_token(webhook_token),
                    "secret_set": bool(os.environ.get(WEBHOOK_SECRET_ENV, "").strip()),
                },
            }
        else:
            result = {
                "status": "dry_run",
                "transport": "openclaw",
                "message": build_message(args.title, text),
                "openclaw": {
                    "binary": os.environ.get(OPENCLAW_BIN_ENV, DEFAULT_OPENCLAW_BIN),
                    "channel": os.environ.get(OPENCLAW_CHANNEL_ENV, DEFAULT_OPENCLAW_CHANNEL),
                    "target": os.environ.get(OPENCLAW_TARGET_ENV, DEFAULT_OPENCLAW_TARGET),
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
