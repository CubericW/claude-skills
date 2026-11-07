#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mlx-whisper>=0.4; sys_platform == 'darwin' and platform_machine == 'arm64'",
#   "faster-whisper>=1.1; sys_platform != 'darwin' or platform_machine != 'arm64'",
#   "zhconv>=1.4",
# ]
# ///
"""Local speech-to-text for video-digest.

Apple Silicon uses mlx-whisper (GPU); everything else uses faster-whisper (CPU/CUDA, VAD).
Writes {"engine", "model", "language", "duration", "segments": [{start, end, text}]}.

  uv run --script transcribe.py audio_or_video --out-json asr.json [--language zh] [--context "title"]
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import sys
import time
from collections import Counter

DEFAULT_MODELS = {"mlx": "mlx-community/whisper-large-v3-turbo", "faster": "large-v3-turbo"}

# Whisper "fills" silence/music with phrases memorized from subtitle credits.
JUNK = re.compile(
    r"(请不吝点赞|点赞[、，,]?\s*订阅|订阅[、，,]?\s*转发|打赏支持|明镜与点点|字幕由.*(提供|制作)|Amara\.org|"
    r"YoYo Television|优优独播剧场|中文字幕[—-]+|字幕志愿者|本字幕由|感谢观看[!！]*$|謝謝觀看|"
    r"Thank(s| you) for watching|Subtitles by|ご視聴ありがとうございました|MBC 뉴스)",
    re.I,
)

PROMPTS = {
    # Biases Whisper towards simplified characters and full-width punctuation.
    "zh": "以下是普通话的转写，使用简体中文，带有逗号、句号、问号等标点符号。",
    "en": "The following is a transcript with proper punctuation and capitalization.",
}


def log(msg: str) -> None:
    print(f"[transcribe] {msg}", file=sys.stderr, flush=True)


def build_prompt(language: str | None, context: str) -> str | None:
    parts = []
    if context:
        parts.append(context.strip()[:120])  # title words help with names and jargon
    if language in PROMPTS:
        parts.append(PROMPTS[language])
    return (" " if language not in ("zh", "ja", "ko") else "").join(parts) or None


def collapse_repeats(text: str) -> str:
    # "好的好的好的好的好的" / "the the the the" -> one occurrence
    return re.sub(r"(.{1,20}?)(?:\s*\1){3,}", r"\1", text)


def clean_segments(segs: list[dict], language: str | None) -> list[dict]:
    out = []
    for s in segs:
        t = collapse_repeats((s.get("text") or "").strip())
        if not t:
            continue
        if JUNK.search(t) and len(t) < 40:
            continue
        nsp, lp = s.get("no_speech_prob"), s.get("avg_logprob")
        if nsp is not None and lp is not None and nsp > 0.6 and lp < -1.0:
            continue
        out.append({"start": round(float(s["start"]), 2), "end": round(float(s["end"]), 2), "text": t})
    # the same line repeated many times in a row is a decoding loop
    deduped, run = [], Counter()
    for s in out:
        if deduped and s["text"] == deduped[-1]["text"]:
            run[s["text"]] += 1
            if run[s["text"]] >= 2:
                deduped[-1]["end"] = s["end"]
                continue
        else:
            run.clear()
        deduped.append(s)
    if language == "zh":  # Whisper often mixes in traditional characters for Mandarin
        import zhconv
        for s in deduped:
            s["text"] = zhconv.convert(s["text"], "zh-cn")
    return deduped


# --------------------------------------------------------------------------- engines

def run_mlx(path: str, model: str, language: str | None, context: str) -> tuple[list[dict], str, float]:
    import mlx_whisper
    from mlx_whisper.audio import SAMPLE_RATE, load_audio

    audio = load_audio(path)
    duration = len(audio) / SAMPLE_RATE
    log(f"audio {duration / 60:.1f} min, model {model}")
    if not language:
        # detect on a 30 s window that skips most intros/jingles
        start = int(min(max(0.0, duration * 0.15), max(0.0, duration - 30)) * SAMPLE_RATE)
        probe = mlx_whisper.transcribe(audio[start:start + 30 * SAMPLE_RATE], path_or_hf_repo=model,
                                       condition_on_previous_text=False, verbose=None)
        language = probe.get("language") or "en"
        log(f"detected language: {language}")
    t = time.time()
    res = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=model,
        language=language,
        initial_prompt=build_prompt(language, context),
        # keeps punctuation/script style consistent across windows; loops are caught by the
        # compression-ratio fallback and clean_segments()
        condition_on_previous_text=True,
        compression_ratio_threshold=2.2,
        verbose=False,
    )
    log(f"transcribed in {time.time() - t:.0f}s ({duration / max(1e-6, time.time() - t):.1f}x realtime)")
    return res.get("segments", []), language, duration


def run_faster(path: str, model: str, language: str | None, context: str) -> tuple[list[dict], str, float]:
    from faster_whisper import WhisperModel

    m = WhisperModel(model, device="auto", compute_type="auto")
    if not language:
        try:
            from faster_whisper.audio import decode_audio
            audio = decode_audio(path)
            language, _, _ = m.detect_language(audio)
        except Exception:  # older faster-whisper: let transcribe() detect
            language = None
    seg_iter, info = m.transcribe(path, language=language, initial_prompt=build_prompt(language, context),
                                  vad_filter=True, beam_size=5, condition_on_previous_text=True)
    language = language or info.language
    log(f"audio {info.duration / 60:.1f} min, language {language}, model {model}")
    segs = []
    for s in seg_iter:
        segs.append({"start": s.start, "end": s.end, "text": s.text,
                     "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob})
        if len(segs) % 50 == 0:
            log(f"… {s.end / 60:.1f} min")
    return segs, language, info.duration


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("media")
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--language", help="ISO code such as zh, en, ja; default: detect")
    ap.add_argument("--context", default="", help="video title etc., used as a vocabulary hint")
    ap.add_argument("--engine", choices=["auto", "mlx", "faster"], default="auto")
    ap.add_argument("--model")
    args = ap.parse_args()

    engine = args.engine
    if engine == "auto":
        engine = "mlx" if sys.platform == "darwin" and platform.machine() == "arm64" else "faster"
    model = args.model or DEFAULT_MODELS[engine]
    language = (args.language or "").lower().split("-")[0] or None
    if language in ("cmn", "chi", "zho"):
        language = "zh"

    t0 = time.time()
    runner = run_mlx if engine == "mlx" else run_faster
    raw, language, duration = runner(args.media, model, language, args.context)
    segs = clean_segments(raw, language)
    out = {"engine": "mlx-whisper" if engine == "mlx" else "faster-whisper", "model": model,
           "language": language, "duration": round(duration, 2), "elapsed": round(time.time() - t0, 1),
           "segments": segs}
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    log(f"{len(segs)} segments ({len(raw) - len(segs)} dropped as noise) -> {args.out_json}")


if __name__ == "__main__":
    main()
