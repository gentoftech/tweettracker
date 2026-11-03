# Starts TweetTracker locally on Windows (API :8000 + Worker + Web :3000)
# Persistent SQLite database lives at: backend\data\tweettracker.db
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "Starting TweetTracker API on http://localhost:8000 ..." -ForegroundColor Cyan
Start-Process -FilePath "uv" -ArgumentList "run --directory `"$RepoRoot\backend`" uvicorn tweettracker.api.main:app --host 127.0.0.1 --port 8000" -WindowStyle Minimized

Write-Host "Starting TweetTracker Background Worker ..." -ForegroundColor Cyan
Start-Process -FilePath "uv" -ArgumentList "run --directory `"$RepoRoot\backend`" python -m tweettracker.worker.main" -WindowStyle Minimized

Write-Host "Starting TweetTracker Web UI on http://localhost:3000 ..." -ForegroundColor Cyan
Start-Process -FilePath "npm.cmd" -ArgumentList "--prefix `"$RepoRoot\web`" run dev -- -p 3000" -WindowStyle Minimized

Write-Host ""
Write-Host "TweetTracker is launching:" -ForegroundColor Green
Write-Host "  Web UI : http://localhost:3000"
Write-Host "  API    : http://localhost:8000/api/health"
Write-Host "  DB     : $RepoRoot\backend\data\tweettracker.db (saved automatically)"
