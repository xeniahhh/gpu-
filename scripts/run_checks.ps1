param(
    [switch]$SkipWebBuild,
    [switch]$SkipBrowserSmoke
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

Push-Location $root
try {
    & $python scripts\check_env.py
    if ($LASTEXITCODE -ne 0) { throw "Environment check failed" }

    & $python scripts\run_tests.py
    if ($LASTEXITCODE -ne 0) { throw "Python tests failed" }

    if (-not $SkipWebBuild) {
        Push-Location "apps\web"
        try {
            pnpm build
            if ($LASTEXITCODE -ne 0) { throw "Web build failed" }
        }
        finally {
            Pop-Location
        }
    }

    if (-not $SkipBrowserSmoke) {
        $server = Start-Process `
            -FilePath $python `
            -ArgumentList "scripts\start_gateway.py" `
            -WorkingDirectory $root `
            -PassThru `
            -WindowStyle Hidden
        try {
            $ready = $false
            for ($attempt = 0; $attempt -lt 20; $attempt++) {
                try {
                    $response = Invoke-WebRequest `
                        -UseBasicParsing `
                        -Uri "http://127.0.0.1:8000/api/health" `
                        -TimeoutSec 2
                    if ($response.StatusCode -eq 200) {
                        $ready = $true
                        break
                    }
                }
                catch {
                    Start-Sleep -Milliseconds 250
                }
            }
            if (-not $ready) { throw "Gateway did not become ready" }
            Push-Location "apps\web"
            try {
                pnpm run smoke
                if ($LASTEXITCODE -ne 0) { throw "Browser smoke test failed" }
            }
            finally {
                Pop-Location
            }
        }
        finally {
            Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        }
    }

    Write-Host "All checks passed."
}
finally {
    Pop-Location
}
