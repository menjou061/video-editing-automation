# 豆包素材语义分析提示（v1.3.13）

## 两阶段顺序

1. `analyze-shots` 只读取当前任务的 `material_sources`，逐镜切片、抽帧并独立描述素材；这一阶段不要读取脚本文案，也不要用脚本反向编造画面标签。
2. `match-shots` / `build` 再读取脚本，按 `claim_text` 对已分析的镜头推荐 Top3，并由编排引擎自动采用 Top1；无合格证据时直接阻断，不请求人工选镜头。

临时分析结果只写入本任务的 `shot_candidates.json` 和 `shot_match_report.json`，未采用镜头不得写入复用素材库。

镜头采用由引擎自动完成：同一临时镜头最多采用一次，同一源视频默认最多采用两次；相邻口播句在存在合格候选时优先换源。达到上限且没有其他能证明该句的镜头时返回 `SHOT_REUSE_LIMIT`，不能用重复画面凑数。

你正在为剪映可编辑草稿选择镜头。第二阶段读取 `shot_candidates.json`（其中已包含本任务素材的抽帧、时间码和临时描述），
再结合口播文案逐句匹配，输出严格 JSON，不要输出 Markdown：

```json
{
  "material_sources": ["/absolute/path/current_product_1.mp4"],
  "full_script": "完整脚本或口播整稿；连续自然语言可直接作为口播，镜头表请用口播:/旁白:明确标注可念内容",
  "segments": [
    {
      "video": "/absolute/path/source.mp4",
      "source_start": 0.0,
      "duration": 2.5,
      "text": "该镜头对应的口播/字幕文案",
      "audio_mode": "tts",
      "voice": "female_gentle",
      "confidence": 0.0,
      "claim_text": "这句口播正在证明的具体卖点",
      "visual_requirements": ["柔软"],
      "evidence_tags": ["手部触摸纸面", "材质近景"],
      "visual_tags": ["纸张", "近景", "手部"],
      "evidence_intervals": [
        {"start": 1.20, "end": 2.35, "frame_path": "抽帧文件绝对路径"}
      ],
      "action_complete": true,
      "rationale": "画面与文案的具体匹配依据"
    }
  ]
}
```

## audio_mode 判定（必须逐段判断）
- `tts`：画面本身无人声，需要 TTS 念出 `text`。此时 `duration` 可给 0 或估值，引擎会按 TTS 实际音频定长。
- `native`：画面里真人正在开口说话（现场原声口播），**必须保留原声、不再配 TTS**；`text` 写其真实台词，
  `source_start/duration` 精确框住人声区间，宁可短一点也不要切进没说话的空镜。
- `file`：使用外部配音文件（同时给 `audio` 绝对路径）。
- `mute`：无旁白的过渡/氛围镜头（需有 BGM 兜底，尽量少用）。

## 硬性要求
- 输入脚本先判断是否可直接口播：自然叙述、人物台词和明确的 `口播:`/`旁白:` 行可以原样用于 TTS；仅含镜头、景别、动作、转场、字幕、时间码等制作说明时，不得把说明文字当成口播。混合脚本只抽取明确标注的口播/旁白行。
- `video`/`audio` 必须用输入素材中的绝对路径，不得编造文件名。
- `source_start`、`duration` 用秒，不得超出 ffprobe 时长；**入点避开手持晃动、运镜推拉、主体入画未稳的片段**，
  优先选主体稳定居中的窗口。
- **口播文案必须与画面内容一致**：这句在讲什么，画面就展示什么；每个有口播的分镜必须填写
  `visual_requirements`（或 `required_visuals`）与 `evidence_tags`，并用 `visual_tags`、`frame_tags`、
  `visual_description` 或 `rationale` 写出可审计的素材证据。已知卖点（如提数、柔软、湿水不破）必须
  在证据中出现；同时必须用 `evidence_intervals` 标出原素材中实际展示该卖点的起止时间，并提供对应抽帧文件。
  找不到匹配画面时不要猜，保持 `pending`，由引擎返回 `VISUAL_EVIDENCE_REQUIRED` 并阻止生成。引擎在稳定层和
  TTS 定长后会再次检查最终切片是否覆盖这些区间；证据被裁掉也必须阻断。`action_complete` 必须明确为 `true`
  才能进入成片。
  `action_complete` 必须明确为 `true` 才能进入成片。视频长于口播时引擎会舍弃多余画面，你只需保证选中窗口内容正确。
- 相邻分镜的口播要衔接自然：人称/视角不跳变、叙事顺序连贯、不重复、不用无前文指代的“它/这”冷开场；
  发现整稿与分镜割裂时先调整文案顺序或补连接词。
- 全片不允许无声画面（mute 段除外且要有 BGM），也不允许有旁白却无对应画面。
- 异源素材之间的硬切由引擎自动加「叠化」，你无需指定转场；同源连续镜头保持连贯即可。
- `confidence` 为 0–1，无法确认给低分、不要猜；每个镜头写可审计的 `rationale`。
- 只输出 JSON 对象。生成后用 `python3 run.py build --input manifest.json` 写入草稿。
