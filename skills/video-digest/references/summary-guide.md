# Writing the summary

Write in Chinese unless the user asks otherwise, whatever the video's language. Scale the
length to the content: a 1-minute Douyin clip gets a few lines; a 2-hour lecture gets a full
outline. A summary that is longer than the useful content of the video has failed.

## Default template

```markdown
# {标题}

> {UP主/频道} · {时长} · {平台} · [原视频]({url})
> 文字来源：{官方字幕 / B站 AI 字幕 / 本地语音转写 / 硬字幕 OCR} · 画面：{N 张关键帧 OCR / 无}

## 一句话总结
{这个视频最核心的结论或信息，一两句}

## 核心要点
- {3–7 条，每条一句完整的信息，带具体数字/名称，不要空泛}

## 分段详解
### [{mm:ss}]({带时间戳的链接}) {段落标题}
{这一段讲了什么：论点、论据、例子、数据。2–6 句或几个要点}
…

## 画面中的关键信息
{幻灯片/图表/代码/字幕里出现、但口头没细说的内容：数据表、公式、代码片段、图表结论、引用来源。没有就省略这一节}

## 值得记下的数据 / 原话
- {关键数字、结论}（{mm:ss}）
- 「{最多 1–2 句简短原话}」（{mm:ss}）

## 备注
{转写可能有误且无法从画面核实的专有名词；缺失或失败的部分；视频的立场/广告成分（如有）}
```

Section rules:
- Title: for a non-Chinese video, give a Chinese translation followed by the original title in
  parentheses.
- Quotes: paraphrase by default; quote at most one or two short sentences, translated into
  Chinese with the original in parentheses only if the wording itself matters. Numbers and
  conclusions are usually more useful than quotes.
- Use the official chapters as section boundaries when they exist; otherwise cut where the
  topic changes (slide titles in `ocr.md` are good cut points). Aim for sections of 3–10 min in
  long videos.
- Keep "核心要点" self-contained: someone who reads only that list should learn the substance.
- Skip empty sections instead of writing "无".

## Adapting to the video type

| Type | Emphasis |
|---|---|
| Lecture / paper reading / tutorial | Structure follows the slides; keep formulas, definitions, steps and code; "画面中的关键信息" is usually the richest section |
| Talk / interview / podcast | Who holds which view, arguments and disagreements, notable quotes with timestamps |
| News / commentary | Facts vs. opinions; dates, numbers, sources cited; note the creator's stance |
| Product review / unboxing | Specs, prices, pros/cons, verdict; comparisons |
| Short video (抖音/小红书 < 3 min) | 3–5 lines: what it shows, the key claim or tip, any product/place named. No section breakdown |
| Vlog / travel / food | Places, routes, prices, times, recommendations, in visit order |
| Music video / no speech | Describe from frames and on-screen text; say there is little or no speech |

## Reliability habits

- When ASR and OCR disagree on a name or number, trust OCR/burned subtitles/title/description,
  and correct silently. If nothing on screen confirms an odd-sounding term, keep it but mark it
  with (?) and list it under 备注.
- Do not invent content for parts that are missing (failed step, no speech, unreadable text).
- Distinguish what the speaker claims from established fact when it matters (health, finance,
  news); a summary is not an endorsement.

## Other output modes

Pick the one the user asks for; all of them read the same work folder.

- **字幕 / 文字稿导出**: give `transcript.srt` / `transcript.md` (or `burned_subs.srt`). If they want
  a clean readable transcript, rewrite `transcript.md` into paragraphs with punctuation and
  fixed terms, keeping `[mm:ss]` markers every paragraph — no summarizing.
- **深度解读 / 逐段精读**: one section per chapter or slide with the full argument, examples and
  your explanation of hard parts; then 3–5 questions the video leaves open.
- **改写成文章 / 公众号 / 小红书**: rewrite into the requested format and tone, keep facts and
  numbers exact, credit the original video with its link.
- **问答**: answer from the transcript with timestamps as evidence; Grep `transcript.md` and
  `ocr.md` for keywords first, then read the surrounding paragraphs.
- **思维导图**: a nested Markdown outline (or Mermaid `mindmap`) of the summary structure.
- **多视频对比 / 专题汇总**: run each video, then a table of who says what, agreements and
  conflicts, with links to each source timestamp.
- **存到 Obsidian**: if the obsidian skill is available, save `summary.md` content as a note
  (title = video title, properties: source url, uploader, date, duration, tags).
