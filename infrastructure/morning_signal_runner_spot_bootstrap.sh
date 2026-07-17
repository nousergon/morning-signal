#!/usr/bin/env bash
# infrastructure/morning_signal_runner_spot_bootstrap.sh — provision an EC2-spot box,
# register it as an EPHEMERAL GitHub Actions self-hosted runner for
# nousergon/morning-signal, let it pick up exactly ONE queued job, then
# self-terminate. alpha-engine-config-I2572 (source design).
#
# SIBLING of ci_watch_spot_bootstrap.sh/sf_watch_spot_bootstrap.sh — mirrors
# their proven skeleton VERBATIM for the watchdog/deferred-shutdown mechanics
# (see ci_watch_spot_bootstrap.sh's header for the full config#1472 incident
# history behind that piece). DIFFERENT WORKLOAD, though: those two run a
# bespoke Claude Code agent over SSM outside the GHA Actions protocol; this
# one runs the actual `actions-runner` binary and lets GitHub's own Actions
# service dispatch the already-queued job to it — the workflow YAML (checkout,
# setup-python, pytest, etc.) is untouched, only `runs-on:` changes. See
# alpha-engine-config-I2572 (source design) for the full rationale (why self-hosted, why
# ephemeral-spot over a persistent box).
#
# Invoked by the `morning-signal-runner-dispatcher` Lambda over SSM. That
# Lambda is triggered by a GitHub `workflow_job` (action=queued) webhook —
# NOT by a thin GHA-hosted job like ci-watch/sf-watch's dispatch legs, since
# the whole point here is a workflow with ZERO GHA-hosted involvement. The
# Lambda's send-command prelude fetches the PAT, clones this repo, then
# `exec`s THIS script from the repo root — same shape as ci-watch/groom.
#
# Usage (from the repo root the SSM prelude cloned to):
#   infrastructure/morning_signal_runner_spot_bootstrap.sh --job-id <workflow_job.id>
#
# REQUIRED SSM: /morning-signal/runner/github_pat — fine-grained PAT,
#   owner=nousergon, repo=morning-signal, Administration: Read and write
#   (REQUIRED to mint a JIT runner config, config#2653 — same scope the old
#   registration-token mint needed; the `Actions` permission alone does NOT
#   cover this, a common gotcha) + Contents: read (checkout).
#   THIS IS A DEDICATED, NARROWLY-SCOPED PAT — deliberately NOT a reuse of
#   SATURDAY_SF_WATCH_PAT, because Administration:write on a repo is a much
#   more sensitive grant (it can register/remove runners and rotate repo
#   settings) than that PAT's existing Contents/PRs/Issues/Actions scopes;
#   keeping it separate keeps the blast radius of a leak contained to "can
#   register a runner", not "can also merge PRs across the whole org".
# REQUIRED AWS: instance profile morning-signal-runner-executor-profile
#   (infrastructure/iam/morning-signal-runner-executor-role-{trust,policy}.json) —
#   read-only access to the one SSM param above. Nothing else: every AWS
#   credential the actual CI steps need (S3 sync, OIDC role assumption, etc.)
#   continues to flow through GitHub's own per-workflow OIDC mechanism
#   (`aws-actions/configure-aws-credentials`), completely independent of this
#   instance's own IAM role — self-hosted runners support that unchanged.
set -uo pipefail

MORNING_SIGNAL_RUNNER_JOB_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --job-id) MORNING_SIGNAL_RUNNER_JOB_ID="$2"; shift 2 ;;
    *) shift ;;
  esac
done

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$REGION"
# A CI/validation job here is short (observed avg <1 min, longest ~5 min for
# the full pytest suite) — generous headroom over that, nowhere near
# ci-watch's 300+ min agent ceiling.
MAX_RUNTIME_SECONDS="${MAX_RUNTIME_SECONDS:-1800}"

log() { echo "[morning-signal-runner-bootstrap] $*"; }

# ── Non-root run user ──────────────────────────────────────────────────────────
# The actions-runner binary REFUSES to `config.sh`/`run.sh` as root unless
# RUNNER_ALLOW_RUNASROOT=1 is set — same shape as Claude Code's own
# --dangerously-skip-permissions root refusal that groom/ci-watch work around
# by dropping privileges instead of overriding the refusal.
MORNING_SIGNAL_RUNNER_USER="${MORNING_SIGNAL_RUNNER_USER:-ec2-user}"
RUN_USER_HOME="$(getent passwd "$MORNING_SIGNAL_RUNNER_USER" | cut -d: -f6)"
[ -n "$RUN_USER_HOME" ] || RUN_USER_HOME="/home/${MORNING_SIGNAL_RUNNER_USER}"
RUNNER_DIR="${RUN_USER_HOME}/actions-runner"

# ── Hard-timeout watchdog + guaranteed teardown ────────────────────────────────
# Copied VERBATIM from ci_watch_spot_bootstrap.sh — see its header for the
# full config#1472 postmortem this mechanism fixes (SSM's own status-report
# race with an immediate shutdown). Do not "simplify".
systemd-run --on-active="${MAX_RUNTIME_SECONDS}" --unit=morning-signal-runner-watchdog \
  --description='morning-signal-runner spot hard-timeout' /sbin/shutdown -h now \
  >/dev/null 2>&1 || log "WARN: watchdog arm failed (shutdown_behavior=terminate still applies)"
MORNING_SIGNAL_RUNNER_SHUTDOWN_DELAY_SECONDS="${MORNING_SIGNAL_RUNNER_SHUTDOWN_DELAY_SECONDS:-30}"
finish() {
  rc=$?
  log "exit rc=$rc — scheduling shutdown ${MORNING_SIGNAL_RUNNER_SHUTDOWN_DELAY_SECONDS}s from now (non-blocking — this script exits immediately so the SSM agent can report the command's real status before the deferred shutdown fires, config#1472)"
  systemd-run --on-active="${MORNING_SIGNAL_RUNNER_SHUTDOWN_DELAY_SECONDS}" --unit=morning-signal-runner-delayed-shutdown \
    --description='morning-signal-runner delayed self-terminate (post-SSM-report)' /sbin/shutdown -h now \
    >/dev/null 2>&1 || {
      log "WARN: delayed-shutdown scheduling failed — falling back to immediate shutdown"
      shutdown -h now >/dev/null 2>&1 || true
    }
  exit "$rc"
}
trap finish EXIT

git config --global --add safe.directory '*' >/dev/null 2>&1 || true

# ── Runtime: git + python3.12 + jq + gh (curl/tar/aws CLI ship on AL2023) ─────
# Pre-installed for speed/reliability, mirroring ci_watch's pattern — actual
# per-job tool provisioning (actions/setup-python, etc.) still works exactly
# as it would on a GitHub-hosted runner; this just avoids first-run download
# latency for the tools this repo's target workflows need. gh CLI added
# after the first real PR-triggered test run (alpha-engine-config#2647 (source incident))
# caught scripts/test_groom_driver.py invoking the real `gh` binary
# (FileNotFoundError) — GitHub-hosted runners ship it preinstalled, this
# box didn't.
log "installing runtime (git, python3.12, jq, gh)..."
dnf install -y -q git python3.12 python3.12-pip jq >/dev/null 2>&1 \
  || log "WARN: runtime install failed — continuing, actions/setup-python may cover python"
if ! command -v gh >/dev/null 2>&1; then
  dnf install -y -q 'dnf-command(config-manager)' >/dev/null 2>&1 || true
  dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo >/dev/null 2>&1 || true
  dnf install -y -q gh >/dev/null 2>&1 || log "WARN: gh install failed"
fi
log "runtime: git=$(git --version 2>/dev/null) python3.12=$(python3.12 --version 2>/dev/null) jq=$(jq --version 2>/dev/null) gh=$(gh --version 2>/dev/null | head -1) aws=$(aws --version 2>&1 | head -1)"

# ── Secrets from SSM (instance profile: morning-signal-runner-executor-role,
# see infrastructure/iam/morning-signal-runner-executor-role-{trust,policy}.json — a
# DEDICATED role scoped to exactly one param; see this script's header for why
# it is not a reuse of SATURDAY_SF_WATCH_PAT) ─────────────────────────────────
GH_TOKEN="$(aws ssm get-parameter --name /morning-signal/runner/github_pat --with-decryption \
  --query 'Parameter.Value' --output text --region "$REGION" 2>/dev/null)"
if [ -z "${GH_TOKEN:-}" ]; then
  log "FATAL: GH_TOKEN missing from SSM (/morning-signal/runner/github_pat)"
  exit 1
fi

# ── Runner identity (computed BEFORE minting — JIT config binds `name` at
# mint time, unlike the old registration-token flow where `--name` was only
# supplied later to config.sh) ─────────────────────────────────────────────────
IMDS_TOKEN="$(curl -sS -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 21600' http://169.254.169.254/latest/api/token 2>/dev/null)"
INSTANCE_ID="$(curl -sS -H "X-aws-ec2-metadata-token: ${IMDS_TOKEN}" http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null)"
RUNNER_NAME="config-spot-${INSTANCE_ID:-$(date +%s)}"

# ── Mint a just-in-time (JIT) runner config (config#2653 — replaces the old
# registration-token + config.sh two-round-trip handshake) ────────────────────
# Requires the SAME GH_TOKEN Administration:write permission the old
# registration-token mint needed — see this script's header; no new PAT scope.
#
# NOTE ON WHAT THIS DOES AND DOES NOT FIX (config#2653 comment, verified
# against GitHub's REST docs before implementing): `generate-jitconfig` takes
# only `name`/`runner_group_id`/`labels`/`work_folder` — there is NO job-id
# parameter, so this call does not (and per the public API cannot) bind the
# resulting runner to the SPECIFIC queued job that caused its own dispatch.
# Job-to-runner assignment for matching labels is still GitHub Actions'
# ordinary scheduler, unchanged by JIT vs. registration-token. What JIT DOES
# fix: the OLD flow was a full extra network round-trip (mint token, THEN
# config.sh does its own separate registration handshake) during which this
# runner was registered-but-not-yet-ready, widening the window another
# concurrently-registering runner could grab this job's slot first. JIT
# collapses that into one mint call with no separate handshake, shrinking
# (not eliminating) the race window. The failure mode where a queued job is
# never serviced at all (GitHub sends its `queued` webhook exactly once, so
# a dropped/misassigned job has no automatic retry) is NOT closed by this
# change — that needs a reconciliation/orphan-sweep on the dispatcher side,
# filed separately (do not conflate the two: this script only ever affects
# ITS OWN registration, it cannot detect or recover an orphaned job).
JIT_CONFIG="$(curl -sS -X POST \
  -H "Authorization: Bearer ${GH_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/nousergon/morning-signal/actions/runners/generate-jitconfig" \
  -d "$(jq -n --arg name "$RUNNER_NAME" \
    '{name: $name, runner_group_id: 1, labels: ["self-hosted","morning-signal-spot"], work_folder: "_work"}')" \
  | jq -r '.encoded_jit_config // empty')"
if [ -z "${JIT_CONFIG:-}" ]; then
  log "FATAL: could not mint a JIT runner config — check GH_TOKEN's Administration:write scope on nousergon/morning-signal"
  exit 1
fi

# ── Install + configure the actions-runner (ephemeral: auto-deregisters and
# run.sh exits after exactly one job) ─────────────────────────────────────────
# DYNAMIC version resolution (config-I2696, 2026-07-15 incident): GitHub
# deprecated the previously-hardcoded v2.321.0 and every box was refused job
# delivery — a hardcoded runner pin is the same silent-drift bug class as
# the Dockerfile duplicate-pin outage (2026-07-11). Resolve the CURRENT
# release at bootstrap (the runner service requires near-latest anyway —
# GitHub auto-deprecates old versions); the static pin below is the
# FALLBACK ONLY for when the releases API itself is unreachable, and the
# tarball sha256 is extracted from the release body (GitHub publishes
# per-asset checksums there) so integrity verification survives the
# dynamic bump. sha check is skipped only on the dynamic path when the
# release body parse fails (logged loudly) — never on the fallback pin.
RUNNER_VERSION_FALLBACK="2.321.0"
RUNNER_SHA256_FALLBACK="ba46ba7ce3a4d7236b16fbe44419fb453bc08f866b24f04d549ec89f1722a29e"

RELEASE_JSON="$(curl -sS --max-time 30 -H "Authorization: Bearer ${GH_TOKEN}" \
  https://api.github.com/repos/actions/runner/releases/latest 2>/dev/null || true)"
RUNNER_VERSION="$(printf '%s' "$RELEASE_JSON" | python3.12 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("tag_name", "").lstrip("v"))
except Exception:
    pass
' 2>/dev/null || true)"
if [ -n "$RUNNER_VERSION" ]; then
  RUNNER_SHA256="$(printf '%s' "$RELEASE_JSON" | python3.12 -c '
import json, re, sys
try:
    body = json.load(sys.stdin).get("body", "")
    m = re.search(r"actions-runner-linux-x64-[0-9.]+\.tar\.gz\W+<!-- BEGIN SHA linux-x64 -->([0-9a-f]{64})", body)
    m = m or re.search(r"<!-- BEGIN SHA linux-x64 -->([0-9a-f]{64})<!-- END SHA linux-x64 -->", body)
    print(m.group(1) if m else "")
except Exception:
    pass
' 2>/dev/null || true)"
  log "resolved actions-runner v${RUNNER_VERSION} dynamically (sha256: ${RUNNER_SHA256:-UNAVAILABLE from release body})"
else
  RUNNER_VERSION="$RUNNER_VERSION_FALLBACK"
  RUNNER_SHA256="$RUNNER_SHA256_FALLBACK"
  log "WARN: releases API unreachable — falling back to pinned actions-runner v${RUNNER_VERSION} (may be deprecated; if the runner is refused jobs, this box will now FAIL LOUD instead of exiting 0 — see the no-job guard below)"
fi
RUNNER_TARBALL="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"

mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR" || { log "FATAL: could not cd to ${RUNNER_DIR}"; exit 1; }
curl -sSLo "$RUNNER_TARBALL" \
  "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_TARBALL}" \
  || { log "FATAL: runner tarball download failed"; exit 1; }
if [ -n "$RUNNER_SHA256" ]; then
  echo "${RUNNER_SHA256}  ${RUNNER_TARBALL}" | sha256sum -c - \
    || { log "FATAL: runner tarball checksum mismatch — refusing to run unverified code"; exit 1; }
else
  log "WARN: no sha256 extracted from release body — proceeding on TLS trust alone (dynamic path only; logged for audit)"
fi
tar xzf "$RUNNER_TARBALL"

# actions-runner bundles a self-contained .NET 6 runtime that needs libicu —
# NOT present on Amazon Linux 2023's base AMI (confirmed live, 2026-07-15
# first smoke test: config.sh failed with "Libicu's dependencies is missing
# for Dotnet Core 6.0"). The runner's own bin/installdependencies.sh does
# NOT work here either (confirmed live, second smoke test): its OS-detection
# only recognizes specific `ID=` values from /etc/os-release and doesn't
# know "amzn" (AL2023) — a known upstream gap (actions/runner has no
# official AL2023 support), so it aborts with "Can't detect current OS
# type" before installing anything. krb5-libs/openssl-libs/zlib (the
# runtime's other deps) are already present on this AMI by default
# (confirmed via a read-only rpm -q against a live fleet AL2023 box) —
# libicu is the ONLY gap, so install it directly via dnf, bypassing the
# broken auto-detect entirely. Must run as root (before the chown/
# privilege-drop below) — dnf needs it.
dnf install -y -q libicu >/dev/null 2>&1 \
  || { log "FATAL: dnf install libicu failed"; exit 1; }

# actions/setup-python@v5 (used by nearly every one of this repo's target
# workflows) downloads a PREBUILT python archive from actions/python-versions
# — that manifest only ships builds for a small set of named distros
# (ubuntu-*, etc.), NOT Amazon Linux 2023, so on a fresh box it fails
# outright with "The version '3.12' ... was not found for this operating
# system" (confirmed live, first real PR-triggered CI run post-migration —
# alpha-engine-config#2647 (source incident) hit this on 11 of 12 concurrent checks). Rather
# than edit every workflow's steps (would break the "runs-on is the only
# diff" migration property, and any future workflow using setup-python would
# hit the same wall again), pre-populate the runner's OWN tool cache with
# the dnf-installed python3.12 so setup-python's "already cached, skip
# download" path satisfies the request — this is the standard fix for
# self-hosted runners on non-Ubuntu Linux. Symlinks (not a copy) into the
# real /usr install: CPython resolves sys.prefix from the RESOLVED path of
# its executable, so a symlinked bin/python3.12 correctly finds the real
# /usr/lib64/python3.12 stdlib without duplicating it.
PY_FULL_VERSION="$(python3.12 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
TOOL_CACHE_DIR="${RUNNER_DIR}/_work/_tool"
PY_CACHE_DIR="${TOOL_CACHE_DIR}/Python/${PY_FULL_VERSION}/x64"
mkdir -p "${PY_CACHE_DIR}/bin"
for bin in python3.12 python3 pip3.12 pip3; do
  src="/usr/bin/${bin}"
  [ -e "$src" ] && ln -sf "$src" "${PY_CACHE_DIR}/bin/${bin}"
done
ln -sf python3.12 "${PY_CACHE_DIR}/bin/python"
[ -e "${PY_CACHE_DIR}/bin/pip3.12" ] && ln -sf pip3.12 "${PY_CACHE_DIR}/bin/pip"
touch "${TOOL_CACHE_DIR}/Python/${PY_FULL_VERSION}/x64.complete"
log "pre-populated tool cache for python ${PY_FULL_VERSION} at ${PY_CACHE_DIR}"

chown -R "${MORNING_SIGNAL_RUNNER_USER}:${MORNING_SIGNAL_RUNNER_USER}" "$RUNNER_DIR"

log "starting run.sh with JIT config (config#2653) — name=${RUNNER_NAME} job_id=${MORNING_SIGNAL_RUNNER_JOB_ID:-n/a}; no separate config.sh step (--jitconfig writes the settings files directly and implies --ephemeral)"
# pip installing a package as a non-root user with no write access to the
# system site-packages silently falls back to a --user install, landing
# console scripts (pytest, etc.) in ~/.local/bin — NOT on PATH by default
# (confirmed live: `pip install pytest` succeeded, then `pytest: command
# not found`). Job steps inherit run.sh's baseline PATH (layered with
# whatever setup-python etc. append via GITHUB_PATH later), so this must be
# set here, not per-step.
runuser -u "$MORNING_SIGNAL_RUNNER_USER" -- /usr/bin/env HOME="$RUN_USER_HOME" \
  RUNNER_TOOL_CACHE="$TOOL_CACHE_DIR" AGENT_TOOLSDIRECTORY="$TOOL_CACHE_DIR" \
  PATH="${RUN_USER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  "${RUNNER_DIR}/run.sh" --jitconfig "$JIT_CONFIG"
RUNNER_EXIT=$?

log "run.sh exited rc=${RUNNER_EXIT}"

# Fail LOUD on listener-exit-without-job (config-I2696, 2026-07-15 incident):
# a runner refused by GitHub (e.g. "Runner version vX.Y.Z is deprecated and
# cannot receive messages") exits rc=0 — SSM reported Success while every
# job stayed queued, masking a fleet-wide CI outage for 3 hours. A run.sh
# exit without having executed a job is a FAILURE for an ephemeral one-job
# box, whatever rc it reports: the runner's Worker diag log is created only
# when a job is actually picked up, and Runner_*.log carries "Running job:"
# on delivery — absence of both ⇒ no job ran.
if ! ls "${RUNNER_DIR}/_diag"/Worker_*.log >/dev/null 2>&1 \
   && ! grep -qs "Running job:" "${RUNNER_DIR}/_diag"/Runner_*.log; then
  log "FATAL: run.sh exited (rc=${RUNNER_EXIT}) WITHOUT executing a job — runner was refused/never delivered work (deprecated version? label mismatch?). Reporting failure so the dispatcher's failure paths engage instead of silent Success."
  tail -20 "${RUNNER_DIR}/_diag"/Runner_*.log 2>/dev/null || true
  exit 1
fi
exit "$RUNNER_EXIT"
# trap finish() shuts the box down with the runner process's exit code. No
# separate `config.sh remove` call needed — --ephemeral already deregistered
# the runner from GitHub's side the moment its one job completed.
