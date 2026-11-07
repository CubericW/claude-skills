#!/usr/bin/env python3
"""video-digest: turn an online video (URL / share text / BV id) or a local media file
into text that an LLM can summarize.

Pipeline
  metadata (yt-dlp) -> best subtitle track -> local ASR when there are no usable subtitles
  -> keyframes + OCR, burned-in subtitle OCR -> transcript.md / ocr.md / digest.md

This file only uses the standard library. The heavy steps run in isolated environments
through `uv run --script` (transcribe.py, visual.py), in parallel.

Usage
  digest.py <url | BV id | share text | local file> [options]
  digest.py --help
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

SCRIPTS = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("VIDEO_DIGEST_CONFIG", "~/.config/video-digest/config.json")).expanduser()

DEFAULTS = {
    "output_dir": "~/video-digest",
    # Browser to read login cookies from ("chrome", "safari", "firefox", "edge", ...), or a
    # cookies.txt path. Only sent to the sites listed in cookie_sites ("*" = every site).
    "cookies_from_browser": None,
    "cookies_file": None,
    "cookie_sites": ["bilibili.com", "b23.tv", "douyin.com", "iesdouyin.com", "xiaohongshu.com",
                     "xhslink.com", "kuaishou.com", "weibo.com", "weibo.cn"],
    "max_height": 720,          # video height downloaded for OCR
    "visual": "auto",           # keyframes + OCR: auto | on | off
    "burned_subs": "auto",      # OCR of hard-coded subtitles: auto | on | off
    "burned_subs_max_minutes": 20,
    "visual_max_minutes": 180,
    "max_frames": 80,
    "asr_engine": "auto",       # auto | mlx | faster
    "asr_model": None,          # engine default (whisper large-v3-turbo)
    "ocr_langs": "zh-Hans,en-US",
    "keep_media": False,
    "proxy": None,
    "ytdlp": "auto",            # auto (latest via uvx, fallback to system) | uvx | system
}

SKIP_SUB_LANGS = {"danmaku", "live_chat", "rechat", "comments"}
PLATFORM_NAMES = {"youtube": "YouTube", "bilibili": "Bilibili", "douyin": "抖音", "xiaohongshu": "小红书",
                  "kuaishou": "快手", "weibo": "微博", "twitter": "X/Twitter", "vimeo": "Vimeo"}
# Apple Vision languages to read on-screen text with, by spoken language. Listing Chinese for an
# English video makes Vision "read" circles and icons as rare Chinese characters.
OCR_LANGS = {"zh": "zh-Hans,en-US", "en": "en-US", "ja": "ja-JP,en-US", "ko": "ko-KR,en-US",
             "fr": "fr-FR,en-US", "de": "de-DE,en-US", "es": "es-ES,en-US", "it": "it-IT,en-US",
             "pt": "pt-BR,en-US", "ru": "ru-RU,en-US", "yue": "zh-Hant,zh-Hans,en-US"}

ERROR_HINTS = [
    (r"412|风控|Unable to extract initial state|geo-restricted",
     "B站风控拒绝了请求：确认 config 里 cookies_from_browser 指向已登录 B站 的浏览器，稍后重试。"),
    (r"Fresh cookies|cookies are needed|login required|需要登录|--cookies",
     "该平台需要登录 cookie：在 ~/.config/video-digest/config.json 设置 cookies_from_browser，"
     "并确认 cookie_sites 包含该网站。"),
    (r"Sign in to confirm|not a bot",
     "YouTube 要求登录验证：可在 cookie_sites 里加入 youtube.com 使用你自己的登录状态，或稍后再试。"),
    (r"Unsupported URL",
     "yt-dlp 不支持这个链接：试试视频详情页的完整链接，或先把视频下载到本地再传文件路径。"),
    (r"xsec_token|xiaohongshu",
     "小红书链接需要带 xsec_token 的完整分享链接（从 App 分享 → 复制链接）。"),
    (r"HTTP Error 403|HTTP Error 404",
     "视频可能已删除、需要会员或有地区限制；可先更新 yt-dlp，或改用本地文件。"),
]


def log(msg: str) -> None:
    print(f"[video-digest] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 2) -> None:
    log(f"ERROR: {msg}")
    sys.exit(code)


# --------------------------------------------------------------------------- config / input

def load_config(args) -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update({k: v for k, v in json.loads(CONFIG_PATH.read_text()).items() if not k.startswith("_")})
        except json.JSONDecodeError as e:
            die(f"config {CONFIG_PATH} is not valid JSON: {e}")
    for key in ("output_dir", "cookies_from_browser", "cookies_file", "max_height", "visual",
                "burned_subs", "max_frames", "asr_engine", "asr_model", "ocr_langs", "proxy"):
        val = getattr(args, key, None)
        if val is not None:
            cfg[key] = val
    if args.keep_media:
        cfg["keep_media"] = True
    if args.no_cookies:
        cfg["cookies_from_browser"] = cfg["cookies_file"] = None
    return cfg


def classify_input(raw: str) -> tuple[str, str]:
    """Return ("local", path) or ("url", url)."""
    raw = raw.strip()
    p = Path(raw).expanduser()
    if p.exists():
        return "local", str(p.resolve())
    if re.fullmatch(r"BV[0-9A-Za-z]{10}", raw):
        return "url", f"https://www.bilibili.com/video/{raw}"
    if re.fullmatch(r"(?i)av\d+", raw):
        return "url", f"https://www.bilibili.com/video/{raw.lower()}"
    m = re.search(r"https?://[^\s\"'<>，。！】]+", raw)  # also handles App share text
    if m:
        return "url", m.group(0).rstrip(").,;")
    die(f"input is neither an existing file nor a URL: {raw!r}")


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^\w一-鿿-]+", "_", text).strip("_")
    return s[:limit] or "video"


# --------------------------------------------------------------------------- yt-dlp

class YtDlp:
    def __init__(self, cfg: dict, url: str):
        self.cfg, self.url = cfg, url
        self.base = self._pick_base(cfg["ytdlp"])

    @staticmethod
    def _pick_base(mode: str) -> list[str]:
        if mode in ("auto", "uvx") and shutil.which("uvx"):
            return ["uvx", "--quiet", "--from", "yt-dlp[default]@latest", "yt-dlp"]
        if shutil.which("yt-dlp"):
            return ["yt-dlp"]
        die("yt-dlp not found: install uv (`brew install uv`) or yt-dlp (`brew install yt-dlp`)")

    def common(self) -> list[str]:
        opts = ["--no-playlist", "--no-progress"]
        if self.cfg.get("proxy"):
            opts += ["--proxy", self.cfg["proxy"]]
        host = (urlparse(self.url).hostname or "").lower()
        sites = self.cfg.get("cookie_sites") or []
        if "*" in sites or any(host == s or host.endswith("." + s) for s in sites):
            if self.cfg.get("cookies_file"):
                opts += ["--cookies", str(Path(self.cfg["cookies_file"]).expanduser())]
            elif self.cfg.get("cookies_from_browser"):
                opts += ["--cookies-from-browser", self.cfg["cookies_from_browser"]]
        if ("youtube.com" in host or "youtu.be" in host) and not shutil.which("deno") and shutil.which("node"):
            opts += ["--js-runtimes", "node"]
        return opts

    def run(self, extra: list[str], capture: bool = True) -> subprocess.CompletedProcess:
        cmd = self.base + self.common() + extra
        res = subprocess.run(cmd, capture_output=capture, text=True)
        # uv itself failed (offline / PyPI unreachable): retry once with the system yt-dlp
        if res.returncode != 0 and self.base[0] == "uvx" and shutil.which("yt-dlp") \
                and "ERROR:" not in (res.stderr or ""):
            log("uvx yt-dlp unavailable, falling back to system yt-dlp")
            self.base = ["yt-dlp"]
            res = subprocess.run(self.base + self.common() + extra, capture_output=capture, text=True)
        return res


def explain_failure(stderr: str) -> str:
    tail = "\n".join(l for l in (stderr or "").strip().splitlines()[-6:])
    hints = [h for pat, h in ERROR_HINTS if re.search(pat, stderr or "", re.I)]
    return tail + ("\n提示: " + " / ".join(hints) if hints else "")


# --------------------------------------------------------------------------- subtitles

def pick_subtitle(info: dict) -> tuple[str, str, dict] | None:
    """Choose the single most trustworthy subtitle track: (kind, lang, fmt).

    Manual tracks beat automatic ones; the video's original language beats translations;
    B站 uploader CC beats B站 AI subtitles (ai-zh); YouTube auto captions use the "-orig" track.
    """
    orig = (info.get("language") or "").lower().split("-")[0]
    zh_pref = ["zh-hans", "zh-cn", "zh", "zh-hant", "zh-tw", "zh-hk", "ai-zh"]

    def rank_manual(lang: str) -> tuple:
        l = lang.lower()
        if l.startswith("ai-"):
            return (3, 0 if l == "ai-zh" else 1, l)
        if orig and l.split("-")[0] == orig:
            return (0, 0, l)
        if l in zh_pref:
            return (1, zh_pref.index(l), l)
        if l.startswith("en"):
            return (2, 0, l)
        return (2, 1, l)

    def rank_auto(lang: str) -> tuple:
        l = lang.lower()
        if l.endswith("-orig"):
            return (0, l)
        if orig and l == orig:
            return (1, l)
        return (5, l)  # machine translations: last resort only

    for kind, table, ranker in (("manual", info.get("subtitles") or {}, rank_manual),
                                ("auto", info.get("automatic_captions") or {}, rank_auto)):
        langs = [l for l, fmts in table.items() if l.lower() not in SKIP_SUB_LANGS and fmts]
        if kind == "auto":
            langs = [l for l in langs if ranker(l)[0] < 5] or ([] if info.get("subtitles") else langs)
        if not langs:
            continue
        lang = sorted(langs, key=ranker)[0]
        fmts = table[lang]
        pref = ["json3", "srt", "vtt", "srv3", "ttml"]
        fmt = sorted(fmts, key=lambda f: pref.index(f.get("ext")) if f.get("ext") in pref else 99)[0]
        return kind, lang, fmt
    return None


def ts_to_sec(ts: str) -> float:
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    sec = 0.0
    for p in parts:
        sec = sec * 60 + float(p)
    return sec


def clean_caption(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)          # vtt/html tags
    text = re.sub(r"\{\\[^}]*\}", "", text)       # ass-style tags
    return html.unescape(text).replace("​", "").strip()


def parse_srt_vtt(text: str) -> list[dict]:
    segs = []
    blocks = re.split(r"\n\s*\n", text.replace("\r", ""))
    for b in blocks:
        m = re.search(r"((?:\d+:)?\d+:\d+[.,]\d+)\s*-->\s*((?:\d+:)?\d+:\d+[.,]\d+)", b)
        if not m:
            continue
        body = b[m.end():].split("\n", 1)[1] if "\n" in b[m.end():] else ""
        lines = [clean_caption(l) for l in body.split("\n")]
        t = " ".join(l for l in lines if l)
        if t:
            segs.append({"start": ts_to_sec(m.group(1)), "end": ts_to_sec(m.group(2)), "text": t})
    # rolling auto-captions repeat the previous line: drop exact repeats / prefixes
    out = []
    for s in segs:
        if out and (s["text"] == out[-1]["text"]):
            out[-1]["end"] = s["end"]
            continue
        if out and s["text"].startswith(out[-1]["text"]) and s["start"] - out[-1]["start"] < 8:
            s = {**s, "start": out[-1]["start"]}
            out[-1] = s
            continue
        out.append(s)
    return out


def parse_json3(text: str) -> list[dict]:
    data = json.loads(text)
    segs = []
    for ev in data.get("events", []):
        if "segs" not in ev:
            continue
        t = clean_caption("".join(s.get("utf8", "") for s in ev["segs"])).replace("\n", " ").strip()
        if not t:
            continue
        start = ev.get("tStartMs", 0) / 1000
        segs.append({"start": start, "end": start + ev.get("dDurationMs", 0) / 1000, "text": t})
    return segs


def parse_subtitle_file(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".json3":
        return parse_json3(text)
    return parse_srt_vtt(text)


def coverage(segs: list[dict], duration: float) -> float:
    if not segs or not duration:
        return 0.0
    return min(1.0, sum(max(0.0, s["end"] - s["start"]) for s in segs) / duration)


# --------------------------------------------------------------------------- text helpers

CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def join_text(a: str, b: str) -> str:
    if not a:
        return b
    if CJK.search(a[-1:]) or CJK.search(b[:1]):
        return a + b
    return a + " " + b


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def paragraphs(segs: list[dict], max_dur: float = 45, max_chars: int = 240, gap: float = 2.5) -> list[dict]:
    paras, cur = [], None
    for s in segs:
        t = s["text"].strip()
        if not t:
            continue
        if cur and (s["start"] - cur["end"] > gap or s["start"] - cur["start"] >= max_dur
                    or len(cur["text"]) >= max_chars):
            paras.append(cur)
            cur = None
        if cur is None:
            cur = {"start": s["start"], "end": s["end"], "text": t}
        else:
            cur["text"] = join_text(cur["text"], t)
            cur["end"] = s["end"]
    if cur:
        paras.append(cur)
    return paras


def est_tokens(text: str) -> int:
    cjk = len(CJK.findall(text))
    return int(cjk * 1.0 + (len(text) - cjk) / 3.8)


def write_srt(segs: list[dict], path: Path) -> None:
    def ts(x):
        ms = int(round(x * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    path.write_text("".join(f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{s['text']}\n\n"
                            for i, s in enumerate(segs, 1)), encoding="utf-8")


# --------------------------------------------------------------------------- media helpers

def ffprobe(path: str) -> dict:
    res = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                         capture_output=True, text=True)
    if res.returncode != 0:
        die(f"ffprobe cannot read {path}: {res.stderr.strip()[-300:]}")
    return json.loads(res.stdout)


def has_video_stream(probe: dict) -> bool:
    for s in probe.get("streams", []):
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            return True
    return False


def find_media(work: Path) -> Path | None:
    files = [p for p in (work / "media").glob("source.*") if p.suffix not in (".part", ".ytdl", ".json")]
    return files[0] if files else None


def uv_script(script: str, args: list[str]) -> list[str]:
    path = str(SCRIPTS / script)
    if shutil.which("uv"):
        return ["uv", "run", "--quiet", "--script", path, *args]
    log("uv not found; running with the current python (dependencies must already be installed)")
    return [sys.executable, path, *args]


def link_template(info: dict, url: str) -> str | None:
    ek = (info.get("extractor_key") or "").lower()
    if ek.startswith("bilibili") and info.get("id", "").startswith(("BV", "av")):
        base = info.get("webpage_url") or url
        m = re.search(r"[?&]p=(\d+)", base)
        vid = re.match(r"(BV\w{10}|av\d+)", info["id"]).group(1)
        return f"https://www.bilibili.com/video/{vid}?{'p=' + m.group(1) + '&' if m else ''}t={{sec}}"
    if ek == "youtube":
        return f"https://www.youtube.com/watch?v={info['id']}&t={{sec}}s"
    return None


# --------------------------------------------------------------------------- main pipeline

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="URL, BV/av id, App share text containing a URL, or a local video/audio file")
    ap.add_argument("--output-dir", help="root folder for results (default ~/video-digest)")
    ap.add_argument("--visual", choices=["auto", "on", "off"], help="keyframes + OCR")
    ap.add_argument("--burned-subs", choices=["auto", "on", "off"], help="OCR of hard-coded subtitles")
    ap.add_argument("--asr", choices=["auto", "force", "off"], default="auto",
                    help="auto: only when no usable subtitles; force: always transcribe audio")
    ap.add_argument("--language", help="spoken language hint for ASR, e.g. zh, en, ja (default: detect)")
    ap.add_argument("--asr-engine", choices=["auto", "mlx", "faster"])
    ap.add_argument("--asr-model")
    ap.add_argument("--ocr-langs", help="Apple Vision languages, e.g. zh-Hans,en-US or ja-JP,en-US")
    ap.add_argument("--max-frames", type=int)
    ap.add_argument("--max-height", type=int)
    ap.add_argument("--sub-region", help="burned-subtitle band as y0,y1 fractions of frame height, e.g. 0.75,1.0")
    ap.add_argument("--cookies-from-browser")
    ap.add_argument("--cookies-file")
    ap.add_argument("--no-cookies", action="store_true")
    ap.add_argument("--proxy")
    ap.add_argument("--keep-media", action="store_true", help="keep the downloaded video/audio")
    ap.add_argument("--force", action="store_true", help="redo every step, ignoring cached results")
    ap.add_argument("--redo", default="", help="comma list of cached steps to redo: subs,asr,visual,burned")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die(f"{tool} not found: brew install ffmpeg")
    cfg = load_config(args)
    t0 = time.time()
    kind, target = classify_input(args.input)
    out_root = Path(cfg["output_dir"]).expanduser()

    # ---- 1. metadata
    ytdlp = None
    if kind == "url":
        ytdlp = YtDlp(cfg, target)
        log(f"fetching metadata: {target}")
        res = ytdlp.run(["--dump-single-json", "--flat-playlist", "--write-subs", "--write-auto-subs",
                         "--skip-download", target])
        if res.returncode != 0 or not res.stdout.strip():
            die("yt-dlp could not read this link:\n" + explain_failure(res.stderr))
        info = json.loads(res.stdout)
        if info.get("_type") == "playlist":
            entries = [{"title": e.get("title"), "url": e.get("url") or e.get("webpage_url"),
                        "duration": e.get("duration")} for e in info.get("entries") or []]
            print(json.dumps({"playlist": info.get("title"), "count": len(entries), "entries": entries[:200]},
                             ensure_ascii=False, indent=1))
            log(f"this is a playlist/collection with {len(entries)} videos; run digest.py on individual entries")
            sys.exit(3)
        vid = f"{(info.get('extractor_key') or 'web').lower()}-{info.get('id')}"
        work = out_root / slug(vid, 80)
        duration = float(info.get("duration") or 0)
        has_video = any((f.get("vcodec") or "none") != "none" for f in info.get("formats") or []) \
            or (info.get("vcodec") or "none") != "none"
        meta = {
            "title": info.get("title"), "uploader": info.get("uploader") or info.get("channel"),
            "upload_date": info.get("upload_date"), "duration": duration,
            "url": info.get("webpage_url") or target,
            "platform": PLATFORM_NAMES.get((info.get("extractor_key") or "").lower(), info.get("extractor_key")),
            "id": info.get("id"), "description": (info.get("description") or "").strip(),
            "tags": info.get("tags") or [], "chapters": info.get("chapters") or [],
            "view_count": info.get("view_count"), "like_count": info.get("like_count"),
            "language": info.get("language"), "has_video": has_video,
            "link_template": link_template(info, target),
        }
    else:
        p = Path(target)
        st = p.stat()
        h = hashlib.sha1(f"{p}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:8]
        work = out_root / f"local-{slug(p.stem)}-{h}"
        probe = ffprobe(target)
        duration = float(probe.get("format", {}).get("duration") or 0)
        has_video = has_video_stream(probe)
        tags = probe.get("format", {}).get("tags", {})
        meta = {"title": tags.get("title") or p.stem, "uploader": tags.get("artist"), "upload_date": None,
                "duration": duration, "url": str(p), "platform": "local file", "id": p.name,
                "description": tags.get("comment", ""), "tags": [], "chapters": [], "language": None,
                "has_video": has_video, "link_template": None}
        info = None

    work.mkdir(parents=True, exist_ok=True)
    (work / "logs").mkdir(exist_ok=True)
    state_path = work / "state.json"
    state = {} if args.force or not state_path.exists() else json.loads(state_path.read_text())
    for step in filter(None, (s.strip() for s in args.redo.split(","))):
        state.pop(step, None)
    if info is not None:
        (work / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    (work / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"「{meta['title']}」 {fmt_ts(duration)} → {work}")

    # ---- 2. subtitles
    sub_segs, sub_desc = [], None
    if info is not None:
        choice = pick_subtitle(info)
        if choice:
            skind, lang, fmt = choice
            cached = state.get("subs", {})
            sub_file = Path(cached["file"]) if cached.get("lang") == lang and Path(cached.get("file", "")).exists() else None
            if sub_file is None:
                for old in work.glob("subs.*"):
                    old.unlink()
                flag = "--write-subs" if skind == "manual" else "--write-auto-subs"
                res = ytdlp.run(["--load-info-json", str(work / "info.json"), "--skip-download", flag,
                                 "--sub-langs", re.escape(lang), "--sub-format", fmt.get("ext") or "best",
                                 "-o", str(work / "subs.%(ext)s")])
                found = sorted(work.glob("subs.*"))
                if res.returncode != 0 or not found:
                    log("subtitle download failed, will use ASR instead:\n" + explain_failure(res.stderr))
                else:
                    sub_file = found[0]
            if sub_file:
                sub_segs = parse_subtitle_file(sub_file)
                if lang.lower().startswith("ai-"):
                    label = "B站 AI 字幕"
                elif skind == "auto":
                    label = "平台自动字幕"
                else:
                    label = "UP主字幕" if meta["platform"] == "Bilibili" else "官方字幕（作者上传）"
                state["subs"] = {"lang": lang, "kind": skind, "file": str(sub_file)}
                cov = coverage(sub_segs, duration)
                sub_desc = f"{label}（{lang}，覆盖 {cov:.0%} 时长）"
                log(f"subtitles: {sub_desc}, {len(sub_segs)} cues")
                if cov < 0.25 and duration > 60:
                    log("subtitle track covers too little of the video; treating it as unusable")
                    sub_desc += "，覆盖太少，另做转写"
        else:
            log("no subtitle track available")

    usable_subs = bool(sub_segs) and (coverage(sub_segs, duration) >= 0.25 or duration <= 60)
    do_asr = args.asr == "force" or (args.asr == "auto" and not usable_subs)
    vis_mode, bs_mode = cfg["visual"], cfg["burned_subs"]
    do_visual = has_video and (vis_mode == "on" or (vis_mode == "auto" and duration <= cfg["visual_max_minutes"] * 60))
    do_burned = has_video and (bs_mode == "on" or (bs_mode == "auto" and not usable_subs
                                                   and duration <= cfg["burned_subs_max_minutes"] * 60))
    if not has_video and (vis_mode == "on" or bs_mode == "on"):
        log("no video stream (audio only): skipping OCR")

    need_asr = do_asr and not (state.get("asr", {}).get("done") and (work / "asr.json").exists())
    need_visual = do_visual and not (state.get("visual", {}).get("done") and (work / "ocr.json").exists())
    need_burned = do_burned and not (state.get("burned", {}).get("done") and (work / "burned_subs.json").exists())

    # ---- 3. media
    media = None
    if need_asr or need_visual or need_burned:
        if kind == "local":
            media = Path(target)
        else:
            media = find_media(work)
            want_video = need_visual or need_burned
            if media is not None and want_video and not has_video_stream(ffprobe(str(media))):
                media.unlink()
                media = None
            if media is None:
                (work / "media").mkdir(exist_ok=True)
                h = int(cfg["max_height"])
                fmt = (f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b" if want_video else "ba/b")
                log(f"downloading {'video ≤' + str(h) + 'p' if want_video else 'audio'} …")
                src = ["--load-info-json", str(work / "info.json")] \
                    if (work / "info.json").exists() and time.time() - (work / "info.json").stat().st_mtime < 3600 \
                    else [target]
                res = ytdlp.run(src + ["-f", fmt, "-S", f"res:{h},vcodec:h264", "--merge-output-format", "mkv",
                                       "--concurrent-fragments", "4", "-o", str(work / "media" / "source.%(ext)s")])
                media = find_media(work)
                if res.returncode != 0 or media is None:
                    die("media download failed:\n" + explain_failure(res.stderr))
                log(f"downloaded {media.name} ({media.stat().st_size / 1e6:.0f} MB)")

    # ---- 4. ASR + OCR in parallel
    procs: dict[str, tuple[subprocess.Popen, object]] = {}
    # Platform language metadata is a reliable hint when present; otherwise Whisper detects it
    # (Chinese platforms host plenty of foreign-language videos, so no platform-based guess).
    lang_hint = args.language or ((meta.get("language") or "").split("-")[0] or None)
    if need_asr:
        a = [str(media), "--out-json", str(work / "asr.json"), "--context", meta.get("title") or ""]
        if lang_hint:
            a += ["--language", lang_hint]
        if cfg.get("asr_engine") and cfg["asr_engine"] != "auto":
            a += ["--engine", cfg["asr_engine"]]
        if cfg.get("asr_model"):
            a += ["--model", cfg["asr_model"]]
        logf = open(work / "logs" / "asr.log", "w")
        procs["asr"] = (subprocess.Popen(uv_script("transcribe.py", a), stdout=logf, stderr=subprocess.STDOUT), logf)
        log("transcribing audio locally (first run downloads the Whisper model, ~1.6 GB) …")
    if need_visual or need_burned:
        ocr_langs = cfg["ocr_langs"]
        if args.ocr_langs is None and ocr_langs == DEFAULTS["ocr_langs"] and lang_hint in OCR_LANGS:
            ocr_langs = OCR_LANGS[lang_hint]
        a = [str(media), "--out-dir", str(work), "--langs", ocr_langs, "--max-frames", str(cfg["max_frames"])]
        if not need_visual:
            a.append("--no-keyframes")
        if need_burned:
            a.append("--burned-subs")
        if args.sub_region:
            a += ["--sub-region", args.sub_region]
        logf = open(work / "logs" / "visual.log", "w")
        procs["visual"] = (subprocess.Popen(uv_script("visual.py", a), stdout=logf, stderr=subprocess.STDOUT), logf)
        log("extracting keyframes / OCR …" if need_visual else "OCR of burned-in subtitles …")

    failures = []
    started = time.time()
    while procs:
        for name, (p, logf) in list(procs.items()):
            if p.poll() is None:
                continue
            logf.close()
            del procs[name]
            if p.returncode == 0:
                log(f"{name} finished ({time.time() - started:.0f}s)")
                if name == "asr":
                    state["asr"] = {"done": True}
                else:
                    if need_visual:
                        state["visual"] = {"done": True}
                    if need_burned:
                        state["burned"] = {"done": True}
            else:
                tail = (work / "logs" / f"{name}.log").read_text(errors="replace").strip().splitlines()[-8:]
                failures.append(name)
                log(f"{name} FAILED (exit {p.returncode}); see logs/{name}.log\n  " + "\n  ".join(tail))
        if procs:
            time.sleep(1)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1))

    # ---- 5. assemble outputs
    asr = json.loads((work / "asr.json").read_text()) if do_asr and (work / "asr.json").exists() else None
    burned = json.loads((work / "burned_subs.json").read_text()) if do_burned and (work / "burned_subs.json").exists() else None
    visual = json.loads((work / "visual.json").read_text()) if (work / "visual.json").exists() else {}

    if asr and asr.get("segments"):
        segs = asr["segments"]
        source = f"本地语音转写（{asr.get('engine')} · {asr.get('model', '').split('/')[-1]} · 语言 {asr.get('language')}）"
        write_srt(segs, work / "transcript.srt")
        if usable_subs:
            source += f"；另有{sub_desc}"
    elif usable_subs:
        segs, source = sub_segs, sub_desc
        write_srt(segs, work / "transcript.srt")
    elif burned and burned.get("cues"):
        segs, source = burned["cues"], "画面硬字幕 OCR（音频中几乎没有可识别的语音）"
    else:
        segs, source = [], "无（没有字幕，语音转写也没有结果）"

    paras = paragraphs(segs)
    transcript = "\n\n".join(f"[{fmt_ts(p['start'])}] {p['text']}" for p in paras)
    (work / "transcript.md").write_text(f"# 文字稿 — {meta['title']}\n\n来源：{source}\n\n{transcript}\n",
                                        encoding="utf-8")

    speech_chars = sum(len(s["text"]) for s in segs)
    notes = []
    if asr and duration > 60 and speech_chars / (duration / 60) < 25:
        notes.append("语音很少（可能是纯音乐或无人声视频），请以画面文字/硬字幕为主。")
    if "asr" in failures:
        notes.append("语音转写失败，见 logs/asr.log。")
    if "visual" in failures:
        notes.append("画面分析失败，见 logs/visual.log。")
    if not has_video:
        notes.append("纯音频，没有画面可分析。")
    elif not do_visual:
        notes.append("未做关键帧 OCR（--visual off 或视频过长）；需要时加 --visual on 重跑。")

    build_digest(work, meta, source, transcript, segs, visual, burned, notes)
    if not cfg["keep_media"] and kind == "url":
        shutil.rmtree(work / "media", ignore_errors=True)
        # yt-dlp's full info (every format and caption URL) can be 15 MB; meta.json keeps what matters
        (work / "info.json").unlink(missing_ok=True)

    summary = {
        "workdir": str(work), "digest": str(work / "digest.md"), "transcript": str(work / "transcript.md"),
        "title": meta["title"], "duration": fmt_ts(duration), "text_source": source,
        "subtitle_coverage": round(coverage(sub_segs, duration), 2) if sub_segs else None,
        "transcript_chars": speech_chars, "keyframes": visual.get("frames", 0),
        "contact_sheets": visual.get("sheets", []), "burned_sub_cues": len((burned or {}).get("cues", [])),
        "failed_steps": failures, "elapsed_sec": round(time.time() - t0),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if not segs and not visual.get("ocr_lines"):
        sys.exit(4)


def build_digest(work: Path, meta: dict, source: str, transcript: str, segs: list[dict],
                 visual: dict, burned: dict | None, notes: list[str]) -> None:
    lines = [f"# {meta['title']}", ""]
    info_bits = [b for b in (meta.get("platform"), meta.get("uploader"),
                             meta.get("upload_date") and f"{meta['upload_date'][:4]}-{meta['upload_date'][4:6]}-{meta['upload_date'][6:]}",
                             f"时长 {fmt_ts(meta['duration'])}") if b]
    lines += [f"- 来源：{' · '.join(info_bits)}", f"- 链接：{meta['url']}"]
    if meta.get("link_template"):
        lines.append(f"- 时间戳链接格式：{meta['link_template']}（{{sec}} 换成秒数）")
    lines.append(f"- 文字来源：{source}")
    if visual.get("frames"):
        lines.append(f"- 画面：{visual['frames']} 张去重关键帧（frames/），OCR 共 {visual.get('ocr_lines', 0)} 行文字"
                     f"（{visual.get('engine')}，全部在下方/ocr.md）；概览拼图 {', '.join(visual.get('sheets', []))}"
                     + (f" 均匀抽取了其中 {visual['sheet_frames']} 张" if visual.get("sheet_frames") else ""))
    if burned is not None:
        lines.append(f"- 硬字幕 OCR：{len(burned.get('cues', []))} 条（burned_subs.srt）")
    for n in notes:
        lines.append(f"- 注意：{n}")
    lines.append(f"- 工作目录：{work}")

    desc = meta.get("description") or ""
    if desc and desc != "-":
        lines += ["", "## 简介", desc[:1500] + ("…" if len(desc) > 1500 else "")]
    if meta.get("tags"):
        lines += ["", "标签：" + "、".join(meta["tags"][:20])]
    if meta.get("chapters"):
        lines += ["", "## 官方章节"]
        for c in meta["chapters"]:
            sec = int(c.get("start_time", 0))
            ts = f"[{fmt_ts(sec)}]({meta['link_template'].format(sec=sec)})" if meta.get("link_template") \
                else f"[{fmt_ts(sec)}]"
            lines.append(f"- {ts} {c.get('title')}")

    ocr_md = (work / "ocr.md")
    if ocr_md.exists():
        ocr_text = ocr_md.read_text(encoding="utf-8")
        lines += [""]
        if est_tokens(ocr_text) <= 6000:
            lines += [ocr_text.replace("# 画面文字", "## 画面文字", 1)]
        else:
            lines += ["## 画面文字（OCR）",
                      f"内容较长（约 {est_tokens(ocr_text)} tokens），见 ocr.md（按时间排列，每帧一个 ### 标题）。"]

    if burned and burned.get("cues") and not source.startswith("画面硬字幕"):
        bs = "\n".join(f"[{fmt_ts(c['start'])}] {c['text']}" for c in burned["cues"])
        lines += ["", "## 画面硬字幕（OCR，可用来校对转写中的专有名词）"]
        lines += [bs if len(bs) <= 5000 else bs[:5000] + "\n…（完整内容见 burned_subs.srt）"]

    lines += ["", "## 文字稿"]
    tokens = est_tokens(transcript)
    n_lines = transcript.count("\n") + 1
    if tokens <= 12000:
        lines += [transcript or "（无）"]
    elif tokens <= 24000:
        lines += [f"文字稿约 {tokens} tokens（{n_lines} 行），见 transcript.md，一次 Read 即可读完。"]
    else:
        per = max(50, int(n_lines * 20000 / tokens))
        lines += [f"文字稿较长：约 {tokens} tokens / {n_lines} 行，见 transcript.md。"
                  f"请分段读完（Read 的 offset/limit，每次约 {per} 行，共 {-(-n_lines // per)} 次）再总结。"]
    (work / "digest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
