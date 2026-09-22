# Live backend console for Klasser.
#
# The API server and the pipeline worker run as detached processes, so their
# output goes to files rather than a window. This tails both into one console,
# tagged, so a generation can be watched as it happens.
#
# Run it again any time - it finds the current logs itself.

$ErrorActionPreference = 'Stop'

$taskDir = Join-Path $env:LOCALAPPDATA `
  'Temp\claude\c--Users-algielu-Downloads-New-folder\42ef4bd8-581b-42aa-86b1-0ce3ab2cfd95\tasks'

if (-not (Test-Path $taskDir)) {
  Write-Host "No log directory at $taskDir" -ForegroundColor Red
  Read-Host "Press Enter to close"
  exit 1
}

# Identify the streams by what they actually print, so this keeps working
# when the backend is restarted and the file names change.
$api = $null; $worker = $null
foreach ($f in Get-ChildItem $taskDir -Filter *.output |
                Sort-Object LastWriteTime -Descending) {
  $head = Get-Content $f.FullName -TotalCount 40 -ErrorAction SilentlyContinue
  if (-not $head) { continue }
  $text = $head -join "`n"
  # The API server loads the AI key pools too, so that line alone does not
  # tell the two apart - only the server prints "Started server process".
  if ($text -match 'Started server process') {
    if (-not $api) { $api = $f.FullName }
  }
  elseif ($text -match 'AI key pools loaded|klasser\.queue|klasser\.pipeline') {
    if (-not $worker) { $worker = $f.FullName }
  }
}

Write-Host ''
Write-Host '  Klasser - live backend' -ForegroundColor Cyan
Write-Host '  ----------------------' -ForegroundColor DarkGray
if ($worker) { Write-Host "  worker : $(Split-Path $worker -Leaf)" -ForegroundColor DarkGray }
if ($api)    { Write-Host "  api    : $(Split-Path $api -Leaf)"    -ForegroundColor DarkGray }
Write-Host '  Ctrl+C to stop. Closing this window does not stop the backend.' -ForegroundColor DarkGray
Write-Host ''

if (-not $worker -and -not $api) {
  Write-Host '  Neither process is running right now.' -ForegroundColor Yellow
  Read-Host '  Press Enter to close'
  exit 0
}

# Each tail runs as its own job; the loop below interleaves whatever arrives.
$jobs = @()
if ($worker) {
  $jobs += Start-Job -ArgumentList $worker -ScriptBlock {
    param($p) Get-Content $p -Tail 25 -Wait | ForEach-Object { "W|$_" } }
}
if ($api) {
  $jobs += Start-Job -ArgumentList $api -ScriptBlock {
    param($p) Get-Content $p -Tail 5 -Wait | ForEach-Object { "A|$_" } }
}

try {
  while ($true) {
    foreach ($j in $jobs) {
      foreach ($line in (Receive-Job $j -ErrorAction SilentlyContinue)) {
        if ($line -notmatch '^(W|A)\|') { continue }
        $tag  = $line.Substring(0, 1)
        $text = $line.Substring(2)

        if ($tag -eq 'A') {
          # Request noise - keep it dim and drop the poll chatter entirely.
          if ($text -match '/status|/queue|/attempts|/progress') { continue }
          Write-Host "  api    $text" -ForegroundColor DarkGray
          continue
        }

        $colour = switch -Regex ($text) {
          'ERROR|Traceback|failed|FAIL' { 'Red'    ; break }
          'WARNING|warn'                { 'Yellow' ; break }
          'layer\d|stage|Generating'    { 'Cyan'   ; break }
          'HTTP Request'                { 'DarkGray'; break }
          default                       { 'Gray' }
        }
        Write-Host "  $text" -ForegroundColor $colour
      }
    }
    Start-Sleep -Milliseconds 400
  }
}
finally {
  $jobs | Stop-Job -ErrorAction SilentlyContinue
  $jobs | Remove-Job -Force -ErrorAction SilentlyContinue
}
