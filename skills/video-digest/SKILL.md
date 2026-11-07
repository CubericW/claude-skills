---
name: video-digest
description: >
  Summarize, transcribe and analyze online or local videos entirely on your Mac (free, no
  account): Bilibili/B站 (BV ids, b23.tv), YouTube, Douyin/抖音, Xiaohongshu/小红书, Kuaishou,
  Weibo, X/Twitter, podcasts and any site yt-dlp supports, plus local video/audio files. Uses
  official or AI subtitles when they exist, otherwise local Whisper speech-to-text; extracts
  keyframes and reads on-screen text (slides, code, charts, burned-in subtitles) with OCR; then
  you write a structured Chinese summary with timestamped sections. Use this whenever the user
  shares a video link, share text or video file, or asks what a video says, wants a summary,
  notes, outline, transcript or subtitles of a video, or wants to know what is on screen / in
  the PPT — e.g. "总结这个视频", "这个视频讲了什么", "B站总结", "视频转文字", "提取字幕",
  "帮我看看这个视频", "整理成笔记", "summarize this video" — even when they don't say
  "summarize".
---

# Video Digest

A local BibiGPT-style pipeline. One script turns a video into text you can read; you then do
the understanding and writing.

```
URL / share text / BV id / file
  → metadata + official chapters (yt-dlp, the user's browser cookies for login-only sites)
  → best subtitle track (uploader CC > B站 AI subtitles > YouTube "-orig" auto captions)
  → no usable subtitles? local speech-to-text (mlx-whisper large-v3-turbo on Apple Silicon)
  → keyframes + OCR (Apple Vision) · burned-in subtitle OCR for short videos without subtitles
  → ~/video-digest/<platform>-<id>/digest.md   ← everything you need, in one file
```

## 1. Run the pipeline

```bash
uv run --script ~/.claude/skills/video-digest/scripts/digest.py "<url | BV id | share text | file>"
```

- Pass the user's input as-is: Douyin/小红书 share text with a URL inside works, so do `BV1xx…`.
- It prints progress on stderr and a JSON summary on stdout (`workdir`, `digest`, `text_source`,
  `contact_sheets`, …). Exit codes: 0 ok · 2 failed (the message says why and what to try) ·
  3 playlist/collection (lists the entries; ask which ones, or run them one by one) · 4 no
  text at all.
- Timing: a video with subtitles takes seconds; without subtitles expect roughly 1 min per
  20–30 min of audio, plus a one-time ~1.6 GB model download. For videos over ~15 min, or on
  the first run, use `run_in_background` and wait for the completion notice instead of
  blocking. Tell the user briefly what is happening (e.g. "没有字幕，正在本地转写").
- Results are cached in the work folder; re-running the same input reuses finished steps.
  `--redo visual` (or `asr`, `subs`, `burned`) redoes one step, e.g. with `--max-height 1080`;
  `--force` redoes everything.

Useful options (defaults come from `~/.config/video-digest/config.json`):

| Option | When |
|---|---|
| `--visual on/off` | Keyframe OCR is on by default for videos ≤ 3 h; `off` for speed when only speech matters |
| `--burned-subs on` | Hard-coded subtitles you want read even though soft subtitles exist (default: only for ≤ 20 min videos without subtitles) |
| `--asr force` | Subtitles look wrong or incomplete — transcribe the audio anyway |
| `--language ja` | Speech language hint when detection might fail (mixed-language intros) |
| `--ocr-langs ja-JP,en-US` | On-screen text is not Chinese/English |
| `--sub-region 0.8,1.0` | Burned subtitles sit somewhere unusual (fractions of frame height) |
| `--max-height 1080` | Small text on slides/code is unreadable at 720p |
| `--keep-media` | The user also wants the downloaded video/audio |

## 2. Read what it produced

Start with `digest.md`: metadata, official chapters, description, on-screen text (OCR),
burned subtitles and the transcript (inline when short; otherwise it tells you to page through
`transcript.md` — read all of it in chunks before summarizing; skimming the start of a long
video produces a summary of the intro).

Then look at the picture, because OCR text alone loses layout, charts and diagrams:
- Open 1–2 contact sheets (`sheets/sheet_01.jpg` …) with the Read tool to see what kind of
  video it is (slides, talking head, screen recording, vlog). The sheets show an even sample of
  the keyframes, not all of them; `ocr.md` covers every keyframe.
- Open individual `frames/kf_*.jpg` (the name carries the timestamp) when OCR shows a chart,
  table, formula, code or diagram that matters to the content.

Other files in the work folder: `transcript.srt`, `burned_subs.srt`, `ocr.json` (boxes and
confidences), `meta.json`, `info.json`, `logs/`.

## 3. Write the summary

Follow `references/summary-guide.md` (template, length by video type, other output modes such
as transcript export, article rewrite and deep-dive Q&A). The points that matter most:

- **Cross-check the text sources.** Speech-to-text gets names, terms and numbers wrong
  ("消耳算法" for 肖尔算法). Slide OCR, burned subtitles, the title, tags and description are
  usually right — prefer them, and fix obvious ASR errors silently. Flag anything you could
  not resolve instead of guessing.
- **Timestamps link back.** When `digest.md` gives a link format, make section timestamps
  clickable (`[12:34](https://www.bilibili.com/video/BV…?t=754)`). Use official chapters as the
  skeleton when they exist.
- **Say where the text came from** (official subtitles / B站 AI subtitles / local ASR / OCR)
  and note gaps (no speech, failed step) so the user knows how far to trust it.
- **Use what was on screen**: numbers on slides, code, chart takeaways — these are often the
  most valuable part and are missing from audio-only summaries.

Save the summary as `summary.md` in the work folder, and give it to the user in your reply
(not just a path). Mention the work folder once so follow-up questions can use it.

## 4. Follow-ups

The work folder stays on disk, so later questions ("第 20 分钟讲了什么", "他对 X 的看法是什么",
"把代码整理出来") are answered from `transcript.md` / `ocr.md` / frames with Grep and Read —
no need to re-run the pipeline. For several videos, run the script once per URL (sequentially;
ASR uses the GPU) and then compare or merge.

## When something fails

Read the error hint first; platform specifics (B站 412 风控, Douyin cookies, 小红书 xsec_token,
YouTube bot check, podcasts, proxies, Hugging Face mirror, model choices) are in
`references/platforms.md`. Never try to bypass a platform's risk control or bot checks; use the
user's own login cookies (configured), a local file the user provides, or report the failure.
