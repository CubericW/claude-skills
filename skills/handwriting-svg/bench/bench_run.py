"""在合成评测集上比较各种后处理方案。指标对照标准答案(gt):
F1(像素) | 粗细比(输出面积/标准面积) | 断笔(一个标准连通域被拆成几块的多余块数 + 整块丢失数) | 噪点(与标准笔画不沾边的输出块)"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path.home() / ".claude/skills/handwriting-svg/scripts"))
sys.path.insert(0, str(Path(__file__).parent))
import hw2svg as h
from rast import rasterize


def weights(norm, B, labels, st, suppress, mode, sw, edge=0.5):
    g = cv2.GaussianBlur(norm, (0, 0), 0.8).astype(np.float32)
    raw = norm.astype(np.float32)
    paper = float(np.median(g[B == 0]))
    if mode in ("comp_raw", "comp_blur"):
        src = raw if mode == "comp_raw" else g
        core = np.full(g.shape, 255, np.float32)
        for i in range(1, len(st)):
            x, y, cw, ch, _ = st[i]
            sub, m = core[y:y + ch, x:x + cw], labels[y:y + ch, x:x + cw] == i
            sub[m] = np.percentile(src[y:y + ch, x:x + cw][m], 10)
        core = cv2.erode(core, np.ones((3, 3), np.uint8))
    else:  # local: 局部半高(Bernsen 式), 半径约一个笔画宽
        r = max(2, int(round(float(mode.split("_")[1]) * sw)))
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        core = cv2.erode(g, k)
    span = np.maximum(paper - core, 1.0)
    w = np.clip(0.5 + (core + edge * span - g) / span, 0, 1)
    w[core >= paper - 10] = 0
    w[(suppress > 0) & (B == 0)] = 0
    return w.astype(np.float32)


def run(img, v):
    norm = h.flatten_background(img)
    if v.get("nlm"):
        sig = 1.4826 * float(np.median(np.abs(norm.astype(np.float32) - cv2.medianBlur(norm, 5))))
        norm = cv2.fastNlMeansDenoising(norm, None, h=max(3.0, v["nlm"] * sig), templateWindowSize=7, searchWindowSize=21)
    B, thr = h.binarize(norm)
    if v.get("hyst"):  # 滞后阈值: 弱阈值内、与强像素相连的部分也算墨迹
        paper = float(np.median(norm))
        weak = h.binarize(norm, thr + v["hyst"] * (paper - thr))[0]
        n, lab, _, _ = cv2.connectedComponentsWithStats(weak, connectivity=8)
        good = np.zeros(n, bool); good[np.unique(lab[B > 0])] = True; good[0] = False
        B = (good[lab] * 255).astype(np.uint8)
    paper = float(np.median(norm))
    Bs = h.binarize(norm, paper - 0.3 * (paper - thr))[0]
    rule_px = np.zeros_like(B)
    hr = h.find_rules(Bs, 0.25)
    sw0 = h.stroke_width(cv2.bitwise_and(B, cv2.bitwise_not(hr[0]))) if hr else h.stroke_width(B)
    B, px = h.strip_all(B, norm, Bs, 0.25, sw0)
    rule_px |= px
    if v.get("hyst2") or v.get("nadapt"):  # 去线之后再做滞后阈值, 线像素不参与
        g = cv2.GaussianBlur(norm, (0, 0), 0.8).astype(np.float32)
        sig = 1.4826 * float(np.median(np.abs(norm.astype(np.float32) - cv2.medianBlur(norm, 5))))
        tw = paper - v["nadapt"] * sig if v.get("nadapt") else thr + v["hyst2"] * (paper - thr)
        weak = ((g < tw) & (cv2.dilate(px, np.ones((3, 3), np.uint8)) == 0)).astype(np.uint8) * 255
        weak = cv2.bitwise_or(weak, B)
        n, lab, st, _ = cv2.connectedComponentsWithStats(weak, connectivity=8)
        good = np.zeros(n, bool); good[np.unique(lab[B > 0])] = True
        if v.get("evidence"):  # 完全由淡墨构成的块: 面积够大且平均对比度够高也收
            swb = h.stroke_width(B)
            for i in np.flatnonzero(~good)[1:]:
                x, y, cw, ch, a = st[i]
                if a >= max(8, 0.8 * swb * swb):
                    good[i] = float((paper - g[y:y + ch, x:x + cw][lab[y:y + ch, x:x + cw] == i]).mean()) >= v["evidence"] * sig
        good[0] = False
        B = (good[lab] * 255).astype(np.uint8)
    sw = h.stroke_width(B)
    B = h.drop_blobs(B, sw * sw)
    sw = h.stroke_width(B)
    B = h.clean_components(B, max(4.0, 0.4 * sw * sw), int(3 * sw), True)
    n, labels, st, _ = cv2.connectedComponentsWithStats(B, connectivity=8)
    W = weights(norm, B, labels, st, cv2.dilate(rule_px, np.ones((3, 3), np.uint8)), v.get("w", "comp_raw"), sw, v.get("edge", 0.5))
    soft = h.soft_mask(B, B, W)
    if v.get("close"):
        soft = cv2.morphologyEx(soft, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (v["close"], v["close"])))
    body = h.trace(soft, 3, v.get("smooth", 1.0), v.get("alpha", 1.0), 2, 0.2)
    Hh, Ww = img.shape
    return rasterize(h.svg_doc(Ww, Hh, body, "#000"), 4.0) < 128


def metrics(out, gt):
    tp = (out & gt).sum(); p = tp / max(1, out.sum()); r = tp / max(1, gt.sum())
    f1 = 2 * p * r / max(1e-9, p + r)
    ng, lg = cv2.connectedComponents(gt.astype(np.uint8), connectivity=8)
    no, lo = cv2.connectedComponents(out.astype(np.uint8), connectivity=8)
    gtd = cv2.dilate(gt.astype(np.uint8), np.ones((13, 13), np.uint8)) > 0
    breaks = 0
    for i in range(1, ng):
        ids = np.unique(lo[(lg == i) & out]); ids = ids[ids > 0]
        breaks += max(0, len(ids) - 1) if len(ids) else 1
    noise = sum(1 for j in range(1, no) if not gtd[lo == j].any())
    return f1, out.sum() / max(1, gt.sum()), breaks, noise, ng - 1


VARIANTS = {
    "当前(墨芯取原图)": {"w": "comp_raw"},
    "墨芯取模糊图": {"w": "comp_blur"},
    "局部半高 r=1.0sw": {"w": "local_1.0"},
    "局部半高 r=1.5sw": {"w": "local_1.5"},
    "局部半高+滞后阈值": {"w": "local_1.5", "hyst": 0.5},
    "局部半高+NLM去噪": {"w": "local_1.5", "nlm": 1.0},
    "局部半高+滞后+NLM": {"w": "local_1.5", "hyst": 0.5, "nlm": 1.0},
    "局部半高+滞后+闭运算3": {"w": "local_1.5", "hyst": 0.5, "close": 3},
    "局部半高+去线后滞后0.5": {"w": "local_1.5", "hyst2": 0.5},
    "局部半高+噪声自适应4σ": {"w": "local_1.5", "nadapt": 4.0},
    "局部半高+噪声4σ+证据6σ": {"w": "local_1.5", "nadapt": 4.0, "evidence": 6.0},
    "局部半高+噪声3σ+证据6σ": {"w": "local_1.5", "nadapt": 3.0, "evidence": 6.0},
}
if __name__ == "__main__":
    bench = Path(sys.argv[1])
    names = sys.argv[2].split(",") if len(sys.argv) > 2 else list(VARIANTS)
    files = sorted(bench.glob("img_*.png"))
    for name in names:
        rows = {"photo": [], "scan": []}
        for f in files:
            img = cv2.imread(str(f), 0)
            gt = cv2.imread(str(f).replace("img_", "gt4_"), 0) > 127
            out = run(img, VARIANTS[name])
            rows["photo" if "_32_" in f.name else "scan"].append(metrics(out, gt))
        msg = []
        for k, r in rows.items():
            a = np.array(r)
            msg.append(f"{k}: F1 {a[:,0].mean():.3f} 粗细 {a[:,1].mean():.2f} 断笔 {a[:,2].sum():3.0f} 噪点 {a[:,3].sum():3.0f} (共{a[:,4].sum():.0f}块)")
        print(f"{name:20s} | " + " | ".join(msg), flush=True)
