# Tillegal Judge helper: no API key; explicit browser sign-in and explicit live action.
param(
    [ValidateSet('Login','Status','Models','Live','Tests','Logout')][string]$Mode = 'Status',
    [string]$Model,
    [string]$Context = 'examples/judge/context.json',
    [string]$PythonPath
)
$ErrorActionPreference = 'Stop'
$judgeRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $judgeRoot
if (-not $PythonPath) {
    $PythonPath = Join-Path $judgeRoot '.tmp\python314\python.exe'
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        $PythonPath = (Get-Command python -ErrorAction Stop).Source
    }
}
if (-not (Test-Path -LiteralPath $PythonPath)) { throw 'Python executable not found.' }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Node.js 22+ is required. Install Node.js, then reopen PowerShell.' }
switch ($Mode) {
    'Login' { & $PythonPath -B -m ai_core.judge_cli chatgpt-login; exit $LASTEXITCODE }
    'Status' { & $PythonPath -B -m ai_core.judge_cli chatgpt-status; exit $LASTEXITCODE }
    'Models' { & $PythonPath -B -m ai_core.judge_cli chatgpt-models; exit $LASTEXITCODE }
    'Logout' { & $PythonPath -B -m ai_core.judge_cli chatgpt-logout; exit $LASTEXITCODE }
    'Tests' {
        & $PythonPath -B -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & node --test tests/chatgpt.test.mjs
        exit $LASTEXITCODE
    }
    'Live' {
        $judgeRun = 'outputs/judge-chatgpt-live-' + [guid]::NewGuid().ToString('N')
        $judgeArgs = @('-B','-m','ai_core.judge_cli','demo','--context',$Context,
            '--artifacts-dir',$judgeRun,'--mode','live','--provider','chatgpt','--allow-live','--timeout','120')
        if ($Model) { $judgeArgs += @('--model',$Model) }
        Write-Host 'Using ChatGPT plan. This sends the question, issues and evidence to the selected model.'
        & $PythonPath @judgeArgs
        $judgeExit = $LASTEXITCODE
        if (Test-Path -LiteralPath "$judgeRun/telemetry.json") {
            Write-Host "Telemetry: $judgeRoot/$judgeRun/telemetry.json"
            Get-Content -LiteralPath "$judgeRun/telemetry.json" -Raw -Encoding UTF8
        }
        Write-Host "Run folder: $judgeRoot/$judgeRun"
        exit $judgeExit
    }
}
