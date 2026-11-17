# Register-NifToolsMenu.ps1 -- right-click verbs for the three NIF tools. Per-user (HKCU) keys only.
#
# READS : HKCU\Software\Classes\{SystemFileAssociations\.nif, 0cto.Nif*}
# WRITES: HKCU\Software\Classes\SystemFileAssociations\.nif\shell\0cto*  (verbs)
#         HKCU\Software\Classes\0cto.Nif*                                 (ProgIDs)
#         HKCU\Software\Classes\.nif\OpenWithProgids                      (so "Open with" lists them)
#         nothing else, nowhere else. No desktop shortcut is created -- ever.
#
# Verbs:
#   Vertex alpha mask...   nif_vertex_gui.py      (paint an edge mask on one .nif)
#   Weld + reproject UV... nif_weld_uv_gui.py     (weld coincident vertices, rebuild the UV map)
#   Shape studio...        nif_studio.py          (merge a copy, reUV, paint in steps, preview)
#
# Why a new file: the earlier installer was not on disk any more, while the two verbs it had created still are in
# HKCU. This rebuilds all three in one place and can report what is installed (`-Status`).
#
# A lesson that is encoded here: `New-Item -Force` on an EXISTING registry key re-creates it and drops its sibling
# values, so every key is created only when `Test-Path` says it is absent, and values are set with Set-ItemProperty.

param(
    [switch]$Install,
    [switch]$Remove,
    [switch]$Status,
    [string]$Python = ""            # pythonw.exe to use; default: the one beside the running python
)

$ErrorActionPreference = "Stop"
$Tools = Split-Path -Parent $MyInvocation.MyCommand.Path
$Base = "HKCU:\Software\Classes"
$NifShell = "$Base\SystemFileAssociations\.nif\shell"

$Verbs = @(
    @{ Key = "0ctoVertexAlphaMask"; ProgId = "0cto.NifAlphaMask";   Label = "Vertex alpha mask..."; Script = "nif_vertex_gui.py" },
    @{ Key = "0ctoWeldUv";          ProgId = "0cto.NifWeldUv";      Label = "Weld + reproject UV..."; Script = "nif_weld_uv_gui.py" },
    @{ Key = "0ctoShapeStudio";     ProgId = "0cto.NifShapeStudio"; Label = "Shape studio..."; Script = "nif_studio.py" }
)

function Get-Pythonw {
    # Order matters: an explicit -Python, then whatever the existing verbs already point at (it is the interpreter
    # this workspace has been using), then PATH, then the running process. Under `powershell -File` the running
    # process is powershell.exe, which is why that is last rather than first.
    if ($Python) { return $Python }
    $existing = "$NifShell\0ctoVertexAlphaMask\command"
    if (Test-Path -LiteralPath $existing) {
        $cmd = (Get-ItemProperty -LiteralPath $existing).'(default)'
        if ($cmd -match '^"([^"]+pythonw?\.exe)"') {
            $cand = $Matches[1]
            if ($cand -match 'python\.exe$') { $cand = $cand -replace 'python\.exe$', 'pythonw.exe' }
            if (Test-Path -LiteralPath $cand) { return $cand }
        }
    }
    foreach ($name in @("pythonw.exe", "python.exe")) {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if ($c) {
            $cand = $c.Source
            if ($cand -match 'python\.exe$') { $cand = $cand -replace 'python\.exe$', 'pythonw.exe' }
            if (Test-Path -LiteralPath $cand) { return $cand }
        }
    }
    $exe = (Get-Process -Id $PID).Path
    if ($exe) { return $exe }
    return "python.exe"
}

function Ensure-Key([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) {
        New-Item -Path $path -Force | Out-Null
    }
}

function Show-Status {
    Write-Output "pythonw : $(Get-Pythonw)"
    Write-Output "tools   : $Tools"
    foreach ($v in $Verbs) {
        $cmdKey = "$NifShell\$($v.Key)\command"
        $present = Test-Path -LiteralPath $cmdKey
        $script = Join-Path $Tools $v.Script
        $state = if ($present) { "installed" } else { "absent" }
        $exists = if (Test-Path -LiteralPath $script) { "present" } else { "MISSING" }
        $value = if ($present) { (Get-ItemProperty -LiteralPath $cmdKey).'(default)' } else { "" }
        Write-Output ("{0,-22} {1,-10} script {2,-8} {3}" -f $v.Key, $state, $exists, $value)
    }
    $progids = Join-Path $Base ".nif\OpenWithProgids"
    if (Test-Path -LiteralPath $progids) {
        $names = (Get-ItemProperty -LiteralPath $progids).PSObject.Properties |
                 Where-Object { $_.Name -notlike "PS*" } | Select-Object -ExpandProperty Name
        Write-Output ("OpenWithProgids : {0}" -f ($names -join ", "))
    } else {
        Write-Output "OpenWithProgids : absent"
    }
}

if ($Status -or (-not $Install -and -not $Remove)) {
    Show-Status
    if (-not $Install -and -not $Remove) { return }
}

if ($Remove) {
    foreach ($v in $Verbs) {
        foreach ($p in @("$NifShell\$($v.Key)", "$Base\$($v.ProgId)")) {
            if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }
        }
        $progids = "$Base\.nif\OpenWithProgids"
        if (Test-Path -LiteralPath $progids) {
            $props = (Get-ItemProperty -LiteralPath $progids).PSObject.Properties | Where-Object { $_.Name -eq $v.ProgId }
            if ($props) { Remove-ItemProperty -LiteralPath $progids -Name $v.ProgId -Force }
        }
        Write-Output "removed $($v.Key)"
    }
    return
}

if ($Install) {
    $py = Get-Pythonw
    foreach ($v in $Verbs) {
        $script = Join-Path $Tools $v.Script
        if (-not (Test-Path -LiteralPath $script)) {
            Write-Output "SKIP $($v.Key): $script is not there"
            continue
        }
        $progId = $v.ProgId
        Ensure-Key "$Base\$progId"
        Set-ItemProperty -LiteralPath "$Base\$progId" -Name "(default)" -Value $v.Label
        Ensure-Key "$Base\$progId\DefaultIcon"
        Set-ItemProperty -LiteralPath "$Base\$progId\DefaultIcon" -Name "(default)" -Value "$py,1"
        Ensure-Key "$Base\$progId\shell\open\command"
        Set-ItemProperty -LiteralPath "$Base\$progId\shell\open\command" -Name "(default)" `
            -Value "`"$py`" `"$script`" `"%1`""

        Ensure-Key "$NifShell\$($v.Key)"
        Set-ItemProperty -LiteralPath "$NifShell\$($v.Key)" -Name "(default)" -Value $v.Label
        Set-ItemProperty -LiteralPath "$NifShell\$($v.Key)" -Name "Icon" -Value "$py,1"
        Ensure-Key "$NifShell\$($v.Key)\command"
        Set-ItemProperty -LiteralPath "$NifShell\$($v.Key)\command" -Name "(default)" -Value "`"$py`" `"$script`" `"%1`""

        $progids = "$Base\.nif\OpenWithProgids"
        Ensure-Key $progids
        if (-not (Get-ItemProperty -LiteralPath $progids -Name $progId -ErrorAction SilentlyContinue)) {
            New-ItemProperty -LiteralPath $progids -Name $progId -PropertyType None -Value ([byte[]]@()) -Force | Out-Null
        }
        Write-Output "installed $($v.Key) -> $script"
    }
    Write-Output ""
    Show-Status
}
