#!/usr/bin/env python3
"""把逐字/逐行 SVG 拼接成新的一行或一页。

用法:
  # 1) 按给定顺序拼接若干 SVG(可用通配符)
  stitch.py out/chars/L01_*.svg -o line.svg
  # 2) 按文字从 manifest 里挑字(需要 hw2svg.py --ocr 生成的文字标签), \\n 换行, 空格留空
  stitch.py --manifest out/manifest.json --text "天气很好\\n明月" -o new.svg
  # 3) 把若干行纵向排成一页
  stitch.py out/lines/L01.svg out/lines/L03.svg --vertical -o page.svg
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)


def load(path: Path):
    root = ET.parse(path).getroot()
    vb = root.get("viewBox")
    if vb:
        vx, vy, vw, vh = map(float, re.split(r"[ ,]+", vb.strip()))
    else:
        vx = vy = 0.0
        vw = float(re.sub(r"[a-z]+$", "", root.get("width", "0")))
        vh = float(re.sub(r"[a-z]+$", "", root.get("height", "0")))
    inner = "".join(ET.tostring(ch, encoding="unicode") for ch in root)
    inner = re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", inner)
    return {"inner": inner, "x": vx, "y": vy, "w": vw, "h": vh, "path": path}


def pick_from_manifest(manifest: Path, text: str, mode: str):
    """按文字挑字。每个字附带所在行的基线位置(画布顶到该行字底的典型距离), 拼接时按基线对齐;
    并给出整页的典型字高, 用于统一缩放(保留书写时字与字的真实大小关系)。"""
    m = json.loads(manifest.read_text("utf-8"))
    base = manifest.parent
    bank: dict[str, list[tuple[Path, float]]] = {}
    sizes = []
    for L in m["lines"]:
        cs = [c for c in L["chars"] if c.get("ink_bbox")]
        if not cs:
            continue
        hs = [c["ink_bbox"][3] for c in cs]
        big = [c for c in cs if c["ink_bbox"][3] >= 0.5 * max(hs)]  # 排除“一”、标点等矮字
        sizes.append(sorted(c["ink_bbox"][3] for c in big)[len(big) // 2])
        bl = sorted(c["ink_bbox"][1] + c["ink_bbox"][3] - c["bbox"][1] for c in big)[len(big) // 2]
        for c in cs:
            if c.get("text"):
                bank.setdefault(c["text"], []).append((base / c["svg"], float(bl)))
    size = sorted(sizes)[len(sizes) // 2] if sizes else None
    used: dict[str, int] = {}
    seq, missing = [], []
    for ch in text:
        if ch == "\n":
            seq.append("\n")
        elif ch == " ":
            seq.append(" ")
        elif ch in bank:
            opts = bank[ch]
            if mode == "random":
                seq.append(random.choice(opts))
            elif mode == "cycle":  # 同一个字多次出现时轮流使用不同的手写样本, 更自然
                seq.append(opts[used.get(ch, 0) % len(opts)])
                used[ch] = used.get(ch, 0) + 1
            else:
                seq.append(opts[0])
        else:
            missing.append(ch)
            seq.append(" ")
    if missing:
        print(f"提示: manifest 中没有这些字, 已留空: {''.join(dict.fromkeys(missing))}", file=sys.stderr)
    return seq, size


def main(argv=None):
    p = argparse.ArgumentParser(description="拼接手写 SVG")
    p.add_argument("svgs", nargs="*", type=Path, help="按顺序拼接的 SVG 文件")
    p.add_argument("--manifest", type=Path, help="hw2svg.py 生成的 manifest.json")
    p.add_argument("--text", help="按文字挑字(配合 --manifest); \\n 表示换行")
    p.add_argument("--pick", choices=["first", "cycle", "random"], default="cycle",
                   help="同一个字有多个样本时的选法")
    p.add_argument("-o", "--out", type=Path, required=True)
    p.add_argument("--height", type=float,
                   help="目标字高(像素)。按文字挑字时默认保持原始大小; 拼文件时默认统一到各画布高度的中位数")
    p.add_argument("--no-scale", action="store_true", help="保持原始尺寸, 不统一高度")
    p.add_argument("--gap", type=float, default=0.0, help="字间距(像素, 可为负)")
    p.add_argument("--space", type=float, default=0.5, help="空格宽度(相对行高)")
    p.add_argument("--wrap", type=int, default=0, help="每行最多几个元素, 0=不自动换行")
    p.add_argument("--line-gap", type=float, default=0.3, help="行距(相对行高)")
    p.add_argument("--vertical", action="store_true", help="纵向排列(拼行成页)")
    p.add_argument("--color", help="统一改填充色, 如 #1a1a1a")
    a = p.parse_args(argv)

    size = None
    if a.text is not None:
        if not a.manifest:
            raise SystemExit("--text 需要配合 --manifest 使用")
        seq, size = pick_from_manifest(a.manifest, a.text.replace("\\n", "\n"), a.pick)
    else:
        seq = list(a.svgs)
    items = []
    for x in seq:
        if isinstance(x, str):
            items.append(x)
        elif isinstance(x, tuple):  # (路径, 基线)
            it = load(x[0])
            it["base"] = x[1]
            items.append(it)
        else:
            items.append(load(x))
    real = [x for x in items if isinstance(x, dict)]
    if not real:
        raise SystemExit("没有可拼接的 SVG")

    hs = sorted(x["h"] for x in real)
    if size:  # 按文字挑字: 整页统一比例, 保留字与字的真实大小关系, 按基线对齐
        k = (a.height / size) if a.height else 1.0
        for it in real:
            it["s"] = k
        H = size * k * 1.25  # 空格/行距的参考行高
    else:
        H = a.height or hs[len(hs) // 2]
        for it in real:
            it["s"] = 1.0 if a.no_scale else H / it["h"]
    rows, row = [], []
    for it in items:
        if it == "\n" or (a.wrap and len(row) >= a.wrap and not a.vertical):
            rows.append(row)
            row = []
            if it == "\n":
                continue
        row.append(it)
    rows.append(row)

    parts, max_w, y = [], 0.0, 0.0
    if a.vertical:  # 每个元素占一行, 左对齐
        for it in (x for r in rows for x in r):
            if not isinstance(it, dict):
                continue
            s = it["s"]
            parts.append((0.0, y, s, it))
            max_w = max(max_w, it["w"] * s)
            y += it["h"] * s + a.line_gap * H
        total_h = y - a.line_gap * H
    else:
        for r in rows:
            x, row_h = 0.0, 0.0
            # 有基线信息时按基线对齐: 各字画布顶部下移, 使"画布顶到行内字底"的距离一致
            bl = max((it["base"] * it["s"] for it in r if isinstance(it, dict) and "base" in it), default=0.0)
            for it in r:
                if it == " ":
                    x += a.space * H
                    continue
                s = it["s"]
                dy = bl - it["base"] * s if "base" in it else 0.0
                parts.append((x, y + dy, s, it))
                x += it["w"] * s + a.gap
                row_h = max(row_h, dy + it["h"] * s)
            max_w = max(max_w, x - a.gap)
            y += (row_h or H) + a.line_gap * H
        total_h = y - a.line_gap * H

    body = []
    for x, y, s, it in parts:
        inner = it["inner"]
        if a.color:
            inner = re.sub(r'fill="(?!none)[^"]*"', f'fill="{a.color}"', inner)
        body.append(f'<g transform="translate({x:.2f},{y:.2f}) scale({s:.4f}) '
                    f'translate({-it["x"]:g},{-it["y"]:g})">{inner}</g>')
    W, Ht = round(max_w, 2), round(total_h, 2)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                     f'<svg xmlns="{SVG_NS}" width="{W}" height="{Ht}" viewBox="0 0 {W} {Ht}">\n'
                     + "\n".join(body) + "\n</svg>\n", "utf-8")
    print(f"拼接 {len(parts)} 个元素 -> {a.out} ({W}x{Ht})")


if __name__ == "__main__":
    main()
