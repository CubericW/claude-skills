"""第四轮: 结构步骤在原分辨率, 只有轮廓(局部半高)在放大图上算。对比: 原流程 / 全流程放大 / 只轮廓放大。"""
import subprocess, sys
from pathlib import Path
import cv2, numpy as np
ARGS4 = list(sys.argv); sys.argv = ["x"]
exec(open(Path(__file__).parent / "bench3.py").read().split("MODES = {")[0])
sys.argv = ARGS4

TURD = True

def run_edges(img, img_up, k_up):
    """原分辨率做结构(含去模糊、去线、去噪), 放大图只用于计算墨迹浓度/轮廓。"""
    norm_n = h.flatten_background(img)
    b0, _ = h.binarize(norm_n)
    sg = psf_est.estimate_sigma(norm_n, b0, h.stroke_width(b0))
    img_d = rl_deconv(img, sg, 10) if sg and sg > 0.3 else img
    norm = h.flatten_background(img_d)
    B, thr = h.binarize(norm)
    paper = float(np.median(norm))
    Bs = h.binarize(norm, paper - 0.3 * (paper - thr))[0]
    hr = h.find_rules(Bs, 0.25)
    sw0 = h.stroke_width(cv2.bitwise_and(B, cv2.bitwise_not(hr[0]))) if hr else h.stroke_width(B)
    B, px = h.strip_all(B, norm, Bs, 0.25, sw0)
    sw = h.stroke_width(B); B = h.drop_blobs(B, sw * sw); sw = h.stroke_width(B)
    B = h.clean_components(B, max(4.0, 0.4 * sw * sw), int(3 * sw), True)
    # 放大图: 去模糊按放大后的尺度重新估计
    nu = h.flatten_background(img_up)
    bu = cv2.resize(B, (nu.shape[1], nu.shape[0]), interpolation=cv2.INTER_NEAREST)
    sgu = psf_est.estimate_sigma(nu, bu, sw * k_up)
    if sgu and sgu > 0.3:
        nu = h.flatten_background(rl_deconv(img_up, sgu, 10))
    pxu = cv2.resize(cv2.dilate(px, np.ones((3, 3), np.uint8)), (nu.shape[1], nu.shape[0]), interpolation=cv2.INTER_NEAREST)
    bu0 = bu
    bu = cv2.dilate(bu, np.ones((3, 3), np.uint8))  # 原分辨率掩膜放大后边缘是台阶, 外扩一点让轮廓由灰度决定
    W = h.ink_weight(nu, bu, sw * k_up, pxu, 0.5)
    W[(pxu > 0) & (bu0 == 0)] = 0  # 横线像素按外扩前的掩膜抑制
    soft = h.soft_mask(bu, bu, W)
    ts = max(1, round(3 / k_up))
    turd = int(0.4 * (sw * k_up * ts) ** 2) if TURD else 2
    body = h.trace(soft, ts, 1.0, 1.0, turd, 0.2)
    Hh, Ww = nu.shape
    return rasterize(h.svg_doc(Ww, Hh, body, "#000"), 4.0 / k_up) < 128

MODES = {"原流程(当前 skill)": ("none", False),
         "只轮廓放大: 双三次": ("bicubic", True), "只轮廓放大: waifu2x n3": ("cunet:3:2", True),
         "只轮廓放大: waifu2x photo n1": ("upconv_7_photo:1:2", True), "只轮廓放大: waifu2x n1": ("cunet:1:2", True)}
for name, (mode, edges_only) in MODES.items():
    rows = {"32": [], "64": []}
    for f in sorted(Path(sys.argv[1]).glob("img_*.png")):
        gt = cv2.imread(str(f).replace("img_", "gt4_"), 0) > 127
        img = cv2.imread(str(f), 0)
        up_img, sc = up(f, mode)
        if edges_only:
            out = run_edges(img, up_img, sc)
        else:
            out = run2(up_img, {"rl_auto": (1.0, 10)}, 4.0 / sc)
        o = np.zeros_like(gt); o[:min(out.shape[0], gt.shape[0]), :min(out.shape[1], gt.shape[1])] = out[:gt.shape[0], :gt.shape[1]]
        f1, th, br, nz, n = metrics(o, gt)
        rows["32" if "_32_" in f.name else "64"].append((f1, th, br, nz))
    msg = []
    for k, r in rows.items():
        a = np.array(r)
        msg.append(f"字高{k}px: F1 {a[:,0].mean():.3f} 粗细 {a[:,1].mean():.2f} 断笔 {a[:,2].sum():3.0f} 噪点 {a[:,3].sum():2.0f}")
    print(f"{name:24s} | " + " | ".join(msg), flush=True)
