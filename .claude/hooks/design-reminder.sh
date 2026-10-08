#!/bin/bash
# Inject DESIGN.md reminder before any frontend file write/edit.
f=$(jq -r '.tool_input.file_path // empty')
case "$f" in
  *.html|*.htm|*.css|*.scss|*.sass|*.less|*.js|*.jsx|*.ts|*.tsx|*.vue|*.svelte|*.svg|*.jinja|*.jinja2|*.j2)
    jq -n '{hookSpecificOutput:{hookEventName:"PreToolUse",additionalContext:"DESIGN RULE: this is a UI file. Follow /Users/jojo/Projects/OutfitGenerator/DESIGN.md strictly: only its tokens (colors, type, spacing, radii), its component specs, and obey every Dont. Re-read the relevant DESIGN.md section first. No invented values. Missing token => ask user."}}'
    ;;
esac
exit 0
