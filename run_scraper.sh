#!/bin/bash
# Runs on the VM via cron at 10:00 and 19:00 daily.
# 1. Scrape jobs from Bright Data
# 2. Score them against the resume with Gemini
# 3. Upload only the scored .csv.gz files to Google Drive
# 4. Upload the gzipped master CSV
#
# Guarded by flock so an overrunning morning run can't collide with the
# evening cron writing to the same master CSV.
set -e

# One-shot pause switch: if ~/pause_until exists and today (ET) is BEFORE the
# YYYY-MM-DD date it contains, skip the entire run — no scrape, no scoring, no
# API spend. Self-clears once that date arrives (the run on/after it proceeds
# normally), so a weekend/vacation pause needs no manual re-enable. To pause:
#   echo 2026-06-15 > ~/pause_until      # resumes on the 15th
# To cancel an active pause early:  rm ~/pause_until
if [ -f ~/pause_until ] && [[ "$(date +%F)" < "$(cat ~/pause_until)" ]]; then
    echo "$(date -Is) paused until $(cat ~/pause_until) — skipping run" >> ~/scraper.log
    exit 0
fi

LOCKFILE=/tmp/run_scraper.lock
exec 9>"$LOCKFILE"
if ! flock -n 9; then
    echo "$(date -Is) run_scraper.sh already running — skipping this fire" >> ~/scraper.log
    exit 0
fi

# Dead-man's switch: ping healthchecks.io on success, /fail on any error, so a
# silently dying pipeline (billing lapse, Vertex 403, Bright Data outage) sends
# an email instead of just rotting in the log. Create a free check at
# https://healthchecks.io (schedule: twice daily, grace ~2h) and paste its ping
# URL here. Empty = pings are skipped, everything else still works.
HEALTHCHECK_URL="${HEALTHCHECK_URL:-}"   # e.g. https://hc-ping.com/<uuid>
ping_hc() {
    if [ -n "$HEALTHCHECK_URL" ]; then
        curl -fsS -m 10 --retry 3 "${HEALTHCHECK_URL}$1" >/dev/null 2>&1 || true
    fi
}
on_exit() {
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "$(date -Is) run_scraper.sh FAILED (exit $status)" >> ~/scraper.log
        ping_hc /fail
    fi
}
trap on_exit EXIT

# Keep scraper.log bounded: when it passes ~5 MB, keep only the last 5000 lines.
LOG=~/scraper.log
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
    tail -n 5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

cd ~
source ~/venv/bin/activate

# Ensure both run-label dirs exist so the rclone copies below never fail with
# "directory not found" (only the current label's dir is created by scraper.py).
mkdir -p ~/morning ~/evening

# Vertex AI auth: project is read by score_jobs.py via os.environ.
# Cron runs with a bare environment, so these must be set here, not in .bashrc.
# Override by exporting GOOGLE_CLOUD_PROJECT before this script (or edit below).
export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-your-gcp-project-id}"
export GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-global}"

# 0. Pull the company blocklist the dashboard appends to (right-click → Block
#    company). Missing file is fine — scraper.py falls back to its built-ins.
rclone copyto gdrive:LinkedInJobs/company_blocklist.txt ~/company_blocklist.txt 2>/dev/null || true

# 1. Scrape — writes ~/morning/linkedin_jobs_<date>_morning.csv (or evening).
#    A zero-result run writes nothing and exits 0; scoring then just runs its
#    rescore pass and the master still uploads.
python ~/scraper.py >> ~/scraper.log 2>&1

# 2. Score — writes ~/<label>/linkedin_jobs_<date>_<label>_scored.csv.gz,
#    then retries master rows whose scoring previously failed.
python ~/score_jobs.py >> ~/scraper.log 2>&1

# 3. Sync — only the scored .csv.gz files
rclone copy ~/morning gdrive:LinkedInJobs/morning --include "*_scored.csv.gz" --update
rclone copy ~/evening gdrive:LinkedInJobs/evening --include "*_scored.csv.gz" --update

# 4. Master CSV upload — runs after every scrape (morning + evening)
gzip -c ~/linkedin_jobs_master.csv > /tmp/linkedin_jobs_master.csv.gz
rclone copyto /tmp/linkedin_jobs_master.csv.gz gdrive:LinkedInJobs/linkedin_jobs_master.csv.gz --update
rm /tmp/linkedin_jobs_master.csv.gz

# 5. Run-metrics upload — score_jobs.py appends one row per run; the dashboard's
#    Stats tab reads this file from the synced Drive folder.
if [ -f ~/run_stats.csv ]; then
    rclone copyto ~/run_stats.csv gdrive:LinkedInJobs/run_stats.csv --update
fi

ping_hc ""
