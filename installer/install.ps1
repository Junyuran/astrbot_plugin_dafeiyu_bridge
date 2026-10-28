# 大肥鱼一键安装器
#
# 不包含 NapCat 本体（NapCat 的许可证禁止再分发），只做「自动化配对」：
#   1. 找到 AstrBot 实例，装上记忆桥插件
#   2. 找到你已装好的 NapCat，读出 QQ 号，写好快速登录
#   3. 把插件配置填好（自带中枢 + 自动拉起 NapCat）
#   4. 在桌面建一个「大肥鱼」快捷方式
#
# 用法：
#   install.cmd                     交互式
#   install.cmd -DryRun             只打印计划，不改任何东西

param(
    [string]$AstrBotPlugins = "",
    [string]$NapCatDir = "",
    [string]$Uin = "",
    [string]$ApiKey = "",
    [switch]$NoShortcut,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$pluginName = "astrbot_plugin_dafeiyu_bridge"

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Step($n, $text) { Write-Host ""; Write-Host "[$n] $text" -ForegroundColor Cyan }

# ---------- 1. 找插件源码 ----------
Step 1 "定位插件源码"
$candidates = @(
    (Join-Path $PSScriptRoot $pluginName),   # 一键包：install.ps1 旁边就是插件目录
    (Join-Path $PSScriptRoot ".."),          # 仓库内：installer/ 的上一层就是插件目录
    (Join-Path $PSScriptRoot "..\$pluginName")
)
$src = $null
foreach ($c in $candidates) {
    if (Test-Path (Join-Path $c "main.py")) { $src = (Resolve-Path $c).Path; break }
}
if (-not $src) {
    Say "找不到插件目录（应该有 main.py）。请把整个压缩包解压后再运行。" "Red"
    exit 1
}
Say "  $src"

# ---------- 2. 找 AstrBot ----------
Step 2 "定位 AstrBot 实例"
if (-not $AstrBotPlugins) {
    $instRoot = Join-Path $env:USERPROFILE ".astrbot_launcher\instances"
    $inst = Get-ChildItem $instRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName "core\data\plugins") } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($inst) {
        $AstrBotPlugins = Join-Path $inst.FullName "core\data\plugins"
    }
}
if (-not $AstrBotPlugins -or -not (Test-Path $AstrBotPlugins)) {
    Say "没找到 AstrBot。请先装 AstrBot 并启动过一次，再来跑这个。" "Red"
    Say "也可以手动指定：install.cmd -AstrBotPlugins `"C:\...\core\data\plugins`"" "Yellow"
    exit 1
}
Say "  $AstrBotPlugins"

# ---------- 3. 找 NapCat ----------
Step 3 "定位 NapCat"
if (-not $NapCatDir) {
    $roots = @("F:\NapCat", "D:\NapCat", "E:\NapCat", "C:\NapCat", (Join-Path $env:USERPROFILE "NapCat"))
    $hits = @()
    foreach ($r in $roots) {
        if (-not (Test-Path $r)) { continue }
        $hits += Get-ChildItem $r -Recurse -Filter "NapCatWinBootMain.exe" -ErrorAction SilentlyContinue
    }
    # 同一个目录里可能有好几份拷贝；优先选「有 versions 子目录、且有 onebot11 配置」的那个
    $ranked = $hits | ForEach-Object {
        $dir = $_.DirectoryName
        $cfgCount = 0
        $vers = Join-Path $dir "versions"
        if (Test-Path $vers) {
            $cfgCount = (Get-ChildItem $vers -Recurse -Filter "onebot11_*.json" -ErrorAction SilentlyContinue).Count
        }
        [pscustomobject]@{ Dir = $dir; Cfg = $cfgCount; HasVersions = (Test-Path $vers) }
    } | Sort-Object -Property @{ Expression = "Cfg"; Descending = $true }, @{ Expression = "HasVersions"; Descending = $true }
    if ($ranked) { $NapCatDir = $ranked[0].Dir }
}
if ($NapCatDir -and (Test-Path (Join-Path $NapCatDir "NapCatWinBootMain.exe"))) {
    Say "  $NapCatDir"
} else {
    $NapCatDir = ""
    Say "  没找到 NapCat —— 插件照样能装，只是不会自动拉起它。" "Yellow"
    Say "  装上 NapCat 后可以在插件设置里补这条路径。" "Yellow"
}

# ---------- 4. 读 QQ 号 ----------
Step 4 "读取机器人 QQ 号"
if (-not $Uin -and $NapCatDir) {
    $cfg = Get-ChildItem (Join-Path $NapCatDir "versions") -Recurse -Filter "onebot11_*.json" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($cfg) { $Uin = ($cfg.BaseName -replace "^onebot11_", "") }
}
if ($Uin) { Say "  $Uin" } else { Say "  没读到（可稍后在插件设置里填）" "Yellow" }

if ($DryRun) {
    Say ""
    Say "== DryRun：以上是计划，什么都没改 ==" "Yellow"
    exit 0
}

# ---------- 5. 安装插件 ----------
Step 5 "安装插件"
$dst = Join-Path $AstrBotPlugins $pluginName
if (Test-Path $dst) {
    $bak = "$dst.bak." + (Get-Date -Format "yyyyMMdd-HHmmss")
    Move-Item $dst $bak
    Say "  旧版本备份为 $(Split-Path $bak -Leaf)"
}
$args = @($src, $dst, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/XD", ".git", "data", "logs", "__pycache__", "/XF", "*.pyc")
& robocopy @args | Out-Null
if ($LASTEXITCODE -ge 8) {
    Copy-Item $src $dst -Recurse -Force
    Get-ChildItem $dst -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
Say "  -> $dst"

# ---------- 6. 写插件配置 ----------
Step 6 "写入插件配置"
$dataRoot = Split-Path $AstrBotPlugins -Parent          # ...\core\data
$confDir = Join-Path $dataRoot "config"
if (-not (Test-Path $confDir)) { New-Item -ItemType Directory -Force -Path $confDir | Out-Null }
$confPath = Join-Path $confDir "astrbot_plugin_dafeiyu_bridge_config.json"

$conf = [ordered]@{
    embedded            = $true
    embedded_db_path    = ""
    brain_url           = "http://127.0.0.1:8787"
    upstream_base_url   = "https://api.deepseek.com"
    upstream_model      = "deepseek-v4-flash"
    upstream_api_key    = ""
    token               = ""
    person_id           = "owner"
    owner_qq_ids        = $Uin
    only_private        = $true
    top_k               = 5
    inject_enabled      = $true
    ingest_enabled      = $true
    timeout             = 20
    diag_enabled        = $true
    debug               = $false
    napcat_autostart    = [bool]$NapCatDir
    napcat_dir          = $NapCatDir
    napcat_uin          = $Uin
}
if (Test-Path $confPath) {
    try {
        $old = Get-Content $confPath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($k in $conf.Keys.Clone()) {
            if ($null -ne $old.$k -and $old.$k -ne "") { $conf[$k] = $old.$k }
        }
    } catch { }
}
if ($ApiKey) { $conf["upstream_api_key"] = $ApiKey }

$json = ($conf | ConvertTo-Json -Depth 5)
[IO.File]::WriteAllText($confPath, $json, (New-Object Text.UTF8Encoding($false)))
Say "  -> $confPath"
if (-not $conf["upstream_api_key"]) {
    Say "  注意：还没填 API Key，装完要在面板里补上（否则只能注入记忆、不记录新的）" "Yellow"
}

# ---------- 7. NapCat 快速登录 ----------
if ($NapCatDir -and $Uin) {
    Step 7 "配置 NapCat 快速登录"
    $quick = Join-Path $NapCatDir "napcat.quick.bat"
    if (Test-Path $quick) {
        Copy-Item $quick "$quick.bak" -Force
    }
    $body = "@echo off`r`nchcp 65001 >nul`r`ncd /d `"%~dp0`"`r`n.\NapCatWinBootMain.exe $Uin`r`npause`r`n"
    [IO.File]::WriteAllText($quick, $body, [Text.Encoding]::ASCII)
    Say "  -> $quick（快速登录 $Uin）"
}

# ---------- 8. 桌面快捷方式 ----------
if (-not $NoShortcut) {
    Step 8 "创建桌面快捷方式"
    $lnkTarget = $null
    $deskLnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "AstrBot Launcher.lnk"
    if (Test-Path $deskLnk) {
        $ws0 = New-Object -ComObject WScript.Shell
        $lnkTarget = $ws0.CreateShortcut($deskLnk).TargetPath
    }
    if ($lnkTarget -and (Test-Path $lnkTarget)) {
        $out = Join-Path ([Environment]::GetFolderPath("Desktop")) "大肥鱼（AstrBot）.lnk"
        $ws = New-Object -ComObject WScript.Shell
        $s = $ws.CreateShortcut($out)
        $s.TargetPath = $lnkTarget
        $s.Description = "启动 AstrBot（会自动拉起 NapCat 和记忆中枢）"
        $s.Save()
        Say "  -> $out"
    } else {
        Say "  没找到 AstrBot 快捷方式，跳过（直接打开 AstrBot 也一样）" "Yellow"
    }
}

Say ""
Say "================================================" "Green"
Say " 装完了。下一步：" "Green"
Say "   1. 重启 AstrBot"
Say "   2. 面板 → 插件 → 大肥鱼记忆桥 → 填 upstream_api_key"
Say "   3. QQ 私聊发：桥状态"
Say "================================================" "Green"
