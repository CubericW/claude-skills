import sys
from pathlib import Path
import math
import cv2, numpy as np
sys.path.insert(0, str(Path.home() / ".claude/skills/handwriting-svg/scripts"))
import hw2svg as h

def estimate_sigma(norm, B, sw):
    """由笔画边缘的最大梯度估计高斯模糊 σ: 阶跃边缘模糊后, 最大梯度 = 对比度 / (σ·√(2π))。
    只看两侧对比度充分的边缘(粗笔画), 取较锐利的一侧分位, 避免细笔画(比光斑还细)把 σ 估大。"""
    g = cv2.GaussianBlur(norm, (0, 0), 0.6).astype(np.float32)   # 轻微去噪, 后面扣除其贡献
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3) / 8; gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3) / 8
    G = np.hypot(gx, gy)
    paper = float(np.median(g[B == 0]))
    r = max(2, int(round(1.5 * sw)))
    core = cv2.erode(g, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
    C = paper - core
    mid = np.abs(g - (core + paper) / 2) < 0.15 * C                 # 半高处
    nms = G >= cv2.dilate(G, np.ones((3, 3), np.uint8)) - 1e-3       # 梯度极大
    strong = C > 0.6 * np.percentile(C[B > 0], 90)
    sel = mid & nms & strong & (G > 1)
    if sel.sum() < 50:
        return None
    # 3x3 中心差分在锐利边缘上会低估斜率: 对高斯边缘, 量到的斜率/对比度 = erf(1/(σ√2))/2, 查表反解 σ
    ratio = float(np.percentile(G[sel] / C[sel], 70))
    grid = np.linspace(0.3, 4.0, 400)
    f = np.array([math.erf(1 / (x * math.sqrt(2))) / 2 for x in grid])
    s = float(np.interp(ratio, f[::-1], grid[::-1]))
    return float(np.sqrt(max(0.0, s * s - 0.6 ** 2)))                 # 扣掉预去噪的 0.6

if __name__ == "__main__":
    for f in sys.argv[1:]:
        img = cv2.imread(f, 0)
        norm = h.flatten_background(img); B, thr = h.binarize(norm)
        print(Path(f).name, "σ估计", round(estimate_sigma(norm, B, h.stroke_width(B)) or -1, 2))
