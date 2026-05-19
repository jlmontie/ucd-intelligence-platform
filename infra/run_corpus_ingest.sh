#!/usr/bin/env bash
# UCD corpus ingest — full pipeline orchestrator.
#
# Designed to run inside tmux for overnight execution. Runs the
# 3-issue validation batch first, prompts for confirmation, then
# executes the full corpus ingest plus the post-ingest sweeps
# (resolve_firms, resolve_people, classify_firms, embed, geocode).
#
# Usage from repo root:
#   tmux new -s ingest
#   infra/run_corpus_ingest.sh                      # validation + full + sweeps
#   infra/run_corpus_ingest.sh --validation-only    # just the 3-issue check
#   infra/run_corpus_ingest.sh --full-only          # skip validation
#   infra/run_corpus_ingest.sh --no-images          # cheaper text-only mode
#   # Combine flags as needed: --full-only --no-images
#
# Prerequisites (script will preflight-check each):
#   - Cloud SQL Auth Proxy running on port 5433 (separate tmux pane)
#   - .env at repo root with DATABASE_URL, VERTEXAI_PROJECT, VERTEXAI_LOCATION
#   - gcloud auth application-default login completed
#   - Python venv at ~/environments/ucd-platform (override with UCD_PYTHON env)
#
# Optional but recommended:
#   - LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY in .env (cost tracing)
#   - OPENAI_API_KEY in .env (embeddings sweep)
#   - GOOGLE_MAPS_API_KEY in .env (geocoding sweep)

set -euo pipefail

# ── Repo root + venv ─────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV_PYTHON="${UCD_PYTHON:-$HOME/environments/ucd-platform/bin/python}"
LOG_DIR="/tmp/ucd-ingest-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$LOG_DIR"

# ── Argument parsing ─────────────────────────────────────────────────────────
VALIDATION_ONLY=false
FULL_ONLY=false
NO_IMAGES=""
REPROCESS=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --validation-only) VALIDATION_ONLY=true ;;
        --full-only)       FULL_ONLY=true ;;
        --no-images)       NO_IMAGES="--no-images" ;;
        --reprocess-all)   REPROCESS="--reprocess" ;;
        -h|--help)
            sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
            exit 0 ;;
        *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done

# ── Helpers ──────────────────────────────────────────────────────────────────
log() {
    local msg="[$(date +%H:%M:%S)] $*"
    echo "$msg" | tee -a "$LOG_DIR/script.log"
}

notify() {
    if command -v osascript &>/dev/null; then
        osascript -e "display notification \"$1\" with title \"UCD ingest\"" \
            2>/dev/null || true
    fi
}

fail() {
    log "FAIL: $*"
    notify "FAILED: $*"
    exit 1
}

# Notify on unexpected error (set -e tripping on any command).
trap 'notify "Script aborted on error"' ERR

# ── Preflight ────────────────────────────────────────────────────────────────
log "=== Preflight ==="
log "Repo:    $REPO_ROOT"
log "Python:  $VENV_PYTHON"
log "Logs:    $LOG_DIR"

[[ -f .env ]]            || fail ".env not found at repo root"
[[ -x "$VENV_PYTHON" ]]  || fail "venv python not executable at $VENV_PYTHON (override via UCD_PYTHON)"

# Load .env into the script's environment (`set -a` exports everything).
set -a
# shellcheck disable=SC1091
source .env
set +a

[[ -n "${DATABASE_URL:-}"      ]] || fail "DATABASE_URL not set"
[[ -n "${VERTEXAI_PROJECT:-}"  ]] || fail "VERTEXAI_PROJECT not set"
[[ -n "${VERTEXAI_LOCATION:-}" ]] || fail "VERTEXAI_LOCATION not set"

# DB reachable?
"$VENV_PYTHON" -c "from core.db import get_conn; c=get_conn(); c.close()" \
    2>"$LOG_DIR/preflight.log" \
    || fail "cannot connect to DB (is the Cloud SQL Auth Proxy running on port 5433?)"

# gcloud ADC valid? Vertex calls fail without it.
gcloud auth application-default print-access-token &>/dev/null \
    || fail "gcloud ADC not authenticated; run: gcloud auth application-default login"

# Optional services — warn but don't abort.
[[ -n "${LANGFUSE_PUBLIC_KEY:-}" ]] || log "WARN: LANGFUSE_PUBLIC_KEY unset → no cost tracing"
[[ -n "${OPENAI_API_KEY:-}"      ]] || log "WARN: OPENAI_API_KEY unset → embeddings sweep will be skipped"
[[ -n "${GOOGLE_MAPS_API_KEY:-}" ]] || log "WARN: GOOGLE_MAPS_API_KEY unset → geocoding sweep will be skipped"

ls issues/*.pdf >/dev/null 2>&1 || fail "no PDFs in issues/"
log "Issues directory: $(ls issues/*.pdf 2>/dev/null | wc -l | tr -d ' ') PDFs"
log "Mode: $([[ -n "$NO_IMAGES" ]] && echo 'text-only (--no-images)' || echo 'with images')"

# ── Validation batch ─────────────────────────────────────────────────────────
if ! $FULL_ONLY; then
    log ""
    log "=== Validation batch (--limit 3) ==="
    log "Expected: ~25 minutes, ~\$1 cost"
    if "$VENV_PYTHON" -m ingest_corpus.ingest \
            --issues_dir issues/ --limit 3 $NO_IMAGES \
            2>&1 | tee "$LOG_DIR/validation.log"; then
        log "Validation batch finished cleanly"
    else
        fail "validation batch errored — see $LOG_DIR/validation.log"
    fi
fi

if $VALIDATION_ONLY; then
    log "Validation-only mode; exiting before full run."
    notify "Validation batch done"
    exit 0
fi

# ── Confirmation gate (skipped in --full-only) ───────────────────────────────
if ! $FULL_ONLY; then
    echo
    echo "Validation results above. Confirm before kicking off the full corpus run."
    echo "  - Check Langfuse dashboard for unexpected costs"
    echo "  - Skim $LOG_DIR/validation.log for parse errors / warnings"
    echo "  - Check DB: project counts, quote counts look reasonable"
    echo
    read -r -p "Proceed with FULL corpus ingest? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || { log "Aborted at confirmation gate"; exit 1; }
fi

# ── Full corpus ingest ───────────────────────────────────────────────────────
log ""
log "=== Full corpus ingest ==="
log "Expected: ~14 hours sequential, ~\$30-35 with images"
START=$(date +%s)

if "$VENV_PYTHON" -m ingest_corpus.ingest \
        --issues_dir issues/ $NO_IMAGES $REPROCESS \
        2>&1 | tee "$LOG_DIR/full.log"; then
    ELAPSED=$(( $(date +%s) - START ))
    log "Full ingest finished in $((ELAPSED / 60)) min"
else
    fail "full corpus ingest errored — see $LOG_DIR/full.log; orphan-resume will heal partial issues on re-run"
fi

# ── Post-ingest sweeps ───────────────────────────────────────────────────────
log ""
log "=== Post-ingest sweeps ==="

run_sweep() {
    local name="$1"; shift
    log "  $name"
    if "$VENV_PYTHON" "$@" 2>&1 | tee "$LOG_DIR/${name}.log"; then
        log "    ok"
    else
        log "    FAILED — see $LOG_DIR/${name}.log (continuing; sweeps are idempotent and can re-run)"
    fi
}

run_sweep resolve_firms   -m core.resolution.resolve_firms
run_sweep resolve_people  -m core.resolution.resolve_people
run_sweep classify_firms  -m core.resolution.classify_firms

if [[ -n "${OPENAI_API_KEY:-}" ]]; then
    # Re-embed everything that draws on probe-derived content so the
    # new probe outputs flow into the vectors. Claim/quote/firm text
    # is rich-context now (see core/embeddings/embed.py); the --redo
    # flag forces re-embedding even where the column is non-NULL.
    run_sweep embed -m core.embeddings.embed \
        --tables projects articles claims quotes firms --redo
else
    log "  SKIP embed (OPENAI_API_KEY unset)"
fi

if [[ -n "${GOOGLE_MAPS_API_KEY:-}" ]]; then
    run_sweep geocode -m core.geocode.geocode
else
    log "  SKIP geocode (GOOGLE_MAPS_API_KEY unset)"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
log ""
log "=== Done ==="
log "All logs:   $LOG_DIR"
log "Total wall time: $((($(date +%s) - START) / 60)) min (excluding validation)"
notify "UCD corpus ingest complete"
