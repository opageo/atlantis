#!/usr/bin/env python3
"""PreToolUse hook: deny a small set of destructive actions.

Runs on every PreToolUse event for every agent in this workspace,
regardless of which agent (or prompt) triggered them. This is deterministic
enforcement, not a prompt — hook matchers are not applied by VS Code, so
filtering happens here instead.

Denies:
  - `git push --force` / `git push -f` (force pushes)
  - `rm -rf` / `rm -fr` / `rm --recursive --force` (recursive force delete)
  - `git reset --hard`
  - commands that mention both "deploy" and "prod"/"production" (heuristic
    placeholder for prod-deploy commands — replace with your real deploy
    script/command names once you have them)
  - edits to `.env` / `.env.*` files (except `.env.example`, which is treated
    as safe documentation)

This is a best-effort deny-list, not a formally verified command parser. It
is one layer of defense, not a substitute for real access controls, backups,
or code review.
"""

import json
import re
import sys

COMMAND_TOOLS = {"run_in_terminal", "send_to_terminal"}
EDIT_TOOLS = {"create_file", "replace_string_in_file", "multi_replace_string_in_file"}

FORCE_PUSH_RE = re.compile(r"git\s+push\b.*(--force\b|(?<!\S)-f\b)", re.IGNORECASE)
RESET_HARD_RE = re.compile(r"git\s+reset\b.*--hard\b", re.IGNORECASE)
RM_FLAGS_RE = re.compile(r"\brm\s+(-[a-zA-Z]+)")
ENV_PATH_RE = re.compile(r"(^|/)\.env(\.[^/]*)?$")
ENV_EXAMPLE_RE = re.compile(r"\.env\.example$")


def deny(reason: str) -> None:
    """Emit a deny decision and exit cleanly."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def check_command(command: str) -> None:
    """Check command against deny-listed patterns and deny if matched."""
    if not command:
        return
    lowered = command.lower()

    if FORCE_PUSH_RE.search(command):
        deny("Blocked by workspace policy: force push (git push --force/-f) is denied.")

    rm_match = RM_FLAGS_RE.search(lowered)
    if rm_match and "r" in rm_match.group(1) and "f" in rm_match.group(1):
        deny("Blocked by workspace policy: `rm` with combined recursive+force flags is denied.")
    if "rm " in lowered and "--recursive" in lowered and "--force" in lowered:
        deny("Blocked by workspace policy: `rm --recursive --force` is denied.")

    if RESET_HARD_RE.search(command):
        deny("Blocked by workspace policy: `git reset --hard` is denied.")

    if "deploy" in lowered and ("prod" in lowered or "production" in lowered):
        deny("Blocked by workspace policy: commands that look like a production deploy are denied.")


def check_edit_path(path: str) -> None:
    """Deny edits to protected paths like .env files."""
    if not path or ENV_EXAMPLE_RE.search(path):
        return
    if ENV_PATH_RE.search(path):
        deny(f"Blocked by workspace policy: editing protected env file '{path}' is denied.")


def main() -> None:
    """Parse PreToolUse payload and dispatch to checkers."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool_name in COMMAND_TOOLS:
        check_command(str(tool_input.get("command", "")))

    if tool_name in EDIT_TOOLS:
        check_edit_path(str(tool_input.get("filePath", "")))
        for replacement in tool_input.get("replacements") or []:
            if isinstance(replacement, dict):
                check_edit_path(str(replacement.get("filePath", "")))

    sys.exit(0)


if __name__ == "__main__":
    main()
