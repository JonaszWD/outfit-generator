#!/bin/bash
# Block any git commit/push (and GitHub write ops) from Claude.
cmd=$(jq -r '.tool_input.command // empty')
# Allow flags/options between git and subcommand, e.g. "git -C x commit", "git -c k=v push"
if echo "$cmd" | grep -Eq '(^|[;&|`($[:space:]])(git([[:space:]]+-[^[:space:]]+([[:space:]]+[^-[:space:]][^[:space:]]*)?)*[[:space:]]+(commit|push|am|cherry-pick|revert|merge|rebase|tag|notes|send-pack)|gh[[:space:]]+(pr[[:space:]]+(create|merge|edit|comment|review|close)|release[[:space:]]+create|repo[[:space:]]+(create|delete|edit)|issue[[:space:]]+(create|comment)|api))([[:space:]]|$)'; then
  jq -n '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:"Blocked by project policy: Claude must never commit or push to git/GitHub. Tell the user to do it manually."}}'
fi
exit 0
