<#
  make-package.ps1 —— 把 O-Cleaner 打包成可拷贝到其它电脑直接运行的 zip

  用法:
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\make-package.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\make-package.ps1 -OutDir "D:\shared"

  说明:
    * 只打包运行必需的文件，自动排除运行数据与缓存
    * .bat 会被强制转成 CRLF + 纯 ASCII（cmd.exe 对 LF 换行和 UTF-8 中文支持很差）
    * 所有 .ps1/.py/.json/.html/.md 统一转成 UTF-8 BOM + CRLF，
      保证在记事本、PowerShell 5.1、中文 Windows 上都不乱码
#>
[CmdletBinding()]
param(
    [string]$OutDir = "",
    [switch]$KeepStaging
)

$ErrorActionPreference = 'Stop'
$Root     = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$AppName  = "O-Cleaner"
$OutDir   = if ($OutDir) { $OutDir } else { Join-Path $Root "dist" }
$StageDir = Join-Path $env:TEMP ("ocpkg_" + [Guid]::NewGuid().ToString("N").Substring(0, 8))

Write-Host ""
Write-Host "  O-Cleaner 打包" -ForegroundColor Cyan
Write-Host ("  源目录: {0}" -f $Root)
Write-Host ("  输出到: {0}" -f $OutDir)
Write-Host ""

# ---------- 1. 收集要打包的文件 ----------
$include = @(
    "app.py",
    "start-ocleaner.bat",
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "HANDOFF.md",
    "config\drives.json",
    "config\targets.json",
    "config\convert.json",
    "config\filetypes.json",
    "config\game-names.json",
    "web\index.html",
    "tools\selftest.py",
    "tools\lint_rules.py",
    "tools\_lint_selftest.py",
    "tools\doctor.py",
    "tools\cdp_check.js",
    "tools\fix-encoding.ps1",
    "tools\make-package.ps1"
)
# 明确不打包：history.json / schedule.json / custom-rules.json / __pycache__

New-Item -ItemType Directory -Path $StageDir -Force | Out-Null
$packed = @()
foreach ($rel in $include) {
    $src = Join-Path $Root $rel
    if (-not (Test-Path -LiteralPath $src)) {
        Write-Host ("  [跳过] 不存在: {0}" -f $rel) -ForegroundColor DarkGray
        continue
    }
    $dst = Join-Path $StageDir $rel
    $dstDir = Split-Path -Parent $dst
    if (-not (Test-Path -LiteralPath $dstDir)) { New-Item -ItemType Directory -Path $dstDir -Force | Out-Null }
    Copy-Item -LiteralPath $src -Destination $dst -Force
    $packed += $rel
}

# ---------- 2. 规范化编码与换行 ----------
# 编码规则只有一处定义：tools\fix-encoding.ps1（它自己也用 -Check 做校验）。
# 早期这里另写了一套规则，结果和 fix-encoding.ps1 不一致，导致「打包产物不合规」。
# 现在直接调用它，避免规则重复。
$fixer = Join-Path $Root "tools\fix-encoding.ps1"
if (-not (Test-Path -LiteralPath $fixer)) {
    Write-Host "  [错误] 缺少 tools\fix-encoding.ps1（编码规则定义在这里）" -ForegroundColor Red
    exit 1
}
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $fixer -Root $StageDir | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [错误] 编码规范化失败" -ForegroundColor Red
    exit 1
}

# 校验：规范化之后不应再有偏离，否则打包产物不合规
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $fixer -Root $StageDir -Check | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [错误] 暂存目录仍存在编码/换行偏离，已中止打包" -ForegroundColor Red
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $fixer -Root $StageDir -Check
    exit 1
}
Write-Host "  编码与换行已按统一规则规范化并复核通过" -ForegroundColor Green

# ---------- 3. 校验关键文件 ----------
$appPy = Join-Path $StageDir "app.py"
# 把 .pyc 写到临时目录，避免在暂存目录里生成 __pycache__ 被一起打包
$pycTmp = Join-Path $env:TEMP ("ocpyc_" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $pycTmp -Force | Out-Null
$compile = & python -c "import py_compile,sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True); print('ok')" $appPy (Join-Path $pycTmp "app.pyc") 2>&1
Remove-Item -LiteralPath $pycTmp -Recurse -Force -ErrorAction SilentlyContinue
if ($compile -notmatch 'ok') {
    Write-Host "  [错误] 打包后的 app.py 编译失败：" -ForegroundColor Red
    Write-Host $compile
    exit 1
}
Write-Host "  app.py 语法校验通过" -ForegroundColor Green

# 兜底：清掉任何 __pycache__ / .pyc，它们不该进压缩包
Get-ChildItem -LiteralPath $StageDir -Recurse -Force -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq "__pycache__" } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
Get-ChildItem -LiteralPath $StageDir -Recurse -Force -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }

$fail = @()
foreach ($b in @("start-ocleaner.bat")) {
    $bp = Join-Path $StageDir $b
    if (Test-Path $bp) {
        $bytes = [System.IO.File]::ReadAllBytes($bp)
        $s = [System.Text.Encoding]::ASCII.GetString($bytes)
        $crlf = ([regex]::Matches($s, "`r`n")).Count
        $lone = ([regex]::Matches($s, "(?<!`r)`n")).Count
        if ($lone -gt 0) { $fail += "$b 存在 $lone 个孤立 LF（cmd.exe 会解析错乱）" }
        else { Write-Host ("  {0}: CRLF={1} 孤立LF=0 非ASCII={2}" -f $b, $crlf,
                (($bytes | Where-Object { $_ -gt 127 }).Count)) -ForegroundColor Green }
    }
}
if ($fail.Count -gt 0) {
    Write-Host "  [错误] 编码校验未通过：" -ForegroundColor Red
    $fail | ForEach-Object { Write-Host "    $_" }
    exit 1
}

# ---------- 4. 打包 zip ----------
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
$ver = "2.1"
$zip = Join-Path $OutDir ("{0}-v{1}.zip" -f $AppName, $ver)
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }

# 让压缩包内顶层就是 O-Cleaner 目录
$wrap = Join-Path $env:TEMP ("ocwrap_" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
$wrapApp = Join-Path $wrap $AppName
New-Item -ItemType Directory -Path $wrapApp -Force | Out-Null
Copy-Item -Path (Join-Path $StageDir "*") -Destination $wrapApp -Recurse -Force
Compress-Archive -Path $wrapApp -DestinationPath $zip -CompressionLevel Optimal
Remove-Item -LiteralPath $wrap -Recurse -Force

$zi = Get-Item -LiteralPath $zip
Write-Host ""
Write-Host ("  压缩包: {0}" -f $zi.FullName) -ForegroundColor Cyan
Write-Host ("  大小  : {0:N1} KB" -f ($zi.Length / 1KB))
Write-Host ""
Write-Host "  包含文件:" -ForegroundColor Gray
Add-Type -AssemblyName System.IO.Compression.FileSystem
$a = [System.IO.Compression.ZipFile]::OpenRead($zip)
$a.Entries | Sort-Object FullName | ForEach-Object {
    Write-Host ("    {0,-44} {1,8:N1} KB" -f $_.FullName, ($_.Length / 1KB))
}
$a.Dispose()

if (-not $KeepStaging) { Remove-Item -LiteralPath $StageDir -Recurse -Force }
else { Write-Host ("  暂存目录保留在: {0}" -f $StageDir) -ForegroundColor DarkGray }
Write-Host ""
Write-Host "  在别的电脑上：解压 -> 确认已装 Python 3 -> 双击 start-ocleaner.bat" -ForegroundColor Green
Write-Host ""
