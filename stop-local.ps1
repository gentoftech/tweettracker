# Stops local TweetTracker processes bound to ports 3000 and 8000 (and background worker)
$ports = @(3000, 8000)
foreach ($p in $ports) {
    $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        if ($c.OwningProcess -gt 0) {
            Write-Host "Stopping PID $($c.OwningProcess) on port $p ..." -ForegroundColor Yellow
            Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    }
}
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*tweettracker.worker.main*" } |
    ForEach-Object {
        Write-Host "Stopping Worker PID $($_.ProcessId) ..." -ForegroundColor Yellow
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Write-Host "TweetTracker stopped. Your data remains saved in backend\data\tweettracker.db." -ForegroundColor Green
