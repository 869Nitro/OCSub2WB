param(
    [Parameter(Mandatory)][string]$Zip,
    [string]$ScreenshotPath
)

$ErrorActionPreference = 'Stop'
$archive = (Resolve-Path -LiteralPath $Zip).Path
$run = Join-Path ([IO.Path]::GetTempPath()) ('ocsub2wb-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
[IO.Directory]::CreateDirectory($run) | Out-Null
$unpacked = Join-Path $run '解压 包'
[IO.Compression.ZipFile]::ExtractToDirectory($archive, $unpacked)
$package = Join-Path $unpacked 'OCSub2WB'
foreach ($file in 'OCSub2WB.exe','opencode-go-proxy.py','ocgo_common.py','README.md','LICENSE','THIRD-PARTY-NOTICES.md','assets\icon.png') {
    if (!(Test-Path -LiteralPath (Join-Path $package $file) -PathType Leaf)) { throw "Missing package file $file" }
}

$profile = Join-Path $run 'WorkBuddy 用户'
$versions = Join-Path $profile '.workbuddy\binaries\python\versions'
[IO.Directory]::CreateDirectory($versions) | Out-Null
$python = (Get-Command python -CommandType Application | Select-Object -First 1).Source
$pythonDir = Split-Path -Parent $python
$runtime = Join-Path $versions 'test-runtime'
New-Item -ItemType Junction -Path $runtime -Target $pythonDir | Out-Null
if (!(Test-Path -LiteralPath (Join-Path $runtime 'pythonw.exe') -PathType Leaf)) {
    throw 'The test Python distribution must include pythonw.exe'
}

$versionSource = [IO.File]::ReadAllText((Join-Path $package 'ocgo_common.py'))
$version = [regex]::Match($versionSource, '(?m)^VERSION = "([^"\r\n]+)"').Groups[1].Value
if (!$version) { throw 'Missing packaged application version' }
$panelTitle = "OCSub2WB v$version"

# Substitute the catalog and quota responses in the child interpreter. The unpacked
# application files remain byte-for-byte the ones in the release.
$fixture = Join-Path $run 'upstream fixture'
[IO.Directory]::CreateDirectory($fixture) | Out-Null
$testUpstream = @'
import http.client
import json
import os
from pathlib import Path

class Reply:
    status = 200

    def __init__(self, resource):
        self.resource = resource

    def getheaders(self):
        return [("Content-Type", "application/json")]

    def read(self):
        if self.resource == "usage":
            return b'{"usage":{"monthly":{"percent":12},"weekly":{"percent":7},"rolling":{"percent":3}}}'
        return json.dumps({"data": [
            {"id": "deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash 1017",
             "type": "chat", "limit": {"context": 1000000, "output": 384000},
             "cost": {"text": {"input": 1, "output": 2}}},
            {"id": "hy4-flash-free", "name": "Hy4 flash Free", "type": "chat",
             "limit": {"context": 128000, "output": 16000},
             "cost": {"text": {"input": 0, "output": 0}}},
        ]}).encode("utf-8")

def fixture_connect(self):
    raise OSError("Network disabled in the packaged panel test")


def fixture_request(self, method, url, body=None, headers=None, *, encode_chunked=False):
    if (self.host, method, url) not in (
        ("opencode.ai", "GET", "/inference/go/v1/models"),
        ("opencode.ai", "GET", "/inference/go/v1/usage"),
    ) or (headers or {}).get("Authorization") != "Bearer oc_sk_panel_start_fixture":
        raise OSError("Unexpected request in the packaged panel test")
    self.fixture_resource = url.rsplit("/", 1)[-1]


def fixture_response(self):
    marker = "OCSUB2WB_CATALOG_MARKER" if self.fixture_resource == "models" else "OCSUB2WB_USAGE_MARKER"
    Path(os.environ[marker]).write_text(f"{os.getpid()}:{os.getppid()}", encoding="ascii")
    return Reply(self.fixture_resource)


http.client.HTTPSConnection.connect = fixture_connect
http.client.HTTPSConnection.request = fixture_request
http.client.HTTPSConnection.getresponse = fixture_response
'@
[IO.File]::WriteAllText((Join-Path $fixture 'sitecustomize.py'), $testUpstream, [Text.UTF8Encoding]::new($false))
$catalogMarker = Join-Path $run 'catalog-used'
$usageMarker = Join-Path $run 'usage-used'
$envPath = Join-Path $package '.env'
$modelsPath = Join-Path $profile '.workbuddy\models.json'
$initialModels = '[{"id":"deepseek-v4.1-flash","vendor":"OpenCode","url":"http://127.0.0.1:8787/v1","apiKey":"oc_sk_old_fixture"}]'
[IO.File]::WriteAllText($modelsPath, $initialModels)

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class PanelSmoke {
    [StructLayout(LayoutKind.Sequential)]
    public struct Rect {
        public int Left, Top, Right, Bottom;
        public int Width => Right - Left;
        public int Height => Bottom - Top;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct MonitorInfo { public int Size; public Rect Monitor, Work; public uint Flags; }
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern IntPtr FindWindow(string cls, string title);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")]
    public static extern IntPtr GetDlgItem(IntPtr hwnd, int item);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    private static extern IntPtr SendMessageTimeout(IntPtr hwnd, uint msg, IntPtr length,
        StringBuilder text, uint flags, uint timeout, out IntPtr result);
    [DllImport("user32.dll", EntryPoint="SendMessageTimeoutW", CharSet=CharSet.Unicode)]
    private static extern IntPtr SendTextTimeout(IntPtr hwnd, uint msg, IntPtr wparam,
        string text, uint flags, uint timeout, out IntPtr result);
    [DllImport("user32.dll")]
    public static extern bool PostMessage(IntPtr hwnd, uint msg, IntPtr wparam, IntPtr lparam);
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hwnd, int cmd);
    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);
    [DllImport("user32.dll")]
    public static extern bool IsZoomed(IntPtr hwnd);
    [DllImport("user32.dll")]
    private static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int w, int h, uint flags);
    [DllImport("user32.dll")]
    private static extern IntPtr MonitorFromWindow(IntPtr hwnd, uint flags);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    private static extern bool GetMonitorInfo(IntPtr monitor, ref MonitorInfo info);
    public static Rect Bounds(IntPtr hwnd) {
        Rect rect;
        if (!GetWindowRect(hwnd, out rect)) throw new InvalidOperationException("Window bounds unavailable");
        return rect;
    }
    public static Rect WorkArea(IntPtr hwnd) {
        var info = new MonitorInfo { Size = Marshal.SizeOf(typeof(MonitorInfo)) };
        if (!GetMonitorInfo(MonitorFromWindow(hwnd, 2), ref info))
            throw new InvalidOperationException("Monitor work area unavailable");
        return info.Work;
    }
    public static void Position(IntPtr hwnd, int x, int y, int width, int height, bool checkTrackLimits) {
        const uint noActivateOrZOrder = 0x0014;
        const uint noSendChanging = 0x0400;
        uint flags = noActivateOrZOrder | (checkTrackLimits ? 0 : noSendChanging);
        if (!SetWindowPos(hwnd, IntPtr.Zero, x, y, width, height, flags))
            throw new InvalidOperationException("Could not position the test window");
    }
    [DllImport("user32.dll")]
    public static extern bool PrintWindow(IntPtr hwnd, IntPtr dc, uint flags);
    public static bool Owns(IntPtr hwnd, uint expected) {
        uint pid;
        return hwnd != IntPtr.Zero && GetWindowThreadProcessId(hwnd, out pid) != 0 && pid == expected;
    }
    public static string ControlText(IntPtr hwnd, int id) {
        var text = new StringBuilder(1024);
        IntPtr result;
        SendMessageTimeout(GetDlgItem(hwnd, id), 0x000D, (IntPtr)text.Capacity, text, 2, 1000, out result);
        return text.ToString();
    }
    public static string Status(IntPtr hwnd) { return ControlText(hwnd, 1001); }
    public static bool SetKey(IntPtr hwnd, string key) {
        IntPtr result;
        return SendTextTimeout(GetDlgItem(hwnd, 1031), 0x000C, IntPtr.Zero, key, 2, 1000, out result)
            != IntPtr.Zero && result != IntPtr.Zero;
    }
    public static bool ClearLog(IntPtr hwnd) {
        IntPtr result;
        return SendTextTimeout(GetDlgItem(hwnd, 1051), 0x000C, IntPtr.Zero, "", 2, 1000, out result)
            != IntPtr.Zero;
    }
}
"@

function Wait-Value([string]$Description, [scriptblock]$Check, [int]$Seconds = 20) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    do {
        $value = & $Check
        if ($value) { return $value }
        Start-Sleep -Milliseconds 150
    } while ($watch.Elapsed.TotalSeconds -lt $Seconds)
    throw "Timed out waiting for $Description"
}

function New-Panel {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = Join-Path $package 'OCSub2WB.exe'
    $info.WorkingDirectory = $package
    $info.UseShellExecute = $false
    $info.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $info.Environment['USERPROFILE'] = $profile
    $info.Environment['PATH'] = "$env:SystemRoot\System32;$env:SystemRoot"
    $info.Environment['PYTHONDONTWRITEBYTECODE'] = '1'
    $info.Environment['PYTHONPATH'] = $fixture
    $info.Environment['OCSUB2WB_CATALOG_MARKER'] = $catalogMarker
    $info.Environment['OCSUB2WB_USAGE_MARKER'] = $usageMarker
    [Diagnostics.Process]::Start($info)
}

function Send-PanelMessage([IntPtr]$Target, [uint32]$Message, [int]$Parameter = 0) {
    if (![PanelSmoke]::Owns($Target, [uint32]$panel.Id)) { throw 'The test did not own the window' }
    if (![PanelSmoke]::PostMessage($Target, $Message, [IntPtr]$Parameter, [IntPtr]::Zero)) {
        throw 'Could not post a message to the test window'
    }
}

function Choose-KeySave([IntPtr]$Window, [string]$Key, [int]$Choice) {
    if (![PanelSmoke]::SetKey($Window, $Key)) { throw 'Could not set the test API key' }
    Send-PanelMessage $Window 0x0111 1032
    $dialog = Wait-Value 'the API key save dialog' {
        $found = [PanelSmoke]::FindWindow('#32770', '保存 API Key')
        if ([PanelSmoke]::Owns($found, [uint32]$panel.Id)) { $found }
    }
    Send-PanelMessage $dialog 0x0466 $Choice # TDM_CLICK_BUTTON
    $null = Wait-Value 'the API key save dialog to close' {
        ![PanelSmoke]::Owns([PanelSmoke]::FindWindow('#32770', '保存 API Key'), [uint32]$panel.Id)
    }
}

function Test-PanelWindow([IntPtr]$Window) {
    $null = Wait-Value 'the panel controls' {
        [PanelSmoke]::GetDlgItem($Window, 1021) -ne [IntPtr]::Zero -and
            [PanelSmoke]::GetDlgItem($Window, 1051) -ne [IntPtr]::Zero
    }
    $initial = [PanelSmoke]::Bounds($Window)
    $models = [PanelSmoke]::GetDlgItem($Window, 1021)
    $log = [PanelSmoke]::GetDlgItem($Window, 1051)
    $modelsInitial = [PanelSmoke]::Bounds($models)
    $logInitial = [PanelSmoke]::Bounds($log)

    # Exercise the layout even if the runner's desktop limits interactive enlargement.
    [PanelSmoke]::Position($Window, $initial.Left, $initial.Top, ($initial.Width + 120), ($initial.Height + 100), $false)
    $null = Wait-Value 'the model list and log to expand' {
        $grown = [PanelSmoke]::Bounds($Window)
        $modelsGrown = [PanelSmoke]::Bounds($models)
        $logGrown = [PanelSmoke]::Bounds($log)
        $grown.Width -gt $initial.Width -and $grown.Height -gt $initial.Height -and
            $modelsGrown.Width -gt $modelsInitial.Width -and $modelsGrown.Height -gt $modelsInitial.Height -and
            $logGrown.Width -gt $logInitial.Width -and $logGrown.Height -gt $logInitial.Height
    }

    $work = [PanelSmoke]::WorkArea($Window)
    [PanelSmoke]::Position($Window, ($work.Right - 100), ($work.Bottom - 100), ($initial.Width - 120), ($initial.Height - 100), $true)
    $minimum = [PanelSmoke]::Bounds($Window)
    if ($minimum.Width -ne $initial.Width -or $minimum.Height -ne $initial.Height) {
        throw 'The panel did not enforce its initial size as the minimum'
    }
    if ($minimum.Right -le $work.Right -or $minimum.Bottom -le $work.Bottom) {
        throw 'Could not position the test window outside the work area'
    }
    Send-PanelMessage $Window 0x007E 32 # WM_DISPLAYCHANGE
    $normal = Wait-Value 'the panel to move into the current work area' {
        $bounds = [PanelSmoke]::Bounds($Window)
        if ($bounds.Right -le $work.Right -and $bounds.Top -ge $work.Top -and
            $bounds.Bottom -le [Math]::Max($work.Bottom, $work.Top + $initial.Height)) { $bounds }
    }

    Send-PanelMessage $Window 0x0112 0xF030 # WM_SYSCOMMAND / SC_MAXIMIZE
    $null = Wait-Value 'the panel to maximize' { [PanelSmoke]::IsZoomed($Window) }
    Send-PanelMessage $Window 0x0112 0xF120 # WM_SYSCOMMAND / SC_RESTORE
    $null = Wait-Value 'the panel to restore to its previous size' {
        $bounds = [PanelSmoke]::Bounds($Window)
        !$([PanelSmoke]::IsZoomed($Window)) -and $bounds.Width -eq $normal.Width -and
            $bounds.Height -eq $normal.Height
    }
}

function Get-Health {
    try { Invoke-RestMethod -Uri 'http://127.0.0.1:8787/healthz' -NoProxy -TimeoutSec 1 }
    catch { $null }
}

function Save-PanelScreenshot([IntPtr]$Window) {
    if (!$ScreenshotPath) { return }
    try {
        Add-Type -AssemblyName System.Drawing
        if (![PanelSmoke]::ClearLog($Window)) { throw 'Could not clear the fixture log from the preview' }
        $rect = [PanelSmoke+Rect]::new()
        if (![PanelSmoke]::GetWindowRect($Window, [ref]$rect)) { throw 'Window bounds unavailable' }
        $bitmap = [Drawing.Bitmap]::new(($rect.Right - $rect.Left), ($rect.Bottom - $rect.Top))
        try {
            $graphics = [Drawing.Graphics]::FromImage($bitmap)
            try {
                $dc = $graphics.GetHdc()
                try {
                    if (![PanelSmoke]::PrintWindow($Window, $dc, 2)) { throw 'PrintWindow failed' }
                } finally { $graphics.ReleaseHdc($dc) }
            } finally { $graphics.Dispose() }
            $bitmap.Save($ScreenshotPath, [Drawing.Imaging.ImageFormat]::Png)
        } finally { $bitmap.Dispose() }
    } catch { Write-Warning "Panel screenshot unavailable: $($_.Exception.Message)" }
}

$busy = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 8787)
$busy.ExclusiveAddressUse = $true
$panel = $null
$duplicate = $null
try {
    try { $busy.Start() }
    catch { throw 'Port 8787 is already occupied. Use an isolated Windows runner.' }

    $panel = New-Panel
    $window = Wait-Value 'the packaged panel window' {
        if ($panel.HasExited) { throw "Panel exited with code $($panel.ExitCode)" }
        $found = [PanelSmoke]::FindWindow('OpenCodeGoPanel', $panelTitle)
        if ([PanelSmoke]::Owns($found, [uint32]$panel.Id)) { $found }
    }

    Send-PanelMessage $window 0x0111 1002
    $alert = Wait-Value 'the port-occupied alert' {
        $found = [PanelSmoke]::FindWindow('#32770', 'OCSub2WB')
        if ([PanelSmoke]::Owns($found, [uint32]$panel.Id)) { $found }
    }
    if ([PanelSmoke]::Status($window) -notmatch '8787.*占用') { throw 'Panel did not report the occupied port' }
    Send-PanelMessage $alert 0x0010
    $null = Wait-Value 'the occupied-port alert to close' {
        ![PanelSmoke]::Owns([PanelSmoke]::FindWindow('#32770', 'OCSub2WB'), [uint32]$panel.Id)
    }
    Choose-KeySave $window 'oc_sk_panel_cancel_fixture' 2
    if (Test-Path -LiteralPath $envPath) { throw 'Cancel created a local configuration file' }

    Choose-KeySave $window 'oc_sk_panel_offline_fixture' 201
    $null = Wait-Value 'the key to be saved locally' { Test-Path -LiteralPath $envPath }
    if ([IO.File]::ReadAllText($envPath).Trim() -ne 'OPENCODE_API_KEY=oc_sk_panel_offline_fixture') {
        throw 'The first offline save did not create the expected configuration'
    }
    if ([IO.File]::ReadAllText($modelsPath) -ne $initialModels -or
        (Test-Path -LiteralPath $catalogMarker) -or (Test-Path -LiteralPath $usageMarker) -or
        (Test-Path -LiteralPath (Join-Path $package 'cache\proxy.pid'))) {
        throw 'Offline save changed the models, started the relay, or used the upstream'
    }

    $existing = ('# keep this comment', '  OPENCODE_API_KEY = old', 'OPENCODE_API_KEY=older',
        'OPENCODE_UPSTREAM_PROXY=off', '') -join [Environment]::NewLine
    [IO.File]::WriteAllText($envPath, $existing, [Text.UTF8Encoding]::new($true))
    Choose-KeySave $window 'oc_sk_panel_offline_fixture' 201
    $null = Wait-Value 'the existing config to be updated offline' {
        $config = [IO.File]::ReadAllText($envPath)
        [regex]::Matches($config, '(?m)^OPENCODE_API_KEY=oc_sk_panel_offline_fixture\r?$').Count -eq 2
    }
    $saved = [IO.File]::ReadAllText($envPath)
    if (!$saved.Contains('# keep this comment' + [Environment]::NewLine) -or
        !$saved.Contains('OPENCODE_UPSTREAM_PROXY=off' + [Environment]::NewLine) -or
        [IO.File]::ReadAllBytes($envPath)[0] -ne 0xEF) {
        throw 'Offline save did not preserve the existing configuration'
    }
    $busy.Stop()

    # Exercise save and start with no configuration left by the offline tests.
    Remove-Item -LiteralPath $envPath
    Choose-KeySave $window 'oc_sk_panel_start_fixture' 202
    $health = Wait-Value 'the packaged relay on port 8787' {
        $response = Get-Health
        if ($response.ok -and $response.name -eq 'opencode-go-proxy') { $response }
    }
    if ($health.port -ne 8787) { throw 'Wrong relay port' }
    $pidFile = Join-Path $package 'cache\proxy.pid'
    if (!(Test-Path -LiteralPath $pidFile) -or [IO.File]::ReadAllText($pidFile).Trim() -ne [string]$health.pid) {
        throw 'The relay did not use the unpacked package'
    }
    $null = Wait-Value 'the running status in the panel' {
        [PanelSmoke]::Status($window) -match '转发桥(已启动|已在运行|运行中)'
    }
    $null = Wait-Value 'the list to refresh after the key is saved and validated' {
        (Test-Path -LiteralPath $catalogMarker) -and (Test-Path -LiteralPath $usageMarker) -and
            [PanelSmoke]::ControlText($window, 1029) -match '2 可选'
    }
    $null = Wait-Value 'all three usage windows to refresh automatically' {
        [PanelSmoke]::ControlText($window, 1017) -match '^3%' -and
            [PanelSmoke]::ControlText($window, 1018) -match '^7%' -and
            [PanelSmoke]::ControlText($window, 1019) -match '^12%'
    }
    if ([IO.File]::ReadAllText($catalogMarker).Trim() -ne "$($health.pid):$($panel.Id)") {
        throw 'The relay did not belong to the unpacked panel'
    }
    $managed = @(Get-Content -LiteralPath $modelsPath -Raw | ConvertFrom-Json)
    if ($managed.Count -ne 1 -or $managed[0].apiKey -ne 'oc_sk_panel_start_fixture') {
        throw 'Save and start did not update the existing managed key'
    }

    [void][PanelSmoke]::ShowWindow($window, 4)
    Test-PanelWindow $window
    Start-Sleep -Milliseconds 400
    Save-PanelScreenshot $window
    Send-PanelMessage $window 0x0010
    $null = Wait-Value 'the panel hidden to tray' { ![PanelSmoke]::IsWindowVisible($window) }

    $duplicate = New-Panel
    if (!$duplicate.WaitForExit(5000) -or $duplicate.ExitCode -ne 0) { throw 'The second panel did not reuse the first' }
    $null = Wait-Value 'the single panel restored from tray' { [PanelSmoke]::IsWindowVisible($window) }

    Send-PanelMessage $window 0x0111 1002
    $null = Wait-Value 'the relay to stop' { !(Get-Health) }
    Send-PanelMessage $window 0x0111 1004
    $confirm = Wait-Value 'the exit confirmation' {
        $found = [PanelSmoke]::FindWindow('#32770', 'OCSub2WB')
        if ([PanelSmoke]::Owns($found, [uint32]$panel.Id)) { $found }
    }
    Send-PanelMessage $confirm 0x0111 6
    if (!$panel.WaitForExit(7000)) { throw 'The panel did not exit' }
    Write-Host 'PASS key save cancel, offline save, save and start, model and usage refresh, window resize, maximize and restore, display-change recovery, occupied port, tray reuse, stop and exit'
} finally {
    $busy.Stop()
    if ($duplicate) {
        if (!$duplicate.HasExited) { $duplicate.Kill() }
        $duplicate.Dispose()
    }
    if ($panel) {
        $pidFile = Join-Path $package 'cache\proxy.pid'
        if (Test-Path -LiteralPath $pidFile) {
            $relayPid = [IO.File]::ReadAllText($pidFile).Trim() -as [int]
            if ($relayPid) {
                $child = Get-Process -Id $relayPid -ErrorAction SilentlyContinue
                if ($child -and $child.Path -eq (Join-Path $runtime 'pythonw.exe')) {
                    Stop-Process -InputObject $child -ErrorAction SilentlyContinue
                }
            }
        }
        if (!$panel.HasExited) { $panel.Kill() }
        $panel.Dispose()
    }
}
