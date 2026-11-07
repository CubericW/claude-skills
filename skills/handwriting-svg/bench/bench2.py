"""第二轮评测: 调研推荐的 Gatos 收缩/膨胀、Potrace 参数、Richardson-Lucy 去模糊。基线 = 当前 skill(局部半高)。"""
import re, subprocess, sys
from pathlib import Path
import cv2, numpy as np
sys.argv_bak = sys.argv; sys.argv = ["x"]
exec(open(Path(__file__).parent / "bench_run.py").read().split('if __name__ == "__main__":')[0])
sys.argv = sys.argv_bak


def rl_deconv(img, sigma, iters):
    """Richardson-Lucy 去模糊(高斯 PSF), 在"墨迹量"域上做: d = 255 - img。"""
    d = np.maximum(255.0 - img.astype(np.float32), 1e-3)
    est = d.copy()
    for _ in range(iters):
        conv = cv2.GaussianBlur(est, (0, 0), sigma)
        est *= cv2.GaussianBlur(d / np.maximum(conv, 1e-3), (0, 0), sigma)
    return np.clip(255.0 - est, 0, 255).astype(np.uint8)


def gatos(bmp_ink, n):
    """Gatos 2006 步骤 1-2: 收缩去孤立点, 带质心约束的膨胀补针孔/细缝(不加粗)。bmp_ink: bool 墨迹。"""
    n = max(3, int(n) | 1)
    fg = bmp_ink.astype(np.float32)
    k = (n, n)
    cnt = cv2.boxFilter(fg, -1, k, normalize=False)
    bg_cnt = n * n - cnt
    out = bmp_ink & ~(bg_cnt > 0.9 * n * n)                       # 收缩
    fg = out.astype(np.float32)
    cnt = cv2.boxFilter(fg, -1, k, normalize=False)
    yy, xx = np.mgrid[0:fg.shape[0], 0:fg.shape[1]].astype(np.float32)
    sx = cv2.boxFilter(fg * xx, -1, k, normalize=False); sy = cv2.boxFilter(fg * yy, -1, k, normalize=False)
    cx = sx / np.maximum(cnt, 1); cy = sy / np.maximum(cnt, 1)
    swell = (~out) & (cnt > 0.05 * n * n) & (np.abs(cx - xx) <= 0.25 * n) & (np.abs(cy - yy) <= 0.25 * n)
    return out | swell


def trace2(soft, scale, smooth, alphamax, turd, opttol, gatos_n=0):
    pad = 2
    m = cv2.copyMakeBorder(soft, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    m = cv2.resize(m, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    if smooth > 0:
        m = cv2.GaussianBlur(m, (0, 0), smooth * scale)
    ink = m >= 0.5
    if gatos_n:
        ink = gatos(ink, gatos_n)
    bmp = np.where(ink, 0, 255).astype(np.uint8)
    hh, ww = bmp.shape
    r = subprocess.run([h.POTRACE, "-", "-b", "svg", "-o", "-", "--flat", "-t", str(turd), "-a", str(alphamax), "-O", str(opttol)],
                       input=b"P5\n%d %d\n255\n" % (ww, hh) + bmp.tobytes(), capture_output=True, check=True)
    hit = re.search(rb'<g transform="([^"]+)"[^>]*>(.*?)</g>', r.stdout, re.S)
    if not hit:
        return ""
    return f'<g transform="translate({-pad},{-pad}) scale({1 / scale:g}) {hit.group(1).decode()}">{hit.group(2).decode()}</g>'


import psf_est

def run2(img, v):
    if v.get("rl"):
        img = rl_deconv(img, v["rl"][0], v["rl"][1])
    if v.get("rl_auto"):
        norm = h.flatten_background(img); Bq, _ = h.binarize(norm)
        sg = psf_est.estimate_sigma(norm, Bq, h.stroke_width(Bq))
        if sg and sg > 0.3:
            img = rl_deconv(img, sg * v["rl_auto"][0], v["rl_auto"][1])
    h.trace_orig = h.trace
    lh = None

    def patched(soft, scale, smooth, alphamax, turd, opttol):
        return trace2(soft, scale, v.get("smooth", smooth), v.get("alpha", alphamax), v.get("turd", turd),
                      v.get("opttol", opttol), v.get("gatos", 0))
    h.trace = patched
    try:
        return run(img, {"w": "local_1.5"})
    finally:
        h.trace = h.trace_orig


def perim(mask):
    cnts, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    return sum(cv2.arcLength(c, True) for c in cnts)


V = {
    "基线(当前 skill)": {},
    "Potrace -a1.2 -O0.5 -t12": {"alpha": 1.2, "opttol": 0.5, "turd": 12},
    "平滑 1.4": {"smooth": 1.4},
    "Gatos 收缩/膨胀 n=9": {"gatos": 9},
    "Gatos n=15": {"gatos": 15},
    "RL 去模糊 σ0.8×8": {"rl": (0.8, 8)},
    "RL σ0.8×15": {"rl": (0.8, 15)},
    "RL 自动σ×10": {"rl_auto": (1.0, 10)},
    "RL 自动σ×15": {"rl_auto": (1.0, 15)},
    "RL 自动σ×1.2 ×10": {"rl_auto": (1.2, 10)},
}
names = sys.argv[2].split("|") if len(sys.argv) > 2 else list(V)
for name in names:
    rows = {"32": [], "64": []}
    for f in sorted(Path(sys.argv[1]).glob("img_*.png")):
        img = cv2.imread(str(f), 0); gt = cv2.imread(str(f).replace("img_", "gt4_"), 0) > 127
        out = run2(img, V[name])
        f1, th, br, nz, n = metrics(out, gt)
        rows["32" if "_32_" in f.name else "64"].append((f1, th, br, nz, perim(out) / perim(gt)))
    msg = []
    for k, r in rows.items():
        a = np.array(r)
        msg.append(f"{k}px: F1 {a[:,0].mean():.3f} 粗细 {a[:,1].mean():.2f} 断笔 {a[:,2].sum():3.0f} 噪点 {a[:,3].sum():2.0f} 毛刺度 {a[:,4].mean():.2f}")
    print(f"{name:24s} | " + " | ".join(msg), flush=True)
