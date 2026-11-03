param(
    [string]$BaseUrl = "http://127.0.0.1:8765",
    [string]$EdgePath = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    [int]$DebugPort = 9222,
    [string]$OutputDir = "evidence\task_c_1103",
    [string]$ScenarioLabel = "跨机架共享链路",
    [string]$OutputStem = "browser_acceptance"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$output = Join-Path $root $OutputDir
$profile = Join-Path $root "var\task_c_1103\edge_profile"
New-Item -ItemType Directory -Force -Path $output, $profile | Out-Null

$editorToken = $env:TASK_C_EDITOR_TOKEN
if (-not $editorToken) {
    throw "TASK_C_EDITOR_TOKEN must be set"
}

$edge = $null
$socket = $null
$commandId = 0

function Receive-CdpMessage {
    $stream = [System.IO.MemoryStream]::new()
    $buffer = New-Object byte[] 65536
    try {
        do {
            $segment = [ArraySegment[byte]]::new($buffer)
            $result = $script:socket.ReceiveAsync(
                $segment,
                [Threading.CancellationToken]::None
            ).GetAwaiter().GetResult()
            if ($result.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) {
                throw "Browser DevTools socket closed unexpectedly"
            }
            $stream.Write($buffer, 0, $result.Count)
        } while (-not $result.EndOfMessage)
        $text = [Text.Encoding]::UTF8.GetString($stream.ToArray())
        return $text | ConvertFrom-Json
    }
    finally {
        $stream.Dispose()
    }
}

function Invoke-Cdp {
    param(
        [Parameter(Mandatory)] [string]$Method,
        [hashtable]$Params = @{}
    )
    $script:commandId += 1
    $id = $script:commandId
    $payload = @{ id = $id; method = $Method; params = $Params } |
        ConvertTo-Json -Depth 100 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    $segment = [ArraySegment[byte]]::new($bytes)
    $script:socket.SendAsync(
        $segment,
        [System.Net.WebSockets.WebSocketMessageType]::Text,
        $true,
        [Threading.CancellationToken]::None
    ).GetAwaiter().GetResult() | Out-Null

    while ($true) {
        $message = Receive-CdpMessage
        if ($message.id -eq $id) {
            if ($message.error) {
                throw "CDP $Method failed: $($message.error.message)"
            }
            return $message.result
        }
    }
}

try {
    $arguments = @(
        "--headless=new",
        "--disable-gpu",
        "--disable-extensions",
        "--no-first-run",
        "--remote-debugging-port=$DebugPort",
        "--user-data-dir=$profile",
        "--window-size=1600,1200",
        "about:blank"
    )
    $edge = Start-Process -FilePath $EdgePath -ArgumentList $arguments -PassThru -WindowStyle Hidden

    $targets = $null
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $targets = Invoke-RestMethod -Uri "http://127.0.0.1:$DebugPort/json/list" -TimeoutSec 1
            if ($targets) { break }
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $targets) {
        throw "Edge DevTools endpoint did not start"
    }

    $target = $targets | Where-Object { $_.type -eq "page" } | Select-Object -First 1
    if (-not $target) {
        throw "No browser page target was available"
    }

    $socket = [System.Net.WebSockets.ClientWebSocket]::new()
    $socket.ConnectAsync(
        [Uri]$target.webSocketDebuggerUrl,
        [Threading.CancellationToken]::None
    ).GetAwaiter().GetResult() | Out-Null

    Invoke-Cdp -Method "Page.enable" | Out-Null
    Invoke-Cdp -Method "Runtime.enable" | Out-Null
    Invoke-Cdp -Method "Emulation.setDeviceMetricsOverride" -Params @{
        width = 1600
        height = 1200
        deviceScaleFactor = 1
        mobile = $false
    } | Out-Null
    Invoke-Cdp -Method "Page.navigate" -Params @{ url = "$BaseUrl/workload" } | Out-Null
    Start-Sleep -Seconds 1

    $tokenLiteral = $editorToken | ConvertTo-Json -Compress
    $scenarioLiteral = $ScenarioLabel | ConvertTo-Json -Compress
    $expression = @"
(async () => {
  const wait = async (predicate, label, timeout = 30000) => {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error('Timed out waiting for ' + label);
  };
  await wait(() => document.readyState === 'complete', 'page load');
  document.getElementById('token').value = $tokenLiteral;
  document.getElementById('connectButton').click();
  await wait(() => document.getElementById('connection').textContent.startsWith('已连接'), 'connection');
  await wait(() => document.querySelectorAll('#scenario option').length > 1, 'scenario load');
  const scenario = document.getElementById('scenario');
  const competition = [...scenario.options].find(option => option.textContent.includes($scenarioLiteral));
  if (!competition) throw new Error('Requested scenario was not listed');
  scenario.value = competition.value;
  scenario.dispatchEvent(new Event('change'));
  document.getElementById('runButton').click();
  await wait(() => document.getElementById('status').textContent === '评估完成'
    || getComputedStyle(document.getElementById('error')).display === 'block', 'simulation completion');
  const status = document.getElementById('status').textContent;
  const error = document.getElementById('error').textContent;
  if (status !== '评估完成') throw new Error(status + ' ' + error);
  document.getElementById('explainButton').click();
  await wait(() => document.querySelectorAll('#explanation p').length > 0, 'read-only explanation');
  const explanation = [...document.querySelectorAll('#explanation p')].map(item => item.textContent);
  const result = {
    title: document.title,
    connection: document.getElementById('connection').textContent,
    status,
    identity: document.getElementById('identity').textContent,
    metrics: document.querySelectorAll('#metrics .metric').length,
    result_rows: document.querySelectorAll('#items tbody tr').length,
    timeline_svg: Boolean(document.querySelector('#timeline svg')),
    resource_svg: Boolean(document.querySelector('#resourceChart svg')),
    invariant_checks: document.querySelectorAll('#checks .check').length,
    evidence_cards: document.querySelectorAll('#evidenceGrid .evidence-item').length,
    evidence_text: document.getElementById('evidenceGrid').textContent,
    evidence_status: document.getElementById('evidenceStatus').textContent,
    explanation,
    page_text_contains_replay_boundary:
      document.body.textContent.includes('仿真记录回放')
      && document.body.textContent.includes('不是服务器正在执行训练的实时监控')
  };
  window.scrollTo(0, 0);
  return result;
})()
"@

    $evaluated = Invoke-Cdp -Method "Runtime.evaluate" -Params @{
        expression = $expression
        awaitPromise = $true
        returnByValue = $true
    }
    if ($evaluated.exceptionDetails) {
        throw "Browser script failed: $($evaluated.exceptionDetails.text) $($evaluated.exceptionDetails.exception.description)"
    }
    $browserResult = $evaluated.result.value

    $layout = Invoke-Cdp -Method "Page.getLayoutMetrics"
    $contentHeight = [Math]::Min(3600, [Math]::Ceiling([double]$layout.cssContentSize.height))
    $capture = Invoke-Cdp -Method "Page.captureScreenshot" -Params @{
        format = "png"
        fromSurface = $true
        captureBeyondViewport = $true
        clip = @{
            x = 0
            y = 0
            width = 1600
            height = $contentHeight
            scale = 1
        }
    }
    $screenshotPath = Join-Path $output "$OutputStem.png"
    [IO.File]::WriteAllBytes($screenshotPath, [Convert]::FromBase64String($capture.data))

    $passed = $browserResult.status -eq "评估完成" `
        -and $browserResult.metrics -gt 0 `
        -and $browserResult.result_rows -gt 0 `
        -and $browserResult.timeline_svg `
        -and $browserResult.resource_svg `
        -and $browserResult.invariant_checks -gt 0 `
        -and $browserResult.evidence_cards -eq 3 `
        -and $browserResult.evidence_text.Contains('熊宇琛') `
        -and $browserResult.evidence_text.Contains('马贤纯') `
        -and $browserResult.evidence_text.Contains('王忻晔') `
        -and $browserResult.explanation.Count -gt 0 `
        -and $browserResult.page_text_contains_replay_boundary
    $payload = @{
        schema_version = 1
        task = "C"
        tested_at = [DateTimeOffset]::UtcNow.ToString("o")
        base_url = $BaseUrl
        browser = "Microsoft Edge 156.0.4314.58 headless"
        editor = $browserResult
        screenshot = $screenshotPath
        passed = $passed
        notes = @(
            "The browser used an editor bearer token supplied only through the page input.",
            "The token was not persisted to the URL, repository, or evidence file.",
            "The page uses synthetic simulation inputs; it does not monitor a live GPU."
        )
    }
    $jsonPath = Join-Path $output "$OutputStem.json"
    $payload | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $jsonPath -Encoding utf8
    $payload | ConvertTo-Json -Depth 4
    if (-not $payload.passed) {
        exit 1
    }
}
finally {
    if ($socket) {
        try {
            $socket.Dispose()
        }
        catch {}
    }
    if ($edge -and -not $edge.HasExited) {
        Stop-Process -Id $edge.Id -Force
    }
}
