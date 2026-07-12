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

# One-shot pause switch: if ~/pause_until exists and now is BEFORE the value it
# contains, skip the entire run -- no scrape, no scoring, no API spend. The value
# is either a date (YYYY-MM-DD) or a date+time (YYYY-MM-DD HH:MM); the comparison
# uses matching granularity, and lexical string comparison is correct for both
# formats. Self-clears once that moment arrives (the run on/after it proceeds
# normally), so a weekend/vacation pause needs no manual re-enable. To pause:
#   echo 2026-06-15 > ~/pause_until            # resumes on the 15th
#   echo "2026-06-15 09:00" > ~/pause_until    # resumes 09:00 on the 15th
# To cancel an active pause early:  rm ~/pause_until
if [ -f ~/pause_until ]; then
    PAUSE_UNTIL="$(cat ~/pause_until)"
    if [[ "$PAUSE_UNTIL" == *" "* ]]; then
        NOW="$(date +"%F %H:%M")"
    else
        NOW="$(date +%F)"
    fi
    if [[ "$NOW" < "$PAUSE_UNTIL" ]]; then
        echo "$(date -Is) paused until $PAUSE_UNTIL - skipping run" >> ~/scraper.log
        exit 0
    fi
fi

LOCKFILE=/tmp/run_scraper.lock
exec 9>"$LOCKFILE"
if ! flock -n 9; then
    echo "$(date -Is) run_scraper.sh already running — skipping this fire" >> ~/scraper.log
    exit 0
fi

# Dead-man's switch: ping healthchecks.io /start when the run begins and
# /<exit-code> when it ends (0 = success, anything else = failure), so a
# silently dying pipeline (billing lapse, Vertex 403, Bright Data outage) sends
# an email instead of just rotting in the log. No-op when HEALTHCHECKS_URL is
# unset or empty -- pings are simply skipped and everything else still works.
# To enable: create a free check at https://healthchecks.io (schedule: twice
# daily, grace ~2h), then set the ping URL in the cron environment (add a
# HEALTHCHECKS_URL=https://hc-ping.com/<uuid> line at the top of the crontab)
# or paste it into the default below. A ping failure never breaks the scrape.
HEALTHCHECKS_URL="${HEALTHCHECKS_URL:-}"   # e.g. https://hc-ping.com/<uuid>
ping_hc() {
    if [ -n "$HEALTHCHECKS_URL" ]; then
        curl -fsS -m 10 --retry 3 "${HEALTHCHECKS_URL}$1" >/dev/null 2>&1 || true
    fi
}
on_exit() {
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "$(date -Is) run_scraper.sh FAILED (exit $status)" >> ~/scraper.log
    fi
    ping_hc "/$status"   # /0 on success, /<code> on failure; exit code unchanged
}
trap on_exit EXIT
ping_hc /start

# Keep scraper.log bounded: when it passes ~5 MB, keep only the last 5000 lines.
LOG=~/scraper.log
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
    tail -n 5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

cd ~
source ~/venv/bin/activate

# Ensure all run-label dirs exist so the rclone copies below never fail with
# "directory not found" (only the current label's dir is created by scraper.py).
mkdir -p ~/morning ~/afternoon ~/evening ~/night

# Vertex AI auth: project is read by score_jobs.py via os.environ.
# Cron runs with a bare environment, so these must be set here, not in .bashrc.
# Override by exporting GOOGLE_CLOUD_PROJECT before this script (or edit below).
export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-your-gcp-project-id}"
export GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-global}"

# 0. Pull the company blocklist the dashboard appends to (right-click → Block
#    company). Missing file is fine — scraper.py falls back to its built-ins.
rclone copyto gdrive:LinkedInJobs/company_blocklist.txt ~/company_blocklist.txt 2>/dev/null || true

# 0.5 Fold in rows pushed up by local machines (dashboard "Find new jobs" / manual
#     adds spool full master rows into ~/incoming; see merge_incoming.py). Bad files
#     quarantine to ~/incoming/bad and never wedge the run; the ONLY nonzero exit is
#     an existing-but-unreadable master, which must stop the run here (set -e),
#     BEFORE the scrape spends money against an exclude set rebuilt from nothing.
python ~/merge_incoming.py >> ~/scraper.log 2>&1

# 1. Scrape — writes ~/morning/linkedin_jobs_<date>_morning.csv (or evening).
#    A zero-result run writes nothing and exits 0; scoring then just runs its
#    rescore pass and the master still uploads.
python ~/scraper.py >> ~/scraper.log 2>&1

# 2. Score — writes ~/<label>/linkedin_jobs_<date>_<label>_scored.csv.gz,
#    then retries master rows whose scoring previously failed.
python ~/score_jobs.py >> ~/scraper.log 2>&1

# 2.5 Retention: blank descriptions older than the window; best-effort, never fails the run.
#     DEPLOY NOTE: prune_master.py must be scp'd to ~/ alongside scraper.py/score_jobs.py.
#     Because this line is best-effort, a MISSING ~/prune_master.py logs "non-fatal (exit 127)"
#     and silently prunes nothing (the master keeps growing) -- so after deploying this script,
#     confirm `ls ~/prune_master.py` on the VM and grep the log for a real prune line, not 127.
python ~/prune_master.py --master ~/linkedin_jobs_master.csv >> ~/scraper.log 2>&1 || echo "$(date -Is) prune_master non-fatal (exit $?)" >> ~/scraper.log

# 3. Sync — only the scored .csv.gz files (one per run-label dir)
for label in morning afternoon evening night; do
    rclone copy ~/"$label" gdrive:LinkedInJobs/"$label" --include "*_scored.csv.gz" --update
done

# 4. Master CSV upload — runs after every scrape (morning + evening)
gzip -c ~/linkedin_jobs_master.csv > /tmp/linkedin_jobs_master.csv.gz
rclone copyto /tmp/linkedin_jobs_master.csv.gz gdrive:LinkedInJobs/linkedin_jobs_master.csv.gz --update
rm /tmp/linkedin_jobs_master.csv.gz

# 5. Run-metrics upload — score_jobs.py appends one row per run; the dashboard's
#    Stats tab reads this file from the synced Drive folder.
if [ -f ~/run_stats.csv ]; then
    rclone copyto ~/run_stats.csv gdrive:LinkedInJobs/run_stats.csv --update
fi

# Success ping is sent by the on_exit trap above (/0 via ping_hc).
