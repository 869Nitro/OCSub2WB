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

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class PanelSmoke {
    [StructLayout(LayoutKind.Sequential)]
    public struct Rect { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern IntPtr FindWindow(string cls, string title);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")]
    public static extern IntPtr GetDlgItem(IntPtr hwnd, int item);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    private static extern IntPtr SendMessageTimeout(IntPtr hwnd, uint msg, IntPtr length,
        StringBuilder text, uint flags, uint timeout, out IntPtr result);
    [DllImport("user32.dll")]
    public static extern bool PostMessage(IntPtr hwnd, uint msg, IntPtr wparam, IntPtr lparam);
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hwnd, int cmd);
    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);
    [DllImport("user32.dll")]
    public static extern bool PrintWindow(IntPtr hwnd, IntPtr dc, uint flags);
    public static bool Owns(IntPtr hwnd, uint expected) {
        uint pid;
        return hwnd != IntPtr.Zero && GetWindowThreadProcessId(hwnd, out pid) != 0 && pid == expected;
    }
    public static string Status(IntPtr hwnd) {
        var text = new StringBuilder(1024);
        IntPtr result;
        SendMessageTimeout(GetDlgItem(hwnd, 1001), 0x000D, (IntPtr)text.Capacity, text, 2, 1000, out result);
        return text.ToString();
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
    [Diagnostics.Process]::Start($info)
}

function Send-PanelMessage([IntPtr]$Target, [uint32]$Message, [int]$Parameter = 0) {
    if (![PanelSmoke]::Owns($Target, [uint32]$panel.Id)) { throw 'The test did not own the window' }
    if (![PanelSmoke]::PostMessage($Target, $Message, [IntPtr]$Parameter, [IntPtr]::Zero)) {
        throw 'Could not post a message to the test window'
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
        $found = [PanelSmoke]::FindWindow('OpenCodeGoPanel', 'OCSub2WB')
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
    $busy.Stop()

    Send-PanelMessage $window 0x0111 1002
    $health = Wait-Value 'the packaged relay on port 8787' {
        $response = Get-Health
        if ($response.ok -and $response.name -eq 'opencode-go-proxy') { $response }
    }
    if ($health.port -ne 8787) { throw 'Wrong relay port' }
    $child = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$($health.pid)"
    if ($child.ParentProcessId -ne $panel.Id -or !$child.CommandLine.Contains($package)) {
        throw 'The relay did not belong to the unpacked panel'
    }
    $null = Wait-Value 'the running status in the panel' {
        [PanelSmoke]::Status($window) -match '转发桥(已启动|已在运行|运行中)'
    }

    [void][PanelSmoke]::ShowWindow($window, 4)
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
    Write-Host 'PASS occupied port, packaged startup from a Unicode profile without Python on PATH, tray reuse, stop and exit'
} finally {
    $busy.Stop()
    if ($duplicate) {
        if (!$duplicate.HasExited) { $duplicate.Kill() }
        $duplicate.Dispose()
    }
    if ($panel) {
        foreach ($child in Get-CimInstance -ClassName Win32_Process -Filter "ParentProcessId=$($panel.Id)") {
            if ($child.Name -eq 'pythonw.exe' -and $child.CommandLine -and $child.CommandLine.Contains($package)) {
                Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
            }
        }
        if (!$panel.HasExited) { $panel.Kill() }
        $panel.Dispose()
    }
}
