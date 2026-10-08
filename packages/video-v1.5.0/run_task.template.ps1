# -*- coding: utf-8 -*-
# JyRun 一次性执行器（由 jy_poll 生成；凭据从进程环境读取；finally 自我删除）
# 占位符：__RECORD_ID__ / __PIPE__
$ErrorActionPreference = 'Continue'
$RID  = '__RECORD_ID__'
$PIPE = [Environment]::GetEnvironmentVariable('JY_PIPE_ROOT', 'Process')
if ([string]::IsNullOrWhiteSpace($PIPE)) { $PIPE = '__PIPE__' }
$configLoader = Join-Path $PIPE 'tools\load-runtime-config.ps1'
if (Test-Path -LiteralPath $configLoader) { & $configLoader -PackageRoot $PIPE | Out-Null }
$configuredPipe = [Environment]::GetEnvironmentVariable('JY_PIPE_ROOT', 'Process')
if (-not [string]::IsNullOrWhiteSpace($configuredPipe)) { $PIPE = $configuredPipe }
$skillRoot = [Environment]::GetEnvironmentVariable('JY_SKILL_ROOT', 'Process')
if ([string]::IsNullOrWhiteSpace($skillRoot)) { $skillRoot = Join-Path $PIPE 'doubao-jianying-orchestrator' }
$draftRoot = [Environment]::GetEnvironmentVariable('JY_DRAFT_ROOT', 'Process')
if ([string]::IsNullOrWhiteSpace($draftRoot)) { $draftRoot = Join-Path $PIPE 'drafts' }
$WORK = [Environment]::GetEnvironmentVariable('JY_WORK_ROOT', 'Process')
if ([string]::IsNullOrWhiteSpace($WORK)) { $WORK = Join-Path $PIPE 'work' }
$STATE = [Environment]::GetEnvironmentVariable('JY_STATE_ROOT', 'Process')
if ([string]::IsNullOrWhiteSpace($STATE)) { $STATE = Join-Path $PIPE 'state' }
$LOGS = [Environment]::GetEnvironmentVariable('JY_LOG_ROOT', 'Process')
if ([string]::IsNullOrWhiteSpace($LOGS)) { $LOGS = Join-Path $PIPE 'logs' }
$env:JY_PIPE_ROOT = $PIPE
$env:JY_WORK_ROOT = $WORK
$env:JY_STATE_ROOT = $STATE
$env:JY_LOG_ROOT = $LOGS
$env:JY_SKILL_ROOT = $skillRoot
$env:JY_DRAFT_ROOT = $draftRoot
$TASK = Join-Path $WORK (Join-Path $RID 'task.json')
$PROC_FILE = Join-Path $STATE 'processed.json'   # 勿改名/勿加 $proc* 变量：PS 5.1 变量名大小写不敏感，$PROC 与 $proc 是同一个变量（1.0.3 血泪）
$LOCK = Join-Path $STATE 'RUNNING.lock'
$PEND = Join-Path $STATE 'PENDING.lock'
$generationStarted = $false

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

function Publish-FailureNotification($reason) {
  # The worker normally writes done.json itself.  This standalone JyRun path
  # must also report a failure when Codex exits before the worker finalizer.
  $taskFile = Join-Path $WORK (Join-Path $RID 'task.json')
  $doneFile = Join-Path $WORK (Join-Path $RID 'done.json')
  $tool = Join-Path $PIPE 'tools\closed_loop.py'
  $policy = Join-Path $PIPE 'WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json'
  if (!(Test-Path -LiteralPath $tool) -or !(Test-Path -LiteralPath $policy)) {
    Out-Log 'FAILURE_NOTIFICATION_DEFERRED tool_or_policy_missing'
    return
  }
  if (!(Test-Path -LiteralPath $doneFile)) {
    [ordered]@{
      record_id = $RID
      status = 'ERROR'
      error = [string]$reason
      failures = @([ordered]@{ phase = 'run_task'; type = 'error'; message = [string]$reason })
      finished_at = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $doneFile -Encoding UTF8
  }
  try {
    $notify = (& python $tool failure-update --task-json $taskFile --done-json $doneFile --policy $policy 2>&1 | Out-String).Trim()
    if ($notify.Length -gt 1200) { $notify = $notify.Substring($notify.Length - 1200) }
    Out-Log ('FAILURE_NOTIFICATION ' + $notify)
  } catch {
    Out-Log ('FAILURE_NOTIFICATION_DEFERRED ' + $_.Exception.Message)
  }
}

$netUseOk = $false
try {
  $preflightTool = Join-Path $PIPE 'tools\package_preflight.py'
  if (!(Test-Path -LiteralPath $preflightTool)) { throw 'PACKAGE_PREFLIGHT_TOOL_MISSING' }
  $preflightOutput = (& python $preflightTool --package-root $PIPE 2>&1 | Out-String)
  if ($LASTEXITCODE -ne 0) { throw 'PACKAGE_PREFLIGHT_FAILED' }
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

  # 视觉子进程固定走火山 CodingPlan 的 OpenAI-compatible profile。
  # 旧的 CC Switch Anthropic Messages env 文件会返回空响应；这里显式
  # 选择 .codex/volc.config.toml 对应的 `codex exec -p volc`，避免
  # 视觉请求和外层任务使用不同 provider。
  $env:JY_VISION_TRANSPORT = 'codex'
  $env:JY_VISION_PROFILE = 'volc'
  $env:JY_VISION_PROVIDER = 'volc'
  $env:JY_VISION_MODEL = 'glm-5.3-flash'
  Remove-Item Env:JY_VISION_ANTHROPIC_ENV -ErrorAction SilentlyContinue

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

  # 真实生产入口必须先冻结合同；提示词不能替代确定性输入/版本哈希校验。
  $evalTool = Join-Path $PIPE 'tools\eval_bootstrap.py'
  $identityFile = [Environment]::GetEnvironmentVariable('JY_PRODUCT_IDENTITY_FILE', 'Process')
  if (-not (Test-Path -LiteralPath $evalTool) -or [string]::IsNullOrWhiteSpace($identityFile)) { throw 'EVAL_BOOTSTRAP_CONFIG_MISSING' }
  $evalArgs = @($evalTool, '--task-json', $TASK, '--runtime-root', $PIPE, '--identity-file', $identityFile)
  $evalMode = [Environment]::GetEnvironmentVariable('JY_EVAL_MODE', 'Process')
  if (-not [string]::IsNullOrWhiteSpace($evalMode)) { $evalArgs += @('--mode', $evalMode) }
  $materialIndex = [Environment]::GetEnvironmentVariable('JY_MATERIAL_INDEX_FILE', 'Process')
  if (-not [string]::IsNullOrWhiteSpace($materialIndex)) { $evalArgs += @('--material-index', $materialIndex) }
  $observations = [Environment]::GetEnvironmentVariable('JY_EVAL_OBSERVATIONS', 'Process')
  if (-not [string]::IsNullOrWhiteSpace($observations)) { $evalArgs += @('--observations', $observations) }
  $evalResult = & python @evalArgs 2>&1 | Out-String
  if ($LASTEXITCODE -ne 0) { throw ('EVAL_BOOTSTRAP_BLOCKED ' + $evalResult.Trim()) }
  Out-Log ('EVAL_BOOTSTRAP ' + $evalResult.Trim())

  $prompt = @'
你是剪映混剪生产流水线执行者，无人值守环境。严格按序执行，禁止跳过、禁止交互：
1. 读 __PIPE_ROOT__\RULES.md 和 __PIPE_ROOT__\WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json（全量规则、视觉匹配合同与红线，逐条遵守；JSON 是自动化执行参数，不能只读文档不落实）
2. 读当前目录 task.json（本次任务；script 为运营原文禁止改动；manifest.voice 必须逐字复制 task.json 的 voice，禁止自行改成相近音色或 fallback）。执行器已在生成前冻结 `work\<record_id>\eval\active_contract.json`；核对 task_id、run_id、品类/SKU 和 inputs 中的素材路径/哈希。实际 build 只能使用合同 inputs 中的素材，不得重新全目录取未批准素材；不得自行改写或重建合同，缺失/漂移即停止并留证。
3. 素材先按 task.json 的品类和 SKU 精确筛选。若 manifest 已标记 material_cache_reused=true，先校验 material_index_sha256、同品类/SKU、标签规则版本及人工审核状态，复用其 shot_analysis_file 和来源；禁止再次全量扫描或跨 SKU 使用。否则只对本任务素材做一次视觉入库分析：枚举素材并在 manifest 写入 material_sources、temporary_material_analysis=true、vision_analysis=true（不要用脚本文案替素材编造标签）。运行 `python __SKILL_ROOT__\run.py analyze-shots --input <manifest> --report-dir <report_dir>`，引擎会自动切镜头、抽取中间/末帧、生成带标签 contact sheet，并用 `codex exec --image` 对当前商品素材逐批生成真实 description/visual_tags/evidence_tags/action_complete/evidence_intervals；不得手工回填、不得用 `frame_visible` 占位。将 manifest.shot_analysis_file 指向生成的 shot_candidates.json，再执行 run.py build；匹配必须扫描当前已批准的同 SKU 候选池，24 只是最终上限，镜头数由脚本语义段与有效匹配决定；顺序固定为 DIRECT → 相关产品/结果特写降级 → MATERIAL_GAP。降级候选不得抢占后续语义段的唯一直接候选；CTA/划算/囤货/数量必须有多包、成排、整箱、堆叠或可见数量，单包特写不能作结尾 CTA；包装文字只有明确指向可读同一卖点时才可标 PACKAGING_CLAIM_DIRECT，不能证明真实性或功能效果。最终 source_timerange 必须覆盖动作/文字证据窗口，错误只修复对应分镜并复用已缓存素材理解。所有字幕必须在画布内显示，字号 8、加粗、X=0/Y=-0.8，并以渲染帧验收；结构检查不能代替画面验收。视觉模型调用失败、返回字段不完整或无法确认动作时，当前片段必须保持 pending 并记录 VISUAL_EVIDENCE_REQUIRED/SHOT_MATCH_BLOCKED；但没有直接动作/结果证据不等于整片失败，按 RULES.md 先尝试相关产品部位/结果特写降级，仍无合适候选才记录 MATERIAL_GAP/留空。最终切片未覆盖所选证据时只修复对应片段或改用候选，不得用无关镜头充数。用引擎 run.py build 生成剪映草稿（本地 __DRAFT_ROOT__）
4. 先完成独立 draft_visual_qc.py 与包完整性检查，在 done.json 写入实际生成的本地草稿名和 QC 报告。不要直接调用 ship_distribute.py，不要自行创建普通 mac_review_approved.json，也不要直接写正式 NAS/表格。执行器会在生成完成后确定性生成 Eval 检查包，并把可编辑草稿复制到独立 Mac 待审区供用户审核；Mac 审核通过前只能是 AWAITING_MAC_REVIEW。随后由 review operator 执行 tools\\eval_stage.py 的 collect → finalize。只有 hash-bound Eval/Mac 收据通过后，授权 finalizer 才能正式分发及回写。count>1 时统一归档到一个父文件夹下的 版本1/版本2/...，表格只写父文件夹；新包通过后才替换旧交付包，不能先删旧包，也不能删除原素材。
5. 写生产日志到 NAS 测试产出\生产日志\；日志必须包含 skill_version=__SKILL_VERSION__、rules_version=__RULES_VERSION__（这两个版本号已由 run 脚本按运行时实际文件注入，照抄即可，不要自行猜测或改写）、voice_field（运营填写原名）、voice_used（实际 speaker_id/剪映显示名/后端）、shot_candidates、shot_match_report、shot_match_ai、visual_evidence_report、pending_items、字幕门禁结果和稳定切片窗口信息
6. 不得在生成步骤绕过 Eval finalizer 调用 tools\\closed_loop.py 的 archive-parent/table-update。后续授权 finalizer 通过 audit → archive-parent → table-update 回写飞书时，只写 草稿文件/任务状态/成片状态 三个业务字段，更新时间由系统自动维护；只有门禁要求的 Eval PASS、Mac 已批准、NAS 包完整、路径可移植且 bat/fix_local 存在时才允许回写；PASS_WITH_GAPS/UNCERTIFIED/超时都只能预览或待复核
7. 写 codex_done.json（status/成品路径/耗时/warnings/失败原因）
8. 最后一行输出 SUMMARY status=SUCCESS 或 status=FAILED 及原因
失败路径：任何步骤失败 -> 生产日志写 FAILED，调用 tools\closed_loop.py failure-update 仅回写 任务状态=生成失败 + 问题反馈失败回执；不写草稿文件/成功状态，不进 NAS；若回写失败写 failure_notification_receipt.json 后继续队列。codex_done.json status=failed。不要反复重试 build 超过 1 次。
9. 如果 task.json.rev 非空，这是轮B二次处理：必须先读取 task.json.issue、feedback_scope、feedback_category 和 draft_file。问题反馈是运营原文，禁止改写；先按问题反馈定位并只替换错误分镜，重新核对源窗口、字幕和视觉 QC，再生成新的 -rev / -rev2 包。新包未通过结构、视觉、路径、Mac 检查前不得删除旧包；通过后才替换旧交付包并按规则回写三字段。
10. 如果 feedback_scope=generic，问题修复成功后在当前 work 目录写 feedback_resolution.json：status=resolved、rule_key、rule_text、evidence；如果没有真正完成修复，写 status=unresolved。不要直接改版本文件，父流程会在成功后做幂等版本沉淀。

'@
  $prompt = $prompt.Replace('__SKILL_VERSION__', $skillVer).Replace('__RULES_VERSION__', $rulesVer).Replace('__PIPE_ROOT__', $PIPE).Replace('__SKILL_ROOT__', $skillRoot).Replace('__DRAFT_ROOT__', $draftRoot)
  Set-Content -LiteralPath (Join-Path $cdir 'prompt.txt') -Value $prompt -Encoding UTF8
  $codexLog = Join-Path $cdir 'codex.log'
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  $generationStarted = $true
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
  $workerDone = Join-Path $cdir 'done.json'
  $hasReviewableDraft = $false
  if (Test-Path -LiteralPath $workerDone) {
    try {
      $previewData = Get-Content -LiteralPath $workerDone -Raw -Encoding UTF8 | ConvertFrom-Json
      $previewNames = if ($previewData.local_only_drafts) { @($previewData.local_only_drafts) } else { @($previewData.drafts) }
      $previewNames = @($previewNames | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
      $hasReviewableDraft = ($previewNames.Count -gt 0)
    } catch {}
  }
  if ($doneStatus -in @('SUCCESS','PARTIAL','PREVIEW_READY') -and $hasReviewableDraft) {
    $stageRoot = [Environment]::GetEnvironmentVariable('JY_MAC_REVIEW_STAGE', 'Process')
    $macRoot = [Environment]::GetEnvironmentVariable('JY_MAC_DRAFT_ROOT', 'Process')
    $evalStage = Join-Path $PIPE 'tools\eval_stage.py'
    $handoffTool = Join-Path $PIPE 'tools\mac_review_handoff.py'
    if ([string]::IsNullOrWhiteSpace($stageRoot) -or [string]::IsNullOrWhiteSpace($macRoot) -or
        -not (Test-Path -LiteralPath $workerDone) -or -not (Test-Path -LiteralPath $evalStage) -or
        -not (Test-Path -LiteralPath $handoffTool)) {
      $doneStatus = 'PREVIEW_PENDING_TRANSFER'
      Out-Log 'MAC_REVIEW_HANDOFF_PENDING config_or_done_missing; draft retained'
    } else {
      $prepareResult = & python $evalStage prepare --task-dir $cdir --draft-root $draftRoot 2>&1 | Out-String
      if ($LASTEXITCODE -ne 0) {
        $doneStatus = 'PREVIEW_PENDING_TRANSFER'
        Out-Log ('MAC_REVIEW_PREPARE_PENDING ' + $prepareResult.Trim())
      } else {
        $handoffResult = & python $handoffTool --task-dir $cdir --draft-root $draftRoot --stage-root $stageRoot --mac-root $macRoot 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) {
          $doneStatus = 'PREVIEW_PENDING_TRANSFER'
          Out-Log ('MAC_REVIEW_TRANSFER_PENDING ' + $handoffResult.Trim())
        } else {
          $doneStatus = 'AWAITING_MAC_REVIEW'
          Out-Log ('MAC_REVIEW_HANDOFF ' + $handoffResult.Trim())
        }
      }
    }
  }
  if ($doneOk -and -not $hasReviewableDraft) {
    $doneStatus = 'PREVIEW_PENDING_TRANSFER'
    Out-Log 'MAC_REVIEW_HANDOFF_PENDING done_or_drafts_missing; no delivery assertion'
  }
  if ($doneStatus -in @('ERROR','FAILED','PARTIAL')) {
    Publish-FailureNotification ('codex_done_status=' + $doneStatus)
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
    if ($t.issue) {
      $sha = [System.Security.Cryptography.SHA256]::Create()
      try { $issueHash = [BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes([string]$t.issue))).Replace('-','').ToLowerInvariant() }
      finally { $sha.Dispose() }
    }
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
  if ($generationStarted) { Publish-FailureNotification $_.Exception.Message }
  else { Out-Log 'PRESTART_BLOCKED no_generation_or_business_failure_writeback' }
  try {
    if (-not $generationStarted) { throw 'PRESTART_BLOCKED' }
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
