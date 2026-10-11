# Build a redistributable zip of claude-usage-bar.
#
# Runs the unit tests, packs usage_widget.py with the repo spec via
# PyInstaller, smoke-tests the exe, then writes a zip and a .sha256
# sidecar. The zip runs without a Python install.
#
# Parameters:
#   -Python     Interpreter used for tests and PyInstaller. Default: python
#   -OutDir     Output directory. Default: <repo>\dist
#   -SkipTests  Skip: python -B -m unittest discover -s tests
#
# Success prints these last three lines, then exits 0:
#   ZIP=<absolute path of the zip>
#   SHA256=<64 lowercase hex characters>
#   SIZE=<size in bytes>
#
# Dependency: PyInstaller must already be installed in the -Python
# interpreter. This script does not download files and does not pip install.
# If PyInstaller is missing it exits 1 with:
#   ERROR=PyInstaller not found for <python>; install it with: <python> -m pip install pyinstaller
#
# ASCII only: Windows PowerShell 5.1 reads a BOM-less .ps1 as GBK, so a
# non-ASCII byte in this file would be corrupted.

[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$OutDir = '',
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    throw $Message
}

function Invoke-Native([string]$FileName) {
    $argList = @($args)
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    & $FileName @argList
    $code = $global:LASTEXITCODE
    if ($null -eq $code) {
        $code = 1
    }
    $script:NativeExit = [int]$code
}

function Get-CommitId([string]$RepoPath) {
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    $headText = $null
    $headExit = 1
    try {
        $headText = & git -C $RepoPath rev-parse --short=7 HEAD
        $headExit = $global:LASTEXITCODE
    } catch {
        return 'nogit'
    }
    $first = $null
    if ($null -ne $headText) {
        $first = @($headText) | Select-Object -First 1
    }
    $short = ([string]$first).Trim()
    if ($headExit -ne 0 -or [string]::IsNullOrWhiteSpace($short)) {
        return 'nogit'
    }
    if ($short -notmatch '^[0-9a-fA-F]+$') {
        return 'nogit'
    }
    $global:LASTEXITCODE = 0
    $statusText = $null
    $statusExit = 1
    try {
        $statusText = & git -C $RepoPath status --porcelain
        $statusExit = $global:LASTEXITCODE
    } catch {
        return 'nogit'
    }
    if ($statusExit -ne 0) {
        return 'nogit'
    }
    $joined = ''
    if ($null -ne $statusText) {
        $joined = @($statusText) -join "`n"
    }
    if ($joined.Length -gt 0) {
        return ($short + '-dirty')
    }
    return $short
}

function Remove-InsideOutDir([string]$Path, [string]$RootDir) {
    $full = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetFullPath($RootDir)
    if (-not $root.EndsWith('\')) {
        $root = $root + '\'
    }
    if (-not $full.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        Fail ("refusing to delete outside OutDir: " + $full)
    }
    if (-not (Test-Path -LiteralPath $full)) {
        return
    }
    if ((Get-Item -LiteralPath $full).PSIsContainer) {
        Remove-Item -LiteralPath $full -Recurse -Force -Confirm:$false
    } else {
        Remove-Item -LiteralPath $full -Force -Confirm:$false
    }
}

$startLocation = (Get-Location).Path
try {
    $repo = Split-Path -Parent $PSScriptRoot
    $repo = [System.IO.Path]::GetFullPath($repo)
    if ([string]::IsNullOrWhiteSpace($OutDir)) {
        $OutDir = Join-Path $repo 'dist'
    }
    if (-not [System.IO.Path]::IsPathRooted($OutDir)) {
        $OutDir = Join-Path (Get-Location).ProviderPath $OutDir
    }
    $OutDir = [System.IO.Path]::GetFullPath($OutDir)
    if ($OutDir.Length -gt 3) {
        $OutDir = $OutDir.TrimEnd('\')
    }
    if (-not [System.IO.Directory]::Exists($OutDir)) {
        $null = [System.IO.Directory]::CreateDirectory($OutDir)
    }

    $pluginPath = Join-Path $repo 'plugins\usage-feed\.claude-plugin\plugin.json'
    if (-not (Test-Path -LiteralPath $pluginPath -PathType Leaf)) {
        Fail ("plugin.json not found: " + $pluginPath)
    }
    $pluginText = [System.IO.File]::ReadAllText($pluginPath, [System.Text.Encoding]::UTF8)
    $plugin = $pluginText | ConvertFrom-Json
    $version = [string]$plugin.version
    if ($version -notmatch '^\d+\.\d+\.\d+$') {
        Fail ("plugin version is not major.minor.patch: " + $version)
    }

    $commit = Get-CommitId $repo
    $zipName = 'claude-usage-bar-' + $version + '-' + $commit + '.zip'
    $zipPath = Join-Path $OutDir $zipName
    $shaPath = $zipPath + '.sha256'
    $exePath = Join-Path $OutDir 'claude-usage-bar.exe'
    $smoke = Join-Path $OutDir 'selftest'
    $spec = Join-Path $repo 'claude-usage-bar.spec'
    $work = Join-Path $repo 'build'

    Set-Location -LiteralPath $repo

    if (-not $SkipTests) {
        Invoke-Native $Python '-B' '-m' 'unittest' 'discover' '-s' 'tests'
        if ($script:NativeExit -ne 0) {
            Fail ("unit tests failed with exit code " + $script:NativeExit)
        }
    }

    Invoke-Native $Python '-m' 'PyInstaller' '--version'
    if ($script:NativeExit -ne 0) {
        Fail ("PyInstaller not found for " + $Python + "; install it with: " + $Python + " -m pip install pyinstaller")
    }

    Invoke-Native $Python '-m' 'PyInstaller' '--noconfirm' '--clean' '--distpath' $OutDir '--workpath' $work $spec
    if ($script:NativeExit -ne 0) {
        Fail ("PyInstaller failed with exit code " + $script:NativeExit)
    }
    if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
        Fail ("executable not found: " + $exePath)
    }

    Remove-InsideOutDir $smoke $OutDir
    $proc = Start-Process -FilePath $exePath -ArgumentList '--selftest-render', ('"' + $smoke + '"') -PassThru
    $finished = $proc.WaitForExit(120000)
    if (-not $finished) {
        $proc.Kill()
        Fail 'selftest timed out after 120000 ms'
    }
    $proc.Refresh()
    if ($proc.ExitCode -ne 0) {
        Fail ("selftest exit code " + $proc.ExitCode)
    }
    $sheet = Join-Path $smoke 'contact_sheet.png'
    if (-not (Test-Path -LiteralPath $sheet -PathType Leaf)) {
        Fail ("contact_sheet.png not found: " + $sheet)
    }
    Remove-InsideOutDir $smoke $OutDir

    $expected = @(
        'claude-usage-bar/claude-usage-bar.exe',
        'claude-usage-bar/INSTALL.txt',
        'claude-usage-bar/LICENSE',
        'claude-usage-bar/README.md',
        'claude-usage-bar/data/',
        'claude-usage-bar/plugins/.claude-plugin/marketplace.json',
        'claude-usage-bar/plugins/usage-feed/.claude-plugin/plugin.json',
        'claude-usage-bar/plugins/usage-feed/hooks/hooks.json',
        'claude-usage-bar/plugins/usage-feed/hooks/register.ts'
    )
    $sources = @{
        'claude-usage-bar/claude-usage-bar.exe' = $exePath
        'claude-usage-bar/INSTALL.txt' = (Join-Path $repo 'packaging\INSTALL.txt')
        'claude-usage-bar/LICENSE' = (Join-Path $repo 'LICENSE')
        'claude-usage-bar/README.md' = (Join-Path $repo 'README.md')
        'claude-usage-bar/plugins/.claude-plugin/marketplace.json' = (Join-Path $repo 'plugins\.claude-plugin\marketplace.json')
        'claude-usage-bar/plugins/usage-feed/.claude-plugin/plugin.json' = (Join-Path $repo 'plugins\usage-feed\.claude-plugin\plugin.json')
        'claude-usage-bar/plugins/usage-feed/hooks/hooks.json' = (Join-Path $repo 'plugins\usage-feed\hooks\hooks.json')
        'claude-usage-bar/plugins/usage-feed/hooks/register.ts' = (Join-Path $repo 'plugins\usage-feed\hooks\register.ts')
    }
    foreach ($name in $expected) {
        if ($name -eq 'claude-usage-bar/data/') {
            continue
        }
        $source = [string]$sources[$name]
        if ([string]::IsNullOrWhiteSpace($source) -or -not (Test-Path -LiteralPath $source -PathType Leaf)) {
            Fail ("source file missing: " + $source)
        }
    }

    Remove-InsideOutDir $zipPath $OutDir
    Remove-InsideOutDir $shaPath $OutDir

    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    $stream = $null
    $archive = $null
    try {
        $stream = New-Object System.IO.FileStream(
            $zipPath,
            ([System.IO.FileMode]::Create),
            ([System.IO.FileAccess]::ReadWrite),
            ([System.IO.FileShare]::None))
        $archive = New-Object System.IO.Compression.ZipArchive(
            $stream,
            ([System.IO.Compression.ZipArchiveMode]::Create),
            $false)
        foreach ($name in $expected) {
            $entry = $archive.CreateEntry($name)
            if ($name -eq 'claude-usage-bar/data/') {
                continue
            }
            $sourceStream = [System.IO.File]::Open(
                [string]$sources[$name],
                ([System.IO.FileMode]::Open),
                ([System.IO.FileAccess]::Read),
                ([System.IO.FileShare]::Read))
            try {
                $entryStream = $entry.Open()
                try {
                    $sourceStream.CopyTo($entryStream)
                } finally {
                    $entryStream.Dispose()
                }
            } finally {
                $sourceStream.Dispose()
            }
        }
    } finally {
        if ($null -ne $archive) {
            $archive.Dispose()
        }
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }

    $stream = $null
    $archive = $null
    try {
        $stream = [System.IO.File]::OpenRead($zipPath)
        $archive = New-Object System.IO.Compression.ZipArchive(
            $stream,
            ([System.IO.Compression.ZipArchiveMode]::Read),
            $false)
        $actual = @()
        foreach ($entry in $archive.Entries) {
            $actual += [string]$entry.FullName
        }
        $actualSorted = @($actual | Sort-Object)
        $expectedSorted = @($expected | Sort-Object)
        if ($actualSorted.Count -ne $expectedSorted.Count) {
            Fail ("zip entry count " + $actualSorted.Count + " != " + $expectedSorted.Count + ": [" + ($actualSorted -join ', ') + "]")
        }
        for ($i = 0; $i -lt $expectedSorted.Count; $i++) {
            if ([string]$actualSorted[$i] -cne [string]$expectedSorted[$i]) {
                Fail ("zip entries [" + ($actualSorted -join ', ') + "] != [" + ($expectedSorted -join ', ') + "]")
            }
        }
        foreach ($entry in $archive.Entries) {
            $entryName = [string]$entry.FullName
            if ($entryName -eq 'claude-usage-bar/data/') {
                if ($entry.Length -ne 0) {
                    Fail ("data/ entry length " + $entry.Length)
                }
                continue
            }
            $sourceLength = (New-Object System.IO.FileInfo ([string]$sources[$entryName])).Length
            if ($entry.Length -ne $sourceLength) {
                Fail ("length mismatch for " + $entryName + ": " + $entry.Length + " != " + $sourceLength)
            }
        }
    } finally {
        if ($null -ne $archive) {
            $archive.Dispose()
        }
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }

    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashStream = [System.IO.File]::OpenRead($zipPath)
        try {
            $hashBytes = $hasher.ComputeHash($hashStream)
        } finally {
            $hashStream.Dispose()
        }
    } finally {
        $hasher.Dispose()
    }
    $builder = New-Object System.Text.StringBuilder
    foreach ($byte in $hashBytes) {
        [void]$builder.Append($byte.ToString('x2'))
    }
    $sha = $builder.ToString()
    if ($sha -notmatch '^[0-9a-f]{64}$') {
        Fail ("unexpected sha256: " + $sha)
    }
    $shaText = $sha + '  ' + $zipName + "`n"
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($shaPath, $shaText, $utf8)

    $size = (New-Object System.IO.FileInfo $zipPath).Length
    Write-Output ("ZIP=" + $zipPath)
    Write-Output ("SHA256=" + $sha)
    Write-Output ("SIZE=" + $size)
    exit 0
} catch {
    $msg = [string]$_.Exception.Message
    if ([string]::IsNullOrWhiteSpace($msg)) {
        $msg = [string]$_
    }
    $msg = ($msg -replace '[\r\n]+', ' ').Trim()
    Write-Output ("ERROR=" + $msg)
    exit 1
} finally {
    Set-Location -LiteralPath $startLocation
}
