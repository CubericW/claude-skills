#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "numpy>=1.24",
#   "pillow>=10.1",
#   "pyobjc-framework-Vision>=10; sys_platform == 'darwin'",
#   "rapidocr>=2; sys_platform != 'darwin'",
#   "onnxruntime>=1.16; sys_platform != 'darwin'",
# ]
# ///
"""Visual pass for video-digest: keyframes, on-screen text (OCR), contact sheets and
burned-in (hard-coded) subtitles.

Keyframes: sample 1 frame/s as tiny grayscale thumbnails, split the timeline where the
picture changes (cuts or gradual drift), keep the last stable frame of each segment, merge
segments that return to an earlier picture, then OCR the full-resolution frames. Frames whose
text is contained in the next frame (slide builds) or identical to the previous one are merged.

Burned subtitles: OCR a band at the bottom of the frame twice a second, keep centred lines,
merge consecutive samples with the same text into cues.

Outputs in --out-dir: frames/*.jpg, sheets/*.jpg, ocr.json, ocr.md, visual.json,
burned_subs.json/.srt (with --burned-subs).

OCR engine: Apple Vision on macOS (local, fast, good Chinese), RapidOCR elsewhere.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

THUMB = 128  # long edge of the thumbnails used for change detection
PIX = 20     # grey-level difference that counts as a changed pixel


def log(msg: str) -> None:
    print(f"[visual] {msg}", file=sys.stderr, flush=True)


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text).lower()


# --------------------------------------------------------------------------- ffmpeg

def probe(video: str) -> tuple[int, int, float]:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", video],
                       capture_output=True, text=True, check=True)
    info = json.loads(r.stdout)
    vs = next(s for s in info["streams"] if s.get("codec_type") == "video")
    w, h = int(vs["width"]), int(vs["height"])
    rot = 0
    for sd in vs.get("side_data_list", []) or []:
        rot = int(sd.get("rotation", rot) or 0)
    rot = rot or int((vs.get("tags") or {}).get("rotate", 0) or 0)
    if abs(rot) % 180 == 90:  # ffmpeg auto-rotates frames; report the displayed size
        w, h = h, w
    dur = float(info.get("format", {}).get("duration") or vs.get("duration") or 0)
    return w, h, dur


def ffmpeg(args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    base = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
    attempts = [base + ["-hwaccel", "videotoolbox"]] if sys.platform == "darwin" else []
    attempts.append(base)
    res = None
    for b in attempts:
        res = subprocess.run(b + args, capture_output=True)
        if res.returncode == 0 and (not capture or res.stdout):
            return res
    raise RuntimeError(f"ffmpeg failed: {res.stderr.decode(errors='replace')[-500:]}")


def thumb_size(w: int, h: int) -> tuple[int, int]:
    if w >= h:
        return THUMB, max(48, round(THUMB * h / w))
    return max(48, round(THUMB * w / h)), THUMB


def sample_thumbs(video: str, fps: float, fast: bool, tw: int, th: int) -> np.ndarray:
    pre = ["-skip_frame", "nokey"] if fast else []
    res = ffmpeg(pre + ["-i", video, "-an", "-sn", "-vf",
                        f"fps={fps},scale={tw}:{th}:flags=area,format=gray", "-f", "rawvideo", "pipe:1"],
                 capture=True)
    arr = np.frombuffer(res.stdout, np.uint8)
    n = len(arr) // (tw * th)
    return arr[: n * tw * th].reshape(n, th, tw)


def grab_frame(video: str, t: float, path: Path) -> None:
    ffmpeg(["-ss", f"{t:.2f}", "-i", video, "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-q:v", "2", str(path)])


# --------------------------------------------------------------------------- OCR engines

class VisionOCR:
    name = "Apple Vision"

    def __init__(self, langs: list[str]):
        import Vision  # noqa: F401  (pyobjc)
        from Foundation import NSURL
        self.V, self.NSURL, self.langs = Vision, NSURL, langs

    def __call__(self, path: Path) -> list[dict]:
        import objc
        V = self.V
        with objc.autorelease_pool():
            handler = V.VNImageRequestHandler.alloc().initWithURL_options_(self.NSURL.fileURLWithPath_(str(path)), {})
            req = V.VNRecognizeTextRequest.alloc().init()
            req.setRecognitionLevel_(V.VNRequestTextRecognitionLevelAccurate)
            req.setUsesLanguageCorrection_(True)
            req.setRecognitionLanguages_(self.langs)
            ok, _err = handler.performRequests_error_([req], None)
            out = []
            for obs in (req.results() or []) if ok else []:
                cands = obs.topCandidates_(1)
                if not cands:
                    continue
                bb = obs.boundingBox()  # normalized, origin bottom-left
                x, y, w, h = bb.origin.x, bb.origin.y, bb.size.width, bb.size.height
                out.append({"text": str(cands[0].string()), "conf": round(float(cands[0].confidence()), 3),
                            "box": [round(x, 4), round(1 - y - h, 4), round(w, 4), round(h, 4)]})
            return out


class RapidOCREngine:
    name = "RapidOCR"

    def __init__(self, langs: list[str]):
        try:
            from rapidocr import RapidOCR
        except ImportError:
            from rapidocr_onnxruntime import RapidOCR
        self.eng = RapidOCR()

    def __call__(self, path: Path) -> list[dict]:
        W, H = Image.open(path).size
        res = self.eng(str(path))
        if hasattr(res, "txts"):  # rapidocr >= 2
            items = list(zip(res.boxes, res.txts, res.scores)) if res.txts else []
        else:                     # rapidocr_onnxruntime: (result, elapse)
            items = [(r[0], r[1], r[2]) for r in (res[0] or [])]
        out = []
        for box, txt, score in items:
            xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
            out.append({"text": str(txt), "conf": round(float(score), 3),
                        "box": [round(v, 4) for v in (min(xs) / W, min(ys) / H,
                                                      (max(xs) - min(xs)) / W, (max(ys) - min(ys)) / H)]})
        return out


def make_ocr(engine: str, langs: list[str]):
    if engine in ("auto", "vision") and sys.platform == "darwin":
        try:
            return VisionOCR(langs)
        except Exception as e:  # pyobjc missing or Vision unavailable
            if engine == "vision":
                raise
            log(f"Apple Vision unavailable ({e}); using RapidOCR")
    return RapidOCREngine(langs)


def reading_order(lines: list[dict], min_conf: float = 0.3) -> list[dict]:
    keep = [l for l in lines if l["conf"] >= min_conf and re.search(r"[\w一-鿿]", l["text"])]
    return sorted(keep, key=lambda l: (round((l["box"][1] + l["box"][3] / 2) / 0.025), l["box"][0]))


def ocr_many(ocr, paths: list[Path], workers: int = 4) -> list[list[dict]]:
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(ocr, paths))


# --------------------------------------------------------------------------- keyframes

def default_band(w: int, h: int) -> tuple[float, float]:
    """Where burned-in subtitles usually sit (fractions of the frame height)."""
    return (0.55, 0.92) if h > w else (0.68, 0.99)


def band_is_busy(thumbs: np.ndarray, band: tuple[float, float]) -> bool:
    """True when the band keeps changing while the rest of the picture holds still: subtitles."""
    n, H, _ = thumbs.shape
    y0, y1 = int(H * band[0]), int(np.ceil(H * band[1]))
    if n < 4 or y0 < 2:
        return False
    indep = 0
    for i in range(0, n - 1, 512):
        d = np.abs(np.diff(thumbs[i:i + 513].astype(np.int16), axis=0)) > PIX
        indep += int(((d[:, y0:y1].mean(axis=(1, 2)) > 0.02) & (d[:, :y0].mean(axis=(1, 2)) < 0.01)).sum())
    return indep / (n - 1) >= 0.08


def subtitle_like(line: dict, band: tuple[float, float]) -> bool:
    x, y, w, h = line["box"]
    return band[0] <= y + h / 2 <= band[1] and 0.2 <= x + w / 2 <= 0.8


def matches_cue(text: str, cues: list[str]) -> bool:
    t = norm(text)
    for c in cues:
        sm = SequenceMatcher(None, t, c)
        if sm.real_quick_ratio() >= 0.5 and sm.quick_ratio() >= 0.5 and sm.ratio() >= 0.5:
            return True
    return False


def stable_mask(thumbs: np.ndarray, ignore_band: tuple[float, float] | None) -> np.ndarray:
    """Pixels that can signal a new picture. Areas that change most of the time (speaker inset,
    tickers, moving background) and the burned-subtitle band would split the timeline at every
    gesture or subtitle line, so they are left out unless almost the whole frame is moving."""
    n, H, W = thumbs.shape
    region = np.ones((H, W), bool)
    if ignore_band:
        region[int(H * ignore_band[0]):int(np.ceil(H * ignore_band[1])), :] = False
    freq, active = np.zeros((H, W)), 0
    for i in range(0, max(0, n - 1), 512):
        d = np.abs(np.diff(thumbs[i:i + 513].astype(np.int16), axis=0)) > PIX
        moving = d.mean(axis=(1, 2)) > 0.002  # repeated frames (keyframe-only decoding) say nothing
        freq += d[moving].sum(axis=0)
        active += int(moving.sum())
    freq /= max(1, active)
    stable = region & (freq < 0.25)
    return stable if stable.mean() >= 0.15 else region


def pick_keyframes(thumbs: np.ndarray, fps: float, duration: float, max_frames: int,
                   ignore_band: tuple[float, float] | None = None, cut: float = 0.015,
                   drift: float = 0.04, min_len: float = 2.0, dup: float = 0.01) -> list[dict]:
    n = len(thumbs)
    if n == 0:
        return []
    mask = stable_mask(thumbs, ignore_band)

    def changed(a: np.ndarray, b: np.ndarray) -> float:  # share of watched pixels that changed
        return float((np.abs(a.astype(np.int16) - b.astype(np.int16)) > PIX)[mask].mean())

    bounds, anchor = [0], thumbs[0]
    for i in range(1, n):  # hard cut, or gradual drift away from the segment's first picture
        if changed(thumbs[i], thumbs[i - 1]) > cut or changed(thumbs[i], anchor) > drift:
            bounds.append(i)
            anchor = thumbs[i]
    bounds.append(n)
    segs = []
    for s, e in zip(bounds[:-1], bounds[1:]):
        if (e - s) / fps < min_len:
            continue  # transitions, fast cuts, constant motion
        rep = e - 1 if e - s >= 2 else s  # last sample: slide fully built, transition not started
        segs.append({"idx": rep, "start": s / fps, "end": min(duration, e / fps), "dur": (e - s) / fps})

    # never leave a long stretch without a frame (talking heads, vlogs, gameplay)
    max_gap = max(60.0, duration / max(1, max_frames // 2))
    points = [0.0] + [sg["idx"] / fps for sg in segs] + [duration]
    for a, b in zip(points[:-1], points[1:]):
        k = int((b - a) // max_gap)
        for j in range(1, k + 1):
            t = a + (b - a) * j / (k + 1)
            half = (b - a) / (2 * (k + 1))
            segs.append({"idx": min(n - 1, int(t * fps)), "start": max(0.0, t - half),
                         "end": min(duration, t + half), "dur": half})
    segs.sort(key=lambda sg: sg["idx"])

    kept: list[dict] = []
    for sg in segs:  # a picture we have already seen (back to an earlier slide) is not new
        twin = next((k for k in kept if changed(thumbs[sg["idx"]], thumbs[k["idx"]]) < dup), None)
        if twin:
            twin["dur"] += sg["dur"]
        else:
            kept.append(sg)
    if len(kept) > max_frames:  # keep what stayed on screen longest
        kept = sorted(sorted(kept, key=lambda k: -k["dur"])[:max_frames], key=lambda k: k["start"])
    return kept


TOKEN = re.compile(r"[\d\s.,:;%+\-−=×÷()/<>|°$€¥]+")


def compact_lines(texts: list[str]) -> list[str]:
    """Axis ticks, matrix entries and stray glyphs come out as dozens of one-token lines;
    fold runs of them into a single line so the readable text stands out."""
    def flush(run: list[str]) -> str:
        toks = " ".join(run).split()
        if len(toks) > 24:  # pixel grids, matrices: the gist is enough
            return " ".join(toks[:12]) + f" …（共 {len(toks)} 个数字/符号）"
        return " ".join(toks)

    out, run = [], []
    for t in texts:
        s = t.strip()
        if TOKEN.fullmatch(s) or (len(s) <= 2 and not re.search(r"[一-鿿]", s)):
            run.append(s)
            continue
        if run:
            out.append(flush(run))
            run = []
        out.append(flush([s]) if TOKEN.fullmatch(s) else s)
    if run:
        out.append(flush(run))
    return out


def find_overlays(per_frame: list[list[dict]], ratio: float = 0.5, min_count: int = 3) -> set[str]:
    """Text present on most frames: channel watermark, logo, fixed lower-third."""
    if len(per_frame) < min_count:
        return set()
    cnt = Counter()
    for lines in per_frame:
        cnt.update({norm(l["text"]) for l in lines})
    return {t for t, c in cnt.items() if t and c >= max(min_count, ratio * len(per_frame))}


def merge_by_text(frames: list[dict]) -> list[dict]:
    """Drop frames whose text is a subset of the next frame (slide builds) or repeats the previous one."""
    def found(x: str, b_lines: list[str], b_joined: str) -> bool:
        if x in b_joined:
            return True
        for y in b_lines:  # OCR of the same text varies a little between frames (l/1, e/c)
            sm = SequenceMatcher(None, x, y)
            if sm.real_quick_ratio() >= 0.8 and sm.quick_ratio() >= 0.8 and sm.ratio() >= 0.8:
                return True
        return False

    def contained(a: dict, b: dict) -> bool:
        al = [x for x in (norm(l["text"]) for l in a["lines"]) if x]
        bl = [x for x in (norm(l["text"]) for l in b["lines"]) if x]
        bj = "".join(bl)
        return bool(al) and sum(1 for x in al if found(x, bl, bj)) / len(al) >= 0.85

    out: list[dict] = []
    for f in frames:
        if out and out[-1]["lines"] and f["lines"]:
            prev = out[-1]
            if contained(f, prev):            # same or less text than the previous frame
                prev["end"] = max(prev["end"], f["end"])
                f["drop"] = True
                continue
            if contained(prev, f):            # previous frame was an earlier build of this one
                f["start"] = prev["start"]
                prev["drop"] = True
                out[-1] = f
                continue
        out.append(f)
    return out


def contact_sheets(frames: list[dict], out_dir: Path, portrait: bool, max_sheets: int) -> tuple[list[str], int]:
    cols, rows, tile_w = (4, 2, 300) if portrait else (3, 3, 512)
    per = cols * rows
    if not frames:
        return [], 0
    chosen = frames
    if len(frames) > per * max_sheets:
        idx = np.linspace(0, len(frames) - 1, per * max_sheets).round().astype(int)
        chosen = [frames[i] for i in sorted(set(idx))]
    try:
        font = ImageFont.load_default(size=24)
    except TypeError:
        font = ImageFont.load_default()
    names = []
    for si in range(0, len(chosen), per):
        batch = chosen[si:si + per]
        tiles = []
        for f in batch:
            im = Image.open(f["path"]).convert("RGB")
            im = im.resize((tile_w, int(im.height * tile_w / im.width)))
            d = ImageDraw.Draw(im)
            label = fmt_ts(f["t"])
            d.rectangle([0, 0, 12 + 14 * len(label), 32], fill=(0, 0, 0))
            d.text((6, 3), label, fill=(255, 255, 255), font=font)
            tiles.append(im)
        th = max(t.height for t in tiles)
        r = (len(tiles) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * tile_w + (cols - 1) * 6, r * th + (r - 1) * 6), (255, 255, 255))
        for i, t in enumerate(tiles):
            sheet.paste(t, ((i % cols) * (tile_w + 6), (i // cols) * (th + 6)))
        name = f"sheets/sheet_{si // per + 1:02d}.jpg"
        sheet.save(out_dir / name, quality=85)
        names.append(name)
    return names, len(chosen)


def run_keyframes(video: str, out_dir: Path, ocr, w: int, h: int, duration: float, max_frames: int,
                  max_sheets: int, sub_band: tuple[float, float] | None = None,
                  sub_texts: list[str] | None = None) -> dict:
    t0 = time.time()
    fast = duration > 3600  # keyframe-only decoding: much faster, slide changes found within one GOP
    thumbs = sample_thumbs(video, 1.0, fast, *thumb_size(w, h))
    log(f"scanned {len(thumbs)} thumbnails in {time.time() - t0:.0f}s{' (keyframe-only)' if fast else ''}")
    band = sub_band or default_band(w, h)
    busy = sub_band is not None or band_is_busy(thumbs, band)
    if busy and sub_band is None:
        log("subtitle band detected: ignoring it for scene changes")
    picks = pick_keyframes(thumbs, 1.0, duration, max_frames, ignore_band=band if busy else None)
    fdir = out_dir / "frames"
    shutil.rmtree(fdir, ignore_errors=True)
    fdir.mkdir(parents=True)
    (out_dir / "sheets").mkdir(exist_ok=True)
    for old in (out_dir / "sheets").glob("*.jpg"):
        old.unlink()
    frames = []
    for i, p in enumerate(picks, 1):
        t = p["idx"] / 1.0
        frames.append({"t": t, "start": p["start"], "end": p["end"],
                       "path": fdir / f"kf_{i:03d}_{int(t) // 60:02d}m{int(t) % 60:02d}s.jpg"})
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda f: grab_frame(video, f["t"], f["path"]), frames))
    frames = [f for f in frames if f["path"].exists()]
    log(f"{len(frames)} candidate keyframes; running OCR ({ocr.name}) …")
    results = ocr_many(ocr, [f["path"] for f in frames])
    for f, lines in zip(frames, results):
        # Subtitle lines are already in the transcript / burned_subs.srt; repeating them here would
        # bury the slides. With burned-sub cues, a line must also match one; without, position decides.
        f["lines"] = [l for l in reading_order(lines)
                      if not (busy and subtitle_like(l, band)
                              and (sub_texts is None or matches_cue(l["text"], sub_texts)))]
    overlays = find_overlays([f["lines"] for f in frames])
    for f in frames:
        f["lines"] = [l for l in f["lines"] if norm(l["text"]) not in overlays]
    merged = merge_by_text(frames)
    for f in frames:
        if f.get("drop"):
            f["path"].unlink(missing_ok=True)
    sheets, sheet_frames = contact_sheets(merged, out_dir, portrait=h > w, max_sheets=max_sheets)

    overlay_display = sorted({l["text"] for lines in results for l in lines if norm(l["text"]) in overlays})
    md = ["# 画面文字（OCR）", "",
          f"关键帧 {len(merged)} 张（OCR：{ocr.name}）。时间为该画面出现的区间，图片在 frames/ 下。"]
    if overlay_display:
        md.append("几乎每帧都有的文字（水印/台标/固定标题，下文已省略）：" + "；".join(overlay_display[:10]))
    n_lines = 0
    for f in merged:
        if not f["lines"]:
            continue
        texts = compact_lines([l["text"] for l in f["lines"]])
        n_lines += len(texts)
        md += ["", f"### [{fmt_ts(f['start'])}–{fmt_ts(f['end'])}] {f['path'].relative_to(out_dir)}"]
        md += texts
    (out_dir / "ocr.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (out_dir / "ocr.json").write_text(json.dumps({
        "engine": ocr.name, "overlays": overlay_display,
        "frames": [{"file": str(f["path"].relative_to(out_dir)), "t": f["t"], "start": f["start"], "end": f["end"],
                    "lines": f["lines"]} for f in merged]}, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"keyframes done: {len(merged)} frames, {n_lines} text lines, {len(sheets)} sheets "
        f"({time.time() - t0:.0f}s)")
    return {"frames": len(merged), "ocr_lines": n_lines, "overlays": overlay_display, "sheets": sheets,
            "sheet_frames": sheet_frames}


# --------------------------------------------------------------------------- burned-in subtitles

def run_burned_subs(video: str, out_dir: Path, ocr, w: int, h: int, region: tuple[float, float] | None,
                    fps: float = 2.0) -> tuple[dict, list[dict], tuple[float, float]]:
    t0 = time.time()
    y0, y1 = region or ((0.55, 0.92) if h > w else (0.68, 0.99))
    tmp = out_dir / "_burned_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    ffmpeg(["-i", video, "-an", "-sn", "-vf",
            f"fps={fps},crop=iw:ih*{y1 - y0:.3f}:0:ih*{y0:.3f},scale='min(1280,iw)':-2",
            "-q:v", "3", str(tmp / "%06d.jpg")])
    files = sorted(tmp.glob("*.jpg"))
    # only OCR a sample when the band actually changed
    todo, owner, prev = [], [], None
    for f in files:
        im = Image.open(f).convert("L")
        small = np.asarray(im.resize((160, max(8, round(160 * im.height / im.width)))), np.float32)
        if prev is None or small.shape != prev.shape or np.abs(small - prev).mean() >= 1.5:
            todo.append(f)
            prev = small
        owner.append(len(todo) - 1)
    log(f"burned subs: {len(files)} samples, {len(todo)} need OCR")
    results = ocr_many(ocr, todo)

    def sub_lines(lines):
        out = []
        for l in reading_order(lines, 0.4):
            cx = l["box"][0] + l["box"][2] / 2
            if 0.2 <= cx <= 0.8 and l["box"][2] < 0.98:  # subtitles are centred
                out.append(l["text"].strip())
        return out

    texts = [sub_lines(results[o]) if o >= 0 else [] for o in owner]
    # text that never leaves the band (show title, watermark) is not a subtitle
    cnt = Counter(t for ls in texts for t in set(map(norm, ls)))
    static = {t for t, c in cnt.items() if c >= max(6, 0.4 * len(texts))}
    samples = [" ".join(x for x in ls if norm(x) not in static) for ls in texts]

    cues: list[dict] = []
    for i, text in enumerate(samples):
        t = i / fps
        if len(norm(text)) < 2:
            continue
        last = cues[-1] if cues else None
        if last and t - last["end"] <= 1.01 / fps and \
                SequenceMatcher(None, norm(text), norm(last["variants"][-1])).ratio() >= 0.6:
            last["end"] = t + 1 / fps
            last["variants"].append(text)
        else:
            cues.append({"start": t, "end": t + 1 / fps, "variants": [text]})
    out = []
    for c in cues:
        counts = Counter(c["variants"])
        best = max(counts, key=lambda v: (counts[v], len(v)))
        if out and SequenceMatcher(None, norm(best), norm(out[-1]["text"])).ratio() >= 0.8 \
                and c["start"] - out[-1]["end"] <= 1.5:
            out[-1]["end"] = round(c["end"], 2)
            continue
        out.append({"start": round(c["start"], 2), "end": round(c["end"], 2), "text": best})
    shutil.rmtree(tmp, ignore_errors=True)

    def ts(x):
        ms = int(round(x * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    (out_dir / "burned_subs.srt").write_text("".join(f"{i}\n{ts(c['start'])} --> {ts(c['end'])}\n{c['text']}\n\n"
                                                     for i, c in enumerate(out, 1)), encoding="utf-8")
    (out_dir / "burned_subs.json").write_text(json.dumps({"region": [y0, y1], "fps": fps, "cues": out},
                                                         ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"burned subs done: {len(out)} cues ({time.time() - t0:.0f}s)")
    return {"burned_cues": len(out), "burned_region": [y0, y1]}, out, (y0, y1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--langs", default="zh-Hans,en-US", help="OCR languages (Apple Vision codes)")
    ap.add_argument("--engine", choices=["auto", "vision", "rapidocr"], default="auto")
    ap.add_argument("--max-frames", type=int, default=80)
    ap.add_argument("--max-sheets", type=int, default=3)
    ap.add_argument("--no-keyframes", action="store_true")
    ap.add_argument("--burned-subs", action="store_true")
    ap.add_argument("--sub-region", help="y0,y1 fractions of the frame height for burned subtitles")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    w, h, duration = probe(args.video)
    log(f"video {w}x{h}, {duration / 60:.1f} min")
    ocr = make_ocr(args.engine, [x.strip() for x in args.langs.split(",") if x.strip()])
    summary_path = out_dir / "visual.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    summary.update({"engine": ocr.name, "width": w, "height": h})
    t0 = time.time()
    sub_band, sub_texts = None, None
    if args.burned_subs:  # first, so keyframe detection can ignore the subtitle band
        region = tuple(float(x) for x in args.sub_region.split(",")) if args.sub_region else None
        stats, cues, band = run_burned_subs(args.video, out_dir, ocr, w, h, region)
        summary.update(stats)
        if len(cues) >= 3:
            sub_band, sub_texts = band, [norm(c["text"]) for c in cues]
    if not args.no_keyframes:
        summary.update(run_keyframes(args.video, out_dir, ocr, w, h, duration, args.max_frames,
                                     args.max_sheets, sub_band, sub_texts))
    summary["elapsed"] = round(time.time() - t0, 1)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
