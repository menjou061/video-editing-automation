# -*- coding: utf-8 -*-
# JyPoll: 飞书待完成任务轮询器（常驻，无敏感信息）
# 由 schtasks 每 10 分钟触发。发现新任务 -> 生成一次性 run 脚本 -> 触发 JyRun 任务。
$ErrorActionPreference = 'Continue'
$PIPE  = [Environment]::GetEnvironmentVariable('JY_PIPE_ROOT', 'Process')
if ([string]::IsNullOrWhiteSpace($PIPE)) { $PIPE = $PSScriptRoot }
$SkillRoot = [Environment]::GetEnvironmentVariable('JY_SKILL_ROOT', 'Process')
$WORK  = Join-Path $PIPE 'work'
$STATE = Join-Path $PIPE 'state'
$LOG   = Join-Path $PIPE 'logs'
$TOOLS = Join-Path $PIPE 'tools'

function Out-Log($m) {
  $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $m
  Add-Content -LiteralPath (Join-Path $LOG 'poll.log') -Value $line -Encoding UTF8
}

New-Item -ItemType Directory -Path $WORK, $STATE, $LOG -Force | Out-Null
$LarkBaseToken = [Environment]::GetEnvironmentVariable('LARK_BASE_TOKEN', 'Process')
$LarkTableId = [Environment]::GetEnvironmentVariable('LARK_TABLE_ID', 'Process')
$NasPassword = [Environment]::GetEnvironmentVariable('NAS_PASSWORD', 'Process')
if ([string]::IsNullOrWhiteSpace($SkillRoot)) { Out-Log 'JY_SKILL_ROOT missing; queue remains stopped'; exit 2 }
if ([string]::IsNullOrWhiteSpace($LarkBaseToken)) { Out-Log 'LARK_BASE_TOKEN missing; queue remains stopped'; exit 2 }
if ([string]::IsNullOrWhiteSpace($LarkTableId)) { Out-Log 'LARK_TABLE_ID missing; queue remains stopped'; exit 2 }
if ([string]::IsNullOrWhiteSpace($NasPassword)) { Out-Log 'NAS_PASSWORD missing; queue remains stopped'; exit 2 }

# ---- 防重：执行中锁 / 已派发锁 / 运行中的 JyRun 任务（均带残留兜底）----
$lockRun = Join-Path $STATE 'RUNNING.lock'
$lockPend = Join-Path $STATE 'PENDING.lock'
# JyRun 是否正在运行（1.0.4：schtasks /v 的中文状态词经 PS 5.1 管道会按 GBK 解码损坏，
# 「正在运行」永远匹配不上 → 运行中被误判空闲 → 清锁+重派 → 双实例抢同一任务目录/Z: 盘。
# 正解：cmd 字节重定向落盘 + 按 Default(GBK) 读；再加进程树兜底：run 脚本 powershell 的命令行含 'run_'）
$jyRunning = $false
$jyStFile = Join-Path $STATE 'jyrun_status.txt'
& cmd /c ('schtasks /query /tn JyRun /v /fo list > ' + $jyStFile + ' 2>nul')
if (Test-Path -LiteralPath $jyStFile) {
  try {
    $jyTxt = [System.IO.File]::ReadAllText($jyStFile, [System.Text.Encoding]::Default)
    if ($jyTxt -match '正在运行|Running') { $jyRunning = $true }
  } catch {}
}
if (-not $jyRunning) {
  $wq = & wmic process where "name='powershell.exe'" get CommandLine /format:csv 2>$null | Out-String
  if ($wq -match 'run_') { $jyRunning = $true }
}
if (Test-Path -LiteralPath $lockRun) {
  # run 脚本执行中；锁超时 2 小时兜底（build 上限 1 小时）
  $age = ((Get-Date) - (Get-Item -LiteralPath $lockRun).LastWriteTime).TotalMinutes
  if ($jyRunning -and $age -lt 120) { exit 0 }
  Remove-Item -LiteralPath $lockRun -Force -ErrorAction SilentlyContinue
  Out-Log 'RUNNING.lock stale, cleared'
}
if (Test-Path -LiteralPath $lockPend) {
  if ($jyRunning) { Out-Log 'task in flight, skip'; exit 0 }
  Remove-Item -LiteralPath $lockPend -Force -ErrorAction SilentlyContinue
  Out-Log 'PENDING.lock stale, cleared'
}

# ---- 已处理记录 ----
# PS 5.1 没有 ConvertFrom-Json -AsHashtable：顶层转 hashtable，值保持对象
function Get-JsonHash($path) {
  $h = @{}
  try {
    $o = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($o -is [System.Collections.IDictionary]) { $h = $o }
    else { foreach ($p in $o.PSObject.Properties) { $h[$p.Name] = $p.Value } }
  } catch { $h = @{} }
  return $h
}
$processed = @{}
if (Test-Path -LiteralPath (Join-Path $STATE 'processed.json')) {
  $processed = Get-JsonHash (Join-Path $STATE 'processed.json')
}

# ---- 读飞书（无代理）----
$env:HTTPS_PROXY=''; $env:HTTP_PROXY=''; $env:ALL_PROXY=''; $env:https_proxy=''; $env:http_proxy=''; $env:all_proxy=''
# PS 5.1 传给原生命令的内联 JSON 双引号会被剥掉（lark-cli 报 invalid JSON near byte 2）；
# 中文字段名经原生命令也有 GBK 编码风险。因此：过滤条件写入 UTF-8 文件后用 --filter-json @file，
# 字段一律用 field id（数组顺序即下方行取值索引）。
$filterAFile = Join-Path $STATE 'filterA.json'
$filterBFile = Join-Path $STATE 'filterB.json'
[System.IO.File]::WriteAllText($filterAFile, '{"logic":"and","conditions":[["fldxNNEXlA","==","待完成"],["fldyPLziiz","!=","验收通过"],["fldyPLziiz","!=","已二次修改"]]}', (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllText($filterBFile, '{"logic":"and","conditions":[["fldxNNEXlA","==","已完成"],["fldailz4cg","!=",""],["fldr3pwQM8","!=",""],["fldyPLziiz","!=","验收通过"],["fldyPLziiz","!=","已二次修改"]]}', (New-Object System.Text.UTF8Encoding($false)))
$fieldIds = @('fldt6jXRlr','fldMONH983','fld2QJuihE','fldzk6X37A','fldWjlEsvJ','fldzFjx4Ge','fldX6TFNA9','fldzmyPErU','fldr3pwQM8','fldailz4cg','fldyPLziiz')
$fieldArgs = '--field-id fldt6jXRlr --field-id fldMONH983 --field-id fld2QJuihE --field-id fldzk6X37A --field-id fldWjlEsvJ --field-id fldzFjx4Ge --field-id fldX6TFNA9 --field-id fldzmyPErU --field-id fldr3pwQM8 --field-id fldailz4cg --field-id fldyPLziiz'
$outA = Join-Path $STATE 'larkA.json'
$outB = Join-Path $STATE 'larkB.json'
$raw = $null; $rids = @(); $rows = @(); $mode = ''
foreach ($fp in @(@($filterAFile,$outA,'A'), @($filterBFile,$outB,'B'))) {
  $mode = $fp[2]
  # 经 cmd /c 字节级重定向：PS 5.1 管道会把 lark-cli 的 UTF-8 输出按 GBK 解码，
  # 中文字符损坏（脚本/素材内容被破坏，ConvertFrom-Json 报错）；文件直写保真。
  # 必须先 cd /d 到 state：lark-cli --filter-json @file 有白名单（仅 CWD/临时目录/~/files），
  # schtasks 默认 CWD 是 C:\WINDOWS\system32，@file 落到 state\ 会被拒（exit 2）。
  & cmd /c ('cd /d ' + $STATE + ' && lark-cli base +record-list --base-token %LARK_BASE_TOKEN% --table-id %LARK_TABLE_ID% --json --filter-json @' + $fp[0] + ' ' + $fieldArgs + ' > ' + $fp[1] + ' 2>nul')
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $fp[1])) { Out-Log ('LARK_EXIT=' + $LASTEXITCODE + ' mode=' + $mode); continue }
  try { $d = [System.IO.File]::ReadAllText($fp[1], [System.Text.Encoding]::UTF8) | ConvertFrom-Json } catch { Out-Log ('LARK_PARSE_FAIL mode=' + $mode); continue }
  if (-not $d.ok) {
    $snip = ''
    try { $snip = ([System.IO.File]::ReadAllText($fp[1], [System.Text.Encoding]::UTF8)).Substring(0,200) } catch {}
    Out-Log ('LARK_NOT_OK mode=' + $mode + ' ' + $snip)
    continue
  }
  $rids = @($d.data.record_id_list)
  $rows = @($d.data.data)
  if ($rids.Count -gt 0) { break }
}
if ($rids.Count -eq 0) { exit 0 }

# 字段序：0 任务ID 1 视频标题 2 脚本文案 3 分镜素材 4 配音名称 5 BGM名称 6 生成条数 7 参考视频 8 草稿文件 9 问题反馈 10 成片状态
for ($i = 0; $i -lt $rids.Count; $i++) {
  $rid = [string]$rids[$i]
  $r = @($rows[$i])
  $script = if ($r.Count -gt 2) { [string]$r[2] } else { '' }
  $mat    = if ($r.Count -gt 3) { [string]$r[3] } else { '' }
  $issue  = if ($r.Count -gt 9) { [string]$r[9] } else { '' }
  $draftFile = if ($r.Count -gt 8) { [string]$r[8] } else { '' }
  $filmStatus = if ($r.Count -gt 10) { [string]$r[10] } else { '' }

  # 轮A：需 文案 + 素材；轮B（二次处理）：需 文案 + 素材 + 问题反馈
  if ($mode -eq 'B') {
    if ([string]::IsNullOrWhiteSpace($script) -or [string]::IsNullOrWhiteSpace($mat) -or [string]::IsNullOrWhiteSpace($issue)) { continue }
    if ($filmStatus -eq '验收通过' -or $filmStatus -eq '已二次修改') { continue }
  } else {
    if ([string]::IsNullOrWhiteSpace($script) -or [string]::IsNullOrWhiteSpace($mat)) { continue }
  }

  # 防重 key：轮A 用 rid；轮B 用 rid#revN（issue 不变不重复派发）
  $key = $rid
  $revVal = $null
  if ($mode -eq 'B') {
    $issueHash = ($issue.GetHashCode()).ToString()
    # 找已处理的 rid#rev* 里 issue 相同者
    $dup = $false
    $maxN = 0
    foreach ($k in $processed.Keys) {
      if ($k -like ($rid + '#*')) {
        if ($processed[$k].issue_hash -eq $issueHash) { $dup = $true }
        $n = 0
        if ($k -match 'rev(\d+)$') { try { $n = [int]$Matches[1] } catch {} }
        if ($n -gt $maxN) { $maxN = $n }
      }
    }
    if ($dup) { continue }          # 同一批问题反馈已处理过
    if ($maxN -ge 3) { continue }   # 同一行最多 3 次二次处理，超限留给 Mac 人工
    $revVal = 'rev' + ($maxN + 1)
    $key = $rid + '#' + $revVal
  } else {
    if ($processed.ContainsKey($rid)) {
      $att = 0
      try { $att = [int]$processed[$rid].attempts } catch {}
      if ($att -ge 2) { continue }   # 失败 2 次不再自动重试，留给 Mac 监控
    }
  }
  # ---- 选中该行 ----
  $feedbackScope = 'none'
  $feedbackCategory = ''
  $feedbackRuleKey = ''
  $feedbackRuleText = ''
  $feedbackClassification = $null
  if ($mode -eq 'B' -and -not [string]::IsNullOrWhiteSpace($issue)) {
    $feedbackInput = Join-Path $STATE ('feedback_' + $rid + '.txt')
    $feedbackOutput = Join-Path $STATE ('feedback_' + $rid + '.json')
    [System.IO.File]::WriteAllText($feedbackInput, $issue, (New-Object System.Text.UTF8Encoding($false)))
    $feedbackTool = Join-Path (Join-Path $PIPE 'tools') 'classify_feedback.py'
    $feedbackSkillRoot = $SkillRoot
    if (Test-Path -LiteralPath $feedbackTool) {
      & python $feedbackTool --input-file $feedbackInput --output-file $feedbackOutput --skill-root $feedbackSkillRoot 2>$null | Out-Null
      if (Test-Path -LiteralPath $feedbackOutput) {
        try {
          $feedbackClassification = Get-Content -LiteralPath $feedbackOutput -Raw -Encoding UTF8 | ConvertFrom-Json
          $feedbackScope = [string]$feedbackClassification.scope
          $feedbackCategory = [string]$feedbackClassification.category
          $feedbackRuleKey = [string]$feedbackClassification.rule_key
          $feedbackRuleText = [string]$feedbackClassification.rule_text
        } catch { Out-Log ('FEEDBACK_CLASSIFY_PARSE_FAIL ' + $rid) }
      }
    } else { Out-Log ('FEEDBACK_CLASSIFY_TOOL_MISSING ' + $rid) }
  }
  $taskId = if ($r.Count -gt 0) { [string]$r[0] } else { '' }
  if ([string]::IsNullOrWhiteSpace($taskId)) { $taskId = $rid.Substring(0, [Math]::Min(12, $rid.Length)) }
  $title  = if ($r.Count -gt 1) { [string]$r[1] } else { '' }
  $voice  = if ($r.Count -gt 4) { [string]$r[4] } else { '' }
  $bgm    = if ($r.Count -gt 5) { [string]$r[5] } else { '' }
  $count  = 1
  if ($r.Count -gt 6) { try { $count = [int]$r[6] } catch {} ; if ($count -lt 1) { $count = 1 } }
  # 素材字段是 markdown link：[UNC](http://UNC) -> 取 UNC
  $matDir = $mat
  if ($mat -match '^\[([^\]]+)\]') { $matDir = $Matches[1] }
  $matDir = $matDir.Trim()
  if (-not ($matDir -match '^\\\\')) { Out-Log ('SKIP bad material format: ' + $rid); continue }
  # 素材 UNC -> 转为 Z: 盘路径（共享名\子路径 -> Z:\子路径）
  $matZ = 'Z:\' + (($matDir.Substring(2)) -split '\\', 2)[1]

  $refVid = if ($r.Count -gt 7) { [string]$r[7] } else { '' }
  $taskDir = Join-Path $WORK $rid
  New-Item -ItemType Directory -Path $taskDir -Force | Out-Null
  $taskObj = [ordered]@{
    record_id = $rid; task_id = $taskId; title = $title; script = $script
    material_dir = $matDir; material_dir_z = $matZ; voice = $voice; bgm = $bgm; count = $count
    reference_video = $refVid; rev = $revVal; issue = $issue; draft_file = $draftFile; proc_key = $key
    film_status = $filmStatus; feedback_scope = $feedbackScope; feedback_category = $feedbackCategory; feedback_rule_key = $feedbackRuleKey; feedback_rule_text = $feedbackRuleText; feedback_classification = $feedbackClassification
  }
  $taskObj | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $taskDir 'task.json') -Encoding UTF8

  # ---- 生成一次性 run 脚本（凭据只从进程环境读取，自我删除）----
  $runSrc = Join-Path $PIPE 'run_task.template.ps1'
  if (-not (Test-Path -LiteralPath $runSrc)) { Out-Log 'RUN_TEMPLATE_MISSING'; exit 1 }
  $runDst = Join-Path $taskDir ('run_' + $rid + '.ps1')
  $body = Get-Content -LiteralPath $runSrc -Raw -Encoding UTF8
  $body = $body.Replace('__RECORD_ID__', $rid).Replace('__PIPE__', $PIPE)
  # 必须带 BOM：PS 5.1 把无 BOM 文件当 GBK 读，脚本内中文（NAS 共享名等）会被破坏
  [System.IO.File]::WriteAllText($runDst, $body, (New-Object System.Text.UTF8Encoding($true)))

  # ---- 注册并触发一次性 JyRun 任务 ----
  & schtasks /create /tn JyRun /tr "powershell -NoProfile -ExecutionPolicy Bypass -File `"$runDst`"" /sc once /st 00:00 /f 2>$null | Out-Null
  & schtasks /run /tn JyRun 2>$null | Out-Null
  Out-Log ('DISPATCH ' + $rid + ' key=' + $key + ' mode=' + $mode + ' scope=' + $feedbackScope + ' task=' + $taskId + ' title=' + $title)
  # 派发锁：run 脚本完成/失败后删除；防止同一任务重复派发
  Set-Content -LiteralPath (Join-Path $STATE 'PENDING.lock') -Value $key -Encoding UTF8
  exit 0
}
exit 0
