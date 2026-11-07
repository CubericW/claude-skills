# Platforms, failures and settings

Config file: `~/.config/video-digest/config.json` (see `config.example.json` in the skill
folder). Command-line flags override it for one run.

## Login cookies

Some sites refuse anonymous requests. The pipeline then passes yt-dlp
`--cookies-from-browser <cookies_from_browser>`, but only for hosts in `cookie_sites`, so the
user's logins are not sent to every site. The browser must be logged in to that site. On macOS
the first read of Chrome cookies shows a Keychain prompt ("Chrome Safe Storage"); the user
should click 始终允许. `cookies_file` (a Netscape cookies.txt) is the alternative.

Never work around risk control or bot checks by other means (fake fingerprints, scraping
anonymous tokens, CAPTCHA services). The options are the user's own cookies, a local file the
user provides, or reporting the failure.

## Bilibili / B站

- Requires cookies: anonymous requests get HTTP 412 "由于触发哔哩哔哩安全风控策略" (yt-dlp shows
  "Unable to extract initial state" or "geo-restricted"). With cookies you also get 1080p and
  subtitles.
- Subtitles: uploader CC tracks (`zh-CN`, `en-US` …) and AI subtitles (`ai-zh`, needs login).
  Many videos have neither; the pipeline then transcribes locally.
- Multi-part videos (分P): pass `?p=N` in the URL to pick a part; default is P1. Collections,
  favourites and space pages are playlists (exit code 3 with the entry list).
- 番剧/电影/付费/充电专属 content may return 403 or only a preview even with cookies.
- Timestamp links: `https://www.bilibili.com/video/BV…?t=<sec>` (`?p=N&t=<sec>` for parts).
- Danmaku is available as a "danmaku" subtitle track in `info.json` but is not used.
- Keyword search (`bilisearch:`) is blocked by B站 risk control even with cookies; work from
  video links. To find videos by topic, ask the user for links or use another search tool.

## YouTube

- Manual subtitles in the original language are preferred over translations; auto captions
  use the `<lang>-orig` track (YouTube's own ASR of the original audio).
- yt-dlp needs a JavaScript runtime for YouTube (deno is installed; node is used as fallback).
- "Sign in to confirm you're not a bot": wait and retry, or — only if the user agrees, since
  it uses their Google account — add `youtube.com` to `cookie_sites`.

## Douyin / 抖音

- Share text such as `7.43 复制打开抖音… https://v.douyin.com/xxxx/` works directly.
- yt-dlp needs "fresh cookies": keep `douyin.com` in `cookie_sites` and make sure the browser
  has opened douyin.com recently. If it still fails, the `mediacrawler` MCP server (if
  connected) can fetch the post via `get_post_detail(platform="dy", post_ids=[id])`; pass the
  direct video URL it returns, or ask the user to save the video and give the file path.
- Most Douyin videos have no soft subtitles but often burned-in ones: burned-subtitle OCR runs
  automatically for videos ≤ 20 min.

## Xiaohongshu / 小红书

- Needs the full link with `xsec_token` (App → 分享 → 复制链接; share text works).
- Only video notes are supported by yt-dlp. For image notes, download the images (or ask the
  user for them) and read them with the Read tool directly.

## Kuaishou, Weibo, X/Twitter, Vimeo, TED, etc.

Anything yt-dlp supports works the same way. Add the domain to `cookie_sites` if the site needs
a login.

## Podcasts and audio

Apple Podcasts / 小宇宙 / RSS enclosure URLs and local mp3/m4a files are transcribed; there is
no picture, so OCR is skipped. Long episodes: run in the background.

## Local files

Any file ffmpeg can read. Files are never deleted. The work folder is
`~/video-digest/local-<name>-<hash>/`.

## Network

- `proxy` in config (or `--proxy socks5://127.0.0.1:7890`) for sites that need one.
- The Whisper model downloads from Hugging Face on the first ASR run. If that is blocked, set
  `HF_ENDPOINT=https://hf-mirror.com` in the environment of the command.
- yt-dlp runs as the latest release via `uvx` (extractors break often; this keeps them fresh),
  falling back to the system `yt-dlp` when offline. Set `"ytdlp": "system"` to always use the
  system one.

## Speech-to-text models

`asr_model` in config / `--asr-model`:
- `mlx-community/whisper-large-v3-turbo` (default): fast, good for Chinese/English.
- `mlx-community/whisper-large-v3-mlx`: a little more accurate, ~3× slower.
- `mlx-community/whisper-small-mlx`: very fast drafts.
On non-Apple-Silicon machines faster-whisper is used (`large-v3-turbo`, with voice-activity
filtering).

Known ASR weaknesses: proper nouns and numbers, songs and background music (may produce
little or no text), heavy dialects, several people talking at once. Cross-check with OCR.

## OCR

Apple Vision (on-device) with `ocr_langs` (default `zh-Hans,en-US`; others: `zh-Hant`,
`ja-JP`, `ko-KR`, `fr-FR`, `de-DE`, …). Keyframes are taken at 720p by default; use
`--max-height 1080` when slide text is small. RapidOCR is the fallback outside macOS.

## Exit codes and logs

0 ok · 2 failure (message includes hints) · 3 playlist · 4 nothing readable. Detailed logs:
`<workdir>/logs/asr.log`, `<workdir>/logs/visual.log`.
