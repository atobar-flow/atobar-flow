#!/usr/bin/env bash
# atobar-flow (ATOBAR — Agentic Teams On BYOK And Repos) onboarding for one Client Repo.
# Run it inside a clone of the repository:
#   curl -fsSL https://cp.example.invalid/onboard.sh | bash
# Source and docs: https://github.com/atobar-flow/atobar-flow/blob/main/scripts/onboard.sh
# Guide:           https://cp.example.invalid/onboarding
#
# Environment overrides:
#   ATOBAR_CP_URL     Control Panel URL (composed from CP_PUBLIC_URL or https://$CP_HOSTNAME)
#   ATOBAR_APP_SLUG   GitHub App slug (default atobar-flow-agent)
#   ATOBAR_REPO       owner/name (default: detected with `gh repo view`)
#   ATOBAR_ROTATE=1   replace an existing ATOBAR_TENANT_TOKEN secret
#   ATOBAR_YES=1      answer "yes" to every confirmation (token must then come from ATOBAR_TOKEN_FILE)
#   ATOBAR_TOKEN_FILE read the tenant token from this file instead of prompting
set -euo pipefail

VERSION="0.3.0"
if [ -z "${ATOBAR_CP_URL:-}" ]; then
  if [ -n "${CP_PUBLIC_URL:-}" ]; then
    ATOBAR_CP_URL="$CP_PUBLIC_URL"
  elif [ -n "${CP_HOSTNAME:-}" ]; then
    ATOBAR_CP_URL="https://${CP_HOSTNAME#https://}"
  fi
fi
CP_URL="${ATOBAR_CP_URL:-https://cp.example.invalid}"
CP_URL="${CP_URL%/}"
APP_SLUG="${ATOBAR_APP_SLUG:-atobar-flow-agent}"
BRANCH="atobar/onboard"
TRIGGER=".github/workflows/flow-trigger.yml"
SECRET="ATOBAR_TENANT_TOKEN"
SPEC_LABEL="flow:onboard"
TTY="${ATOBAR_TTY:-/dev/tty}"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# stdin is the script itself under `curl | bash`, so prompts read from the terminal.
ask() {
  local prompt="$1" reply
  if [ "${ATOBAR_YES:-}" = 1 ]; then return 0; fi
  [ -r "$TTY" ] || die "no terminal to ask '$prompt'; set ATOBAR_YES=1 to run unattended"
  printf '    %s [y/N] ' "$prompt"
  read -r reply <"$TTY" || reply=""
  case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

open_url() {
  info "$1"
  if command -v open >/dev/null 2>&1; then open "$1" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 || true
  fi
}

preflight() {
  say "atobar-flow onboard $VERSION: checking prerequisites"
  local tool
  for tool in git gh curl; do
    command -v "$tool" >/dev/null 2>&1 || die "'$tool' is required (GitHub CLI: https://cli.github.com)"
  done
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "run this inside a clone of the repository to onboard"
  gh auth status >/dev/null 2>&1 || die "GitHub CLI is not signed in; run: gh auth login -s repo,workflow"

  REPO="${ATOBAR_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)}"
  [[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "could not tell which GitHub repository this clone is (got '$REPO')"
  info "repository: $REPO"

  [ "$(gh api "repos/$REPO" -q .permissions.admin 2>/dev/null || true)" = true ] \
    || die "you must be an admin of $REPO to onboard it"
  DEFAULT_BRANCH="$(gh api "repos/$REPO" -q .default_branch)"
  info "default branch: $DEFAULT_BRANCH"
  EMPTY=0
  if [ -z "$(git ls-remote --heads origin 2>/dev/null || true)" ]; then
    git rev-parse -q --verify HEAD >/dev/null &&
      die "$REPO has no commits on GitHub but this clone does; push them first (git push -u origin HEAD), then re-run"
    EMPTY=1
    info "$REPO is empty: onboarding makes its first commit and the spec issue describes what to build from zero"
  fi

  curl -fsS --max-time 15 "$CP_URL/healthz" >/dev/null || die "Control Panel $CP_URL is not reachable"
  info "Control Panel: $CP_URL"
}

stage_app() {
  say "Stage 1/5: GitHub App"
  info "The $APP_SLUG GitHub App must be installed on $REPO (choose 'Only select repositories')."
  if ask "Is the App already installed on $REPO?"; then return; fi
  open_url "https://github.com/apps/$APP_SLUG/installations/new"
  ask "Continue once the App is installed?" || die "stopped; re-run when the App is installed"
}

has_secret() {
  gh secret list -R "$REPO" --json name -q '.[].name' 2>/dev/null | grep -qx "$SECRET"
}

read_token() {
  if [ -n "${ATOBAR_TOKEN_FILE:-}" ]; then
    TOKEN="$(tr -d '[:space:]' <"$ATOBAR_TOKEN_FILE")"
    return
  fi
  [ -r "$TTY" ] || die "no terminal to read the token; set ATOBAR_TOKEN_FILE"
  printf '    Paste the tenant token (input hidden): '
  IFS= read -rs TOKEN <"$TTY" || TOKEN=""
  printf '\n'
}

stage_token() {
  say "Stage 2/5: register the repository and save its token"
  if has_secret && [ "${ATOBAR_ROTATE:-}" != 1 ]; then
    info "$SECRET is already set on $REPO; skipping (ATOBAR_ROTATE=1 replaces it)."
    return
  fi
  info "In the Control Panel: sign in, click 'Add to atobar-flow' for $REPO, open it and click 'Generate token'."
  open_url "$CP_URL/tenants/$REPO"
  local tenant_repo
  TOKEN=""
  read_token
  [[ "$TOKEN" == atb_* ]] || { TOKEN=""; die "that is not an atobar-flow tenant token (they start with atb_)"; }
  tenant_repo="$(curl -fsS --max-time 15 -H @- "$CP_URL/v1/tenant" <<<"Authorization: Bearer $TOKEN" \
    | sed -n 's/.*"repo":"\([^"]*\)".*/\1/p' || true)"
  if [ "$tenant_repo" != "$REPO" ]; then
    TOKEN=""
    die "the Control Panel did not accept this token for $REPO (it belongs to '${tenant_repo:-nothing}')"
  fi
  printf '%s' "$TOKEN" | gh secret set "$SECRET" -R "$REPO" >/dev/null
  TOKEN=""
  info "saved $SECRET to $REPO Actions secrets (the token was not printed or written to disk)"
}

stage_trigger() {
  say "Stage 3/5: trigger workflow"
  local tmp current
  tmp="$(mktemp)"
  curl -fsS --max-time 15 -G --data-urlencode "repo=$REPO" "$CP_URL/onboard/flow-trigger.yml" -o "$tmp" \
    || die "could not download the trigger workflow from $CP_URL"
  grep -q '^name: flow Trigger' "$tmp" || die "the Control Panel returned an unexpected trigger workflow"
  current="$(gh api "repos/$REPO/contents/$TRIGGER?ref=$DEFAULT_BRANCH" -H "Accept: application/vnd.github.raw" 2>/dev/null || true)"
  if [ "$current" = "$(cat "$tmp")" ]; then
    info "$TRIGGER is already up to date on $DEFAULT_BRANCH."
    TRIGGER_LIVE=1
    rm -f "$tmp"
    return
  fi
  TRIGGER_LIVE=0
  local existing
  existing="$(gh pr list -R "$REPO" --head "$BRANCH" --state open --json url -q '.[0].url' 2>/dev/null || true)"
  if [ -n "$existing" ]; then
    info "onboarding PR already open: $existing"
    rm -f "$tmp"
    return
  fi
  [ -z "$(git status --porcelain)" ] || die "the clone has uncommitted changes; commit or stash them and re-run"
  if [ "$EMPTY" = 1 ]; then
    first_commit "$tmp"
    return
  fi
  ask "Open a pull request in $REPO adding $TRIGGER?" || { info "skipped"; rm -f "$tmp"; return; }
  local start
  start="$(git rev-parse --abbrev-ref HEAD)"
  git fetch -q origin "$DEFAULT_BRANCH"
  git switch -q -C "$BRANCH" "origin/$DEFAULT_BRANCH"
  mkdir -p "$(dirname "$TRIGGER")"
  mv "$tmp" "$TRIGGER"
  git add "$TRIGGER"
  git commit -q -m "chore(atobar): add atobar-flow trigger workflow"
  git push -q -f origin "$BRANCH" || { git switch -q "$start"; die "push failed; you may need: gh auth refresh -s workflow"; }
  git switch -q "$start"
  gh pr create -R "$REPO" --base "$DEFAULT_BRANCH" --head "$BRANCH" \
    --title "chore(atobar): onboard to atobar-flow" \
    --body "Adds \`$TRIGGER\`, generated by atobar-flow. It forwards \`flow:*\` issue labels to $CP_URL using the \`$SECRET\` Actions secret. Opened by [atobar-flow onboarding]($CP_URL/onboarding)."
}

# An empty repository has no base branch for a pull request, so the trigger lands as its first commit.
first_commit() {
  ask "Push the first commit to $REPO ($DEFAULT_BRANCH) with a README and $TRIGGER?" || { info "skipped"; rm -f "$1"; return; }
  git checkout -q --orphan "$DEFAULT_BRANCH"
  mkdir -p "$(dirname "$TRIGGER")"
  mv "$1" "$TRIGGER"
  [ -e README.md ] || printf '# %s\n\nBuilt by [atobar-flow](%s/onboarding) from the evaluation spec issue.\n' \
    "${REPO#*/}" "$CP_URL" >README.md
  git add README.md "$TRIGGER"
  git commit -q -m "chore(atobar): initial commit with atobar-flow trigger workflow"
  git push -q -u origin "$DEFAULT_BRANCH" || die "push failed; you may need: gh auth refresh -s workflow"
  TRIGGER_LIVE=1
  info "pushed the first commit to $DEFAULT_BRANCH"
}

empty_note() {
  [ "${EMPTY:-0}" = 1 ] || return 0
  cat <<'EOF'

## Build from zero
<!-- This repository started empty. Describe what to build: language, layout, entry points and the first
     user-visible milestone. Metrics below must be measurable once that milestone exists. -->
EOF
}

spec_body() {
  cat <<EOF
This issue is the **evaluation spec** for onboarding \`$REPO\` to atobar-flow. Guide: $CP_URL/onboarding

atobar-flow maintainers turn it into the tenant manifest, which lives in atobar-flow (not in this repository), so agents can never weaken their own pass criteria. Edit this issue until it is complete.

## Business objective
<!-- One or two sentences: what outcome should every change move toward? -->
$(empty_note)

## Metrics
<!-- One row per metric. Measurement = a command run in CI whose last output line is a number. -->
| key | measurement | comparator | threshold | baseline_mode | min_samples | why it reflects the objective |
|---|---|---|---|---|---|---|
| example_metric | \`python .atobar/metrics/example.py\` | >= | 1.0 | absolute | 1 | |

## How it could be gamed
<!-- What would make the number look good without the outcome being true? -->

## Metrics runtime
<!-- Python version and setup commands CI needs before the measurements run. -->

## Arbitration
Starts in \`shadow\` mode. Graduating to \`recommend\` and \`auto_merge\` is a reviewed change in atobar-flow.
EOF
}

stage_spec() {
  say "Stage 4/5: evaluation spec issue"
  local existing
  existing="$(gh issue list -R "$REPO" --label "$SPEC_LABEL" --state open --json url -q '.[0].url' 2>/dev/null || true)"
  if [ -n "$existing" ]; then
    info "evaluation spec issue already open: $existing"
    return
  fi
  ask "Open the evaluation spec issue in $REPO?" || { info "skipped"; return; }
  gh label create "$SPEC_LABEL" -R "$REPO" --color 5319e7 --force \
    --description "atobar-flow onboarding: evaluation spec" >/dev/null
  spec_body | gh issue create -R "$REPO" --title "atobar-flow evaluation spec" --label "$SPEC_LABEL" --body-file -
}

summary() {
  say "Stage 5/5: what happens next"
  [ "${TRIGGER_LIVE:-0}" = 1 ] || info "1. Merge the onboarding pull request so flow:* labels reach atobar-flow."
  info "2. Fill in the evaluation spec issue. atobar-flow maintainers review it and define your metrics."
  info "3. Merge the flow-metrics pull request atobar-flow then opens in $REPO."
  info "4. Shadow calibration, then activation. Track every stage at $CP_URL/tenants/$REPO"
  info "Guide: $CP_URL/onboarding"
}

main() {
  preflight
  stage_app
  stage_token
  stage_trigger
  stage_spec
  summary
}

main "$@"
