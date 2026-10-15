# O-Cleaner 本机自检编排（PowerShell 7）—— 逐道跑本机可执行的门禁，
# 汇总结论。完整套件的权威验证仍走 CI 的「自测 / 界面渲染检查」作业
# （本机跑完整 selftest.py 受环境限制不可行）。
# 用法: pwsh -NoProfile -ExecutionPolicy Bypass -File tools/selfcheck.ps1
$ErrorActionPreference = "Continue"
$py = "C:\Users\<user>\.workbuddy\binaries\python\versions\3.13.12\python.exe"
$root = Split-Path -Parent $PSScriptRoot
$results = @()

function Run-Step($name, $scriptblock) {
    $out = & $scriptblock 2>&1
    $rc = $LASTEXITCODE
    $tail = ($out | Select-Object -Last 3) -join " | "
    $script:results += [pscustomobject]@{ Step = $name; Rc = $rc; Tail = $tail }
    Write-Output ("[{0}] {1}  rc={2}" -f ($(if ($rc -eq 0) {"OK"} else {"FAIL"})), $name, $rc)
    if ($rc -ne 0) { $out | Select-Object -Last 6 | ForEach-Object { Write-Output "    $_" } }
}

Run-Step "py_compile 全部 .py" {
    & $py -m py_compile (Get-ChildItem $root -Filter *.py -File).FullName `
                        (Get-ChildItem "$root\tools" -Filter *.py -File).FullName | Out-Null
}
Run-Step "doctor 环境自检" {
    Set-Location $root; & $py tools\doctor.py | Out-Null
}
Run-Step "lint_rules --strict" {
    & $py "$root\tools\lint_rules.py" --strict | Out-Null
}
Run-Step "_lint_selftest 缺陷注入" {
    & $py "$root\tools\_lint_selftest.py" | Out-Null
}
Run-Step "编码与换行契约检查" {
    Set-Location $root
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\fix-encoding.ps1 -Check | Out-Null
}
Run-Step "JS 语法检查（cdp_check + probe_ui）" {
    $node = "C:\Users\<user>\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
    & $node --check "$root\tools\cdp_check.js"; if ($LASTEXITCODE -ne 0) { throw "cdp_check.js" }
    & $node --check "$root\tools\probe_ui.js"; if ($LASTEXITCODE -ne 0) { throw "probe_ui.js" }
}

Write-Output ""
Write-Output "===== 自检汇总 ====="
$results | Format-Table Step, Rc, Tail -AutoSize -Wrap
$failed = @($results | Where-Object { $_.Rc -ne 0 })
Write-Output ("结论：" + $(if ($failed.Count -eq 0) { "全部通过（" + $results.Count + " 道）" } else { $failed.Count.ToString() + " 道失败" }))
