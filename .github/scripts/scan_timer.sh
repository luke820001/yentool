#!/usr/bin/env bash
# On-time trigger for daily-scan, running entirely inside GitHub (no PC, no
# third-party scheduler).
#
# Why this exists: GitHub's `schedule` event fires a median ~2h and up to 12.5h
# late (measured in the schedule doc under docs/), so scan.yml's own cron can never be on time. But
# a workflow_dispatch starts the moment it is sent (measured 2026-09-14:
# created_at == the second `gh workflow run` returned), and a job's `sleep` is
# exact. So an EARLY, late-arriving cron only has to land somewhere before the
# target; this script then sleeps until the target and dispatches the scan.
#
# A job may run at most 6h. A wait longer than that is bridged by re-dispatching
# this workflow (GITHUB_TOKEN may trigger workflow_dispatch, unlike most events).
#
# Done means the PUBLISHED phone data is today's session and not degraded -- not
# "a run finished". A run can finish green with nothing new: exit 1 when the feed
# is unreachable, or a degraded OTC snapshot before TPEX has published.
#
# Env: GH_TOKEN, GH_REPO (gh), PAGES_URL, TZ=Asia/Taipei.
# Test hooks: NOW_EPOCH fakes the clock, DRY_RUN=1 prints the plan and exits.
set -euo pipefail

FIRST_ATTEMPT="15:00"     # TPEX margin; see the OTC-snapshot guard in scan_headless
LAST_ATTEMPT="19:30"      # after this, scan.yml's own late cron is the fallback
RETRY_MIN=75              # 15:00, 16:15, 17:30, 18:45 -> at most 4 scans on a holiday
JOB_BUDGET_MIN=340        # hop before the 360-minute job limit
START=$(date +%s)

now() { echo "${NOW_EPOCH:-$(date +%s)}"; }
today() { date -d "@$(now)" +%F; }
at_today() { date -d "$(today) $1" +%s; }
log() { echo "[timer $(date -d "@$(now)" '+%F %T %Z')] $*"; }

# Sleep, but hand over to a fresh run first if the sleep would outlive the job.
wait_until() {
  local target=$1 secs
  secs=$(( target - $(now) ))
  (( secs <= 0 )) && return 0
  if (( $(date +%s) - START + secs > JOB_BUDGET_MIN * 60 )); then
    local chunk=$(( JOB_BUDGET_MIN * 60 - ($(date +%s) - START) - 120 ))
    (( chunk > 0 )) && { log "sleeping ${chunk}s, then handing over"; sleep "$chunk"; }
    log "re-dispatching scan-timer to continue the wait"
    gh workflow run scan-timer.yml --ref main
    exit 0
  fi
  log "sleeping ${secs}s until $(date -d "@$target" +%T)"
  sleep "$secs"
}

published_today() {
  local body date degraded
  body=$(curl -fsS --max-time 30 "${PAGES_URL}/scan_result.json?t=$(date +%s%N)") || return 1
  date=$(jq -r '.meta.data_date // "" | .[0:10]' <<<"$body")
  degraded=$(jq -r '.meta.degraded // ""' <<<"$body")
  log "published data_date=${date:-?} degraded=${degraded:-none}"
  [[ "$date" == "$(today)" && -z "$degraded" ]]
}

dispatch_and_wait() {
  local since id i
  since=$(date -u +%s)
  gh workflow run scan.yml --ref main
  for i in $(seq 1 24); do
    sleep 5
    id=$(gh run list --workflow scan.yml --event workflow_dispatch --limit 5 \
          --json databaseId,createdAt \
          --jq "map(select((.createdAt | fromdateiso8601) >= $since - 30)) | .[0].databaseId // empty")
    [[ -n "$id" ]] && break
  done
  if [[ -z "${id:-}" ]]; then log "dispatched run never appeared"; return 0; fi
  log "watching scan run $id"
  gh run watch "$id" --interval 30 >/dev/null 2>&1 || true
  log "scan run $id: $(gh run view "$id" --json conclusion --jq .conclusion)"
  sleep 20   # Pages deploy is inside the run; give the CDN a moment
}

dow=$(date -d "@$(now)" +%u)
if (( dow > 5 )); then log "weekend, nothing to do"; exit 0; fi

first=$(at_today "$FIRST_ATTEMPT")
last=$(at_today "$LAST_ATTEMPT")
if [[ "${DRY_RUN:-}" == 1 ]]; then
  log "plan: first=$(date -d "@$first" +%T) last=$(date -d "@$last" +%T) wait=$(( first - $(now) ))s"
  exit 0
fi

if published_today; then log "today's data already published"; exit 0; fi

wait_until "$first"
attempts=0
while :; do
  if published_today; then log "done"; exit 0; fi
  attempts=$(( attempts + 1 ))
  log "attempt $attempts"
  dispatch_and_wait
  if published_today; then log "done"; exit 0; fi
  next=$(( $(now) + RETRY_MIN * 60 ))
  if (( next > last )); then log "next retry would pass $LAST_ATTEMPT, stopping"; exit 0; fi
  wait_until "$next"
done
