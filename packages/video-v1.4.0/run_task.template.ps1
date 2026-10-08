# -*- coding: utf-8 -*-
# JyRun 一次性执行器（由 jy_poll 生成；凭据从进程环境读取；finally 自我删除）
# 占位符：__RECORD_ID__ / __PIPE__
$ErrorActionPreference = 'Continue'
$RID  = '__RECORD_ID__'
$PIPE = [Environment]::GetEnvironmentVariable('JY_PIPE_ROOT', 'Process')
if ([string]::IsNullOrWhiteSpace($PIPE)) { $PIPE = '__PIPE__' }
$skillRoot = [Environment]::GetEnvironmentVariable('JY_SKILL_ROOT', 'Process')
if ([string]::IsNullOrWhiteSpace($skillRoot)) { $skillRoot = Join-Path $PIPE 'doubao-jianying-orchestrator' }
$draftRoot = [Environment]::GetEnvironmentVariable('JY_DRAFT_ROOT', 'Process')
if ([string]::IsNullOrWhiteSpace($draftRoot)) { $draftRoot = Join-Path $PIPE 'drafts' }
$WORK = Join-Path $PIPE 'work'
$STATE = Join-Path $PIPE 'state'
$LOGS = Join-Path $PIPE 'logs'
$TASK = Join-Path $WORK (Join-Path $RID 'task.json')
$PROC_FILE = Join-Path $STATE 'processed.json'   # 勿改名/勿加 $proc* 变量：PS 5.1 变量名大小写不敏感，$PROC 与 $proc 是同一个变量（1.0.3 血泪）
$LOCK = Join-Path $STATE 'RUNNING.lock'
$PEND = Join-Path $STATE 'PENDING.lock'

function Out-Log($m) {
  $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' [' + $RID + '] ' + $m
  Add-Content -LiteralPath (Join-Path $LOGS 'run.log') -Value $line -Encoding UTF8
}

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

$netUseOk = $false
try {
  Set-Content -LiteralPath $LOCK -Value $RID -Encoding UTF8
  Out-Log 'RUN_START'

  # ---- NAS 挂载（同一登录会话，Codex 子进程可见 Z:）----
  & net use Z: /delete /y 2>$null | Out-Null
  $nasPassword = [Environment]::GetEnvironmentVariable('NAS_PASSWORD', 'Process')
  if ([string]::IsNullOrWhiteSpace($nasPassword)) { throw 'NAS_PASSWORD_MISSING' }
  $nasShare = [Environment]::GetEnvironmentVariable('JY_NAS_SHARE', 'Process')
  $nasUser = [Environment]::GetEnvironmentVariable('JY_NAS_USER', 'Process')
  if ([string]::IsNullOrWhiteSpace($nasShare) -or [string]::IsNullOrWhiteSpace($nasUser)) { throw 'JY_NAS_CONFIG_MISSING' }
  net use Z: $nasShare /user:$nasUser $nasPassword | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'NET_USE_FAILED' }
  $netUseOk = $true
  Out-Log 'NAS_MOUNTED'

  # ---- 环境（Codex 及其子进程继承）----
  $env:PYTHONUTF8 = '1'
  $env:HTTPS_PROXY=''; $env:HTTP_PROXY=''; $env:ALL_PROXY=''; $env:https_proxy=''; $env:http_proxy=''; $env:all_proxy=''
  $env:JY_CLOSED_LOOP_POLICY = Join-Path $PIPE 'WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json'
  $env:JY_MAC_REVIEW_REQUIRED = '1'
  # Keep the operator's original table label available to the engine.  The
  # LLM may prepare a manifest, but it is not allowed to replace this source
  # value with a guessed/fallback speaker.
  try {
    $operatorTask = Get-Content -LiteralPath $TASK -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($operatorTask.voice) { $env:JY_OPERATOR_VOICE = [string]$operatorTask.voice }
  } catch {}

  # ---- Codex 执行（无人值守；90 分钟超时保护）----
  $cdir = Join-Path $WORK $RID

  # 视觉子进程优先走 CC Switch 当前 CodingPlan。该 provider 是
  # Anthropic Messages 协议，vision_analyzer 通过本地受限 env 文件调用；
  # 没有配置文件时才回退到流水线原有 Codex profile。
  $ccSwitchVision = [Environment]::GetEnvironmentVariable('JY_VISION_ENV_FILE', 'Process')
  if (Test-Path -LiteralPath $ccSwitchVision) {
    $env:JY_VISION_TRANSPORT = 'anthropic'
    $env:JY_VISION_ANTHROPIC_ENV = $ccSwitchVision
    $env:JY_VISION_MODEL = 'glm-5.3-flash'
  } else {
    $env:JY_VISION_PROFILE = 'flash'
  }

  # 版本号从**运行时实际文件**读取，不写死在提示词里。
  # 起因：tools/promote_feedback.py 的幂等沉淀只自动改写 `skill_version=X.Y.Z` 字面量，
  # 从不维护 rules_version 那一句，于是提示词长期停在 1.3.3 而 RULES.md 早已 1.3.6，
  # 运行时反复告警 RULES_VERSION_PROMPT_MISMATCH，成片无法自证用的是哪版规则。
  # 改为两侧都按实际文件注入后，这一类漂移不再可能发生。
  $skillVer = 'unknown'
  $vf = Join-Path $skillRoot 'VERSION'
  if (Test-Path -LiteralPath $vf) { $skillVer = (Get-Content -LiteralPath $vf -Raw -Encoding UTF8).Trim() }
  $rulesVer = 'unknown'
  $rf = Join-Path $PIPE 'RULES.md'
  if (Test-Path -LiteralPath $rf) {
    $rm = Select-String -LiteralPath $rf -Pattern '^version:\s*([0-9][0-9.]*)' | Select-Object -First 1
    if ($rm) { $rulesVer = $rm.Matches[0].Groups[1].Value }
  }

  $prompt = @'
你是剪映混剪生产流水线执行者，无人值守环境。严格按序执行，禁止跳过、禁止交互：
1. 读 __PIPE_ROOT__\RULES.md 和 __PIPE_ROOT__\WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json（全量规则、视觉匹配合同与红线，逐条遵守；JSON 是自动化执行参数，不能只读文档不落实）
2. 读当前目录 task.json（本次任务；script 为运营原文禁止改动；manifest.voice 必须逐字复制 task.json 的 voice，禁止自行改成相近音色或 fallback）
3. 只处理当前 task.json 的 material_dir：先枚举本商品视频并在 manifest 写入 material_sources、temporary_material_analysis=true、vision_analysis=true（不要读历史素材库；不要用脚本文案替素材编造标签）。运行 `python __SKILL_ROOT__\run.py analyze-shots --input <manifest> --report-dir <report_dir>`，引擎会自动切镜头、抽取中间/末帧、生成带标签 contact sheet，并用 `codex exec --image` 对当前商品素材逐批生成真实 description/visual_tags/evidence_tags/action_complete/evidence_intervals；不得手工回填、不得用 `frame_visible` 占位。将 manifest.shot_analysis_file 指向生成的 shot_candidates.json，再执行 run.py build；匹配必须扫描当前商品的全候选池，24 只是最终上限，镜头数由脚本语义段与有效匹配决定；顺序固定为 DIRECT → 相关产品/结果特写降级 → MATERIAL_GAP。降级候选不得抢占后续语义段的唯一直接候选；CTA/划算/囤货/数量必须有多包、成排、整箱、堆叠或可见数量，单包特写不能作结尾 CTA；包装文字只有明确指向可读同一卖点时才可标 PACKAGING_CLAIM_DIRECT，不能证明真实性或功能效果。最终 source_timerange 必须覆盖动作/文字证据窗口，错误只修复对应分镜并复用已缓存素材理解。所有字幕必须在画布内显示，字号 8、加粗、X=0/Y=-0.8，并以渲染帧验收；结构检查不能代替画面验收。视觉模型调用失败、返回字段不完整或无法确认动作时，当前片段必须保持 pending 并记录 VISUAL_EVIDENCE_REQUIRED/SHOT_MATCH_BLOCKED；但没有直接动作/结果证据不等于整片失败，按 RULES.md 先尝试相关产品部位/结果特写降级，仍无合适候选才记录 MATERIAL_GAP/留空。最终切片未覆盖所选证据时只修复对应片段或改用候选，不得用无关镜头充数。用引擎 run.py build 生成剪映草稿（本地 __DRAFT_ROOT__）
4. 先完成独立 draft_visual_qc.py 与包完整性检查，再生成 Mac 检查包并等待 task 目录下的 mac_review_approved.json；未批准不得 NAS 分发或表格回写。批准后才调用 skill 内 orchestrator\\ship_distribute.py --style unc；禁止使用已退役 ship_portable。count>1 时统一归档到一个父文件夹下的 版本1/版本2/...，表格只写父文件夹；新包通过后才删除并替换旧交付包，不能先删旧包，也不能删除原素材
5. 写生产日志到 NAS 测试产出\生产日志\；日志必须包含 skill_version=__SKILL_VERSION__、rules_version=__RULES_VERSION__（这两个版本号已由 run 脚本按运行时实际文件注入，照抄即可，不要自行猜测或改写）、voice_field（运营填写原名）、voice_used（实际 speaker_id/剪映显示名/后端）、shot_candidates、shot_match_report、shot_match_ai、visual_evidence_report、pending_items、字幕门禁结果和稳定切片窗口信息
6. 成功才通过 tools\\closed_loop.py 的 audit → archive-parent → table-update 回写飞书；只写 草稿文件/任务状态/成片状态 三个业务字段，更新时间由系统自动维护。只有 PASS、Mac 已批准、NAS 包完整、路径可移植且 bat/fix_local 存在时才允许回写；PASS_WITH_GAPS/UNCERTIFIED/超时都只能预览或待复核
7. 写 codex_done.json（status/成品路径/耗时/warnings/失败原因）
8. 最后一行输出 SUMMARY status=SUCCESS 或 status=FAILED 及原因
失败路径：任何步骤失败 -> 生产日志写 FAILED，飞书不回写，codex_done.json status=failed。不要反复重试 build 超过 1 次。
9. 如果 task.json.rev 非空，这是轮B二次处理：必须先读取 task.json.issue、feedback_scope、feedback_category 和 draft_file。问题反馈是运营原文，禁止改写；先按问题反馈定位并只替换错误分镜，重新核对源窗口、字幕和视觉 QC，再生成新的 -rev / -rev2 包。新包未通过结构、视觉、路径、Mac 检查前不得删除旧包；通过后才替换旧交付包并按规则回写三字段。
10. 如果 feedback_scope=generic，问题修复成功后在当前 work 目录写 feedback_resolution.json：status=resolved、rule_key、rule_text、evidence；如果没有真正完成修复，写 status=unresolved。不要直接改版本文件，父流程会在成功后做幂等版本沉淀。

'@
  $prompt = $prompt.Replace('__SKILL_VERSION__', $skillVer).Replace('__RULES_VERSION__', $rulesVer).Replace('__PIPE_ROOT__', $PIPE).Replace('__SKILL_ROOT__', $skillRoot).Replace('__DRAFT_ROOT__', $draftRoot)
  Set-Content -LiteralPath (Join-Path $cdir 'prompt.txt') -Value $prompt -Encoding UTF8
  $codexLog = Join-Path $cdir 'codex.log'
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  # codex 是 npm 的 .cmd shim，Start-Process 不能直接跑 .cmd（BAD_EXE_FORMAT），必须经 cmd /c
  # 模型配置由部署环境注入；本模板不读取或保存任何账号凭据。
  # 回退：把 flash 改回 aijws 即走旧中转（模板备份 run_task.template.ps1.bak_aijws_<TS>）
  $p = Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c','codex exec -p flash --dangerously-bypass-approvals-and-sandbox') -WorkingDirectory $cdir -RedirectStandardInput (Join-Path $cdir 'prompt.txt') -RedirectStandardOutput $codexLog -RedirectStandardError (Join-Path $cdir 'codex.err.log') -PassThru -NoNewWindow
  $timeout = 5400  # 90 分钟
  $finished = $p.WaitForExit($timeout * 1000)
  if (-not $finished) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; throw 'CODEX_TIMEOUT' }
  $sw.Stop()
  Out-Log ('CODEX_EXIT=' + $p.ExitCode + ' WALL_S=' + [math]::Round($sw.Elapsed.TotalSeconds,1))

  # ---- 结果判定 ----
  $doneFile = Join-Path $cdir 'codex_done.json'
  $doneOk = $false
  $doneStatus = 'unknown'
  if (Test-Path -LiteralPath $doneFile) {
    try {
      $dj = Get-Content -LiteralPath $doneFile -Raw -Encoding UTF8 | ConvertFrom-Json
      $doneStatus = [string]$dj.status
      $doneOk = ($doneStatus -eq 'SUCCESS')
    } catch { $doneOk = $false }
  }

  # Rule promotion is intentionally not automatic. Resolved feedback stays a
  # candidate for human review and cannot mutate the released tool or policy.

  # ---- 更新 processed.json（防重）----
  $proc = @{}
  if (Test-Path -LiteralPath $PROC_FILE) { $proc = Get-JsonHash $PROC_FILE }
  $key = $RID
  $issueHash = ''
  if ($TASK -and (Test-Path -LiteralPath $TASK)) {
    $t = Get-Content -LiteralPath $TASK -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($t.proc_key) { $key = [string]$t.proc_key }
    if ($t.issue) { $issueHash = ([string]$t.issue).GetHashCode().ToString() }
  }
  $prev = @{ status = 'unknown'; attempts = 0; at = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') }
  if ($proc.ContainsKey($key)) { $prev = $proc[$key] }
  $att = 0
  try { $att = [int]$prev.attempts } catch {}
  $proc[$key] = [ordered]@{ status = $doneStatus; attempts = ($att + 1); at = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'); issue_hash = $issueHash }
  $proc | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $PROC_FILE -Encoding UTF8
  Out-Log ('DONE status=' + $doneStatus + ' done_file=' + $doneOk)
} catch {
  Out-Log ('RUN_ERROR ' + $_.Exception.Message)
  try {
    $proc2 = @{}
    if (Test-Path -LiteralPath $PROC_FILE) { $proc2 = Get-JsonHash $PROC_FILE }
    $prev2 = @{ attempts = 0 }
    if ($proc2.ContainsKey($RID)) { $prev2 = $proc2[$RID] }
    $a2 = 0
    try { $a2 = [int]$prev2.attempts } catch {}
    $proc2[$RID] = [ordered]@{ status = 'ERROR'; attempts = ($a2 + 1); at = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') }
    $proc2 | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $PROC_FILE -Encoding UTF8
  } catch {}
} finally {
  if ($netUseOk) { & net use Z: /delete /y 2>$null | Out-Null }
  Remove-Item -LiteralPath $LOCK -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $PEND -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
  Out-Log 'RUN_END'
}
