#!/usr/bin/env bash
# Render the daily research briefing to MP4 for the dashboard's Briefing page.
#
# OFFLINE and OUT-OF-BAND from the trading path: it only reads
# decisions/decisions.json and writes web/static/media/. Runs in the dedicated
# `alpaca-media` conda env (edge-tts + torch/diffusers + node/ffmpeg/hyperframes),
# never the trading .venv.
#
# Usage:
#   ./briefing/run_briefing.sh                 # all defaults (SDXL backgrounds)
#   ./briefing/run_briefing.sh --no-images     # skip the GPU, gradient backgrounds
#   ./briefing/run_briefing.sh --symbols NVDA,AMZN
#
# Cron (after the research run — e.g. a few minutes past run_research.sh):
#   20 13 * * 1-5  cd <repo> && ./briefing/run_briefing.sh >> briefing/briefing.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_NAME="${ALPACA_MEDIA_ENV:-alpaca-media}"
# Resolve the conda base robustly — under cron's non-login shell `conda` is not on
# PATH, so `$(conda info --base)` is empty and sourcing fails. Fall back to the
# usual install locations.
CONDA_BASE="${CONDA_BASE:-}"
if [[ -z "$CONDA_BASE" ]]; then
  if command -v conda >/dev/null 2>&1; then
    CONDA_BASE="$(conda info --base)"
  else
    for c in "$HOME/anaconda3" "$HOME/miniconda3" /opt/conda; do
      [[ -f "$c/etc/profile.d/conda.sh" ]] && CONDA_BASE="$c" && break
    done
  fi
fi
# shellcheck disable=SC1090
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "$ENV_NAME"

echo "[$(date -u +%FT%TZ)] rendering briefing (env=$ENV_NAME)"
# -u: block-buffered stdout has hidden two crashes and stalled a monitor
python -u -m briefing.generate "$@"

# Optional YouTube publish of whatever was just archived. Opt-in per render tier
# via BRIEFING_PUBLISH=1: run_research_postmarket.sh sets it for the weekday
# post-market host recap and run_research_weekend.sh for the weekend H3 recap
# (both KIND=video), so a bare `run_briefing.sh` — an ad-hoc or test render —
# still never posts. --latest resolves the date+slot from the
# archive, so no date arithmetic here and no UTC/local rollover bug.
#
# KIND matters beyond bookkeeping: it selects the recap's own hashtag set and it is
# what arms the pre-upload anatomy QC, which is skipped for the typographic
# slideshows and blocks the upload on a limb defect for the recap.
#
# Deliberately NON-FATAL: a failed upload (expired token, YouTube 5xx, quota)
# must not fail the render or mark the cron job as failed — the video is already
# rendered and archived, and the publish is safely retryable by hand because the
# video id is recorded in meta.json.
if [[ "${BRIEFING_PUBLISH:-0}" == "1" ]]; then
  echo "[$(date -u +%FT%TZ)] publishing to YouTube"
  if ! python -m briefing.publish_youtube publish \
        --latest --kind "${BRIEFING_PUBLISH_KIND:-slideshow}" --lang en; then
    echo "[$(date -u +%FT%TZ)] WARNING: YouTube publish failed (render is fine; retry with --latest)" >&2
  fi
fi
echo "[$(date -u +%FT%TZ)] done"
