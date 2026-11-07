#!/usr/bin/env python3
"""手写字迹照片/扫描件 -> 无背景、无横线的透明矢量 SVG（整页 / 逐行 / 逐字）。

流程:
  OpenCV  光照拉平 -> 二值化 -> 纠偏 -> 横线/竖线检测与去除(含被横线截断的笔画修补)
          -> 去噪 -> 连通域分行、分字
  RapidOCR(可选, --ocr) 用 PP-OCR 模型给出行框/单字框和识别文字, 指导切分并给文件打标签
  Potrace 位图 -> 贝塞尔曲线 SVG
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

POTRACE = shutil.which("potrace") or "/opt/homebrew/bin/potrace"
WAIFU2X = next(iter(sorted((Path(__file__).resolve().parent.parent / "tools").glob("waifu2x-ncnn-vulkan-*/waifu2x-ncnn-vulkan"))), None)
IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif", ".pdf"}


# ---------------------------------------------------------------- 基础工具

@dataclass
class Comp:
    idx: int
    x: int
    y: int
    w: int
    h: int
    area: int
    cx: float
    cy: float

    @property
    def x1(self):
        return self.x + self.w

    @property
    def y1(self):
        return self.y + self.h


def runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """布尔序列中连续为 True 的区间 [a, b)。"""
    f = np.concatenate([[False], flags.astype(bool), [False]])
    d = np.diff(f.astype(np.int8))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()))


def read_image(path: Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None and shutil.which("sips"):  # HEIC 等 OpenCV 读不了的格式交给 macOS sips
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "in.png"
            subprocess.run(["sips", "-s", "format", "png", str(path), "--out", str(out)],
                           capture_output=True)
            img = cv2.imread(str(out), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"无法读取图片: {path}")
    return img


def load_pages(path: Path) -> list[tuple[str, np.ndarray]]:
    """读取输入, 返回 [(名称, BGR 图)]。PDF 优先无损抽取内嵌 JPEG(手机扫描件多为每页一张), 否则用 Ghostscript 300dpi 渲染。"""
    if path.suffix.lower() != ".pdf":
        return [(path.stem, read_image(path))]
    data = path.read_bytes()
    imgs = []
    for m in re.finditer(rb"<<(?:(?!>>).)*?/Subtype\s*/Image(?:(?!>>).)*?/DCTDecode(?:(?!>>).)*?>>\s*stream\r?\n",
                         data, re.S):
        end = data.find(b"endstream", m.end())
        im = cv2.imdecode(np.frombuffer(data[m.end():end], np.uint8), cv2.IMREAD_COLOR)
        if im is not None and min(im.shape[:2]) >= 300:
            imgs.append(im)
    if not imgs and shutil.which("gs"):
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=png16m", "-r300",
                            f"-sOutputFile={td}/p%03d.png", str(path)], capture_output=True)
            imgs = [cv2.imread(str(f), cv2.IMREAD_COLOR) for f in sorted(Path(td).glob("p*.png"))]
    if not imgs:
        raise SystemExit(f"无法从 PDF 中取出页面图像: {path} (可安装 ghostscript: brew install ghostscript)")
    if len(imgs) == 1:
        return [(path.stem, imgs[0])]
    return [(f"{path.stem}_p{i:02d}", im) for i, im in enumerate(imgs, 1)]


def rotate(img: np.ndarray, angle: float, border) -> np.ndarray:
    """绕中心旋转并扩展画布, 不裁掉四角。angle>0 为逆时针。"""
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(h * sin + w * cos + 0.5), int(h * cos + w * sin + 0.5)
    M[0, 2] += nw / 2 - w / 2
    M[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(img, M, (nw, nh), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=border)


def blit(dst: np.ndarray, src: np.ndarray, ox: int, oy: int) -> np.ndarray:
    """把 src 贴到 dst 的 (ox, oy) 处, 自动裁掉越界部分。"""
    H, W = dst.shape[:2]
    h, w = src.shape[:2]
    ax0, ay0, ax1, ay1 = max(0, ox), max(0, oy), min(W, ox + w), min(H, oy + h)
    if ax1 > ax0 and ay1 > ay0:
        dst[ay0:ay1, ax0:ax1] = src[ay0 - oy:ay1 - oy, ax0 - ox:ax1 - ox]
    return dst


def crop(arr: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
    """取 arr[y0:y1, x0:x1], 越界部分补 0。"""
    return blit(np.zeros((y1 - y0, x1 - x0), arr.dtype), arr, -x0, -y0)


# ---------------------------------------------------------------- 预处理

def flatten_background(gray: np.ndarray) -> np.ndarray:
    """估计纸张亮度(去掉笔画后的大尺度背景)并相除, 消除阴影/光照不均, 纸面≈255。"""
    h, w = gray.shape
    f = max(1, min(h, w) // 500)
    small = cv2.resize(gray, (w // f, h // f), interpolation=cv2.INTER_AREA) if f > 1 else gray
    k = max(7, (min(small.shape) // 40) | 1)
    bg = cv2.dilate(small, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    bg = cv2.medianBlur(bg, min(k, 255))
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR)
    norm = gray.astype(np.float32) / np.maximum(bg.astype(np.float32), 1.0) * 255.0
    return np.clip(norm, 0, 255).astype(np.uint8)


def estimate_blur(norm: np.ndarray, B: np.ndarray, sw: float) -> float | None:
    """估计成像模糊(高斯 σ, 像素): 阶跃边缘被高斯模糊后, 3x3 中心差分量到的 斜率/对比度 = erf(1/(σ√2))/2, 查表反解。
    只看对比度充分的(粗)笔画在半高处的梯度极大点, 取较锐利的一侧分位; 笔画比光斑还细时会估低, 去模糊因而偏保守。"""
    g = cv2.GaussianBlur(norm, (0, 0), 0.6).astype(np.float32)
    G = np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)) / 8
    paper = float(np.median(g[B == 0]))
    r = max(2, int(round(1.5 * sw)))
    core = cv2.erode(g, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
    C = paper - core
    if not (B > 0).any():
        return None
    sel = ((np.abs(g - (core + paper) / 2) < 0.15 * C) & (G >= cv2.dilate(G, np.ones((3, 3), np.uint8)) - 1e-3)
           & (C > 0.6 * np.percentile(C[B > 0], 90)) & (G > 1))
    if sel.sum() < 50:
        return None
    ratio = float(np.percentile(G[sel] / C[sel], 70))
    grid = np.linspace(0.3, 4.0, 400)
    f = np.array([math.erf(1 / (x * math.sqrt(2))) / 2 for x in grid])
    s = float(np.interp(ratio, f[::-1], grid[::-1]))
    return float(np.sqrt(max(0.0, s * s - 0.6 ** 2)))  # 扣掉估计前去噪用的 0.6


def deblur(gray: np.ndarray, sigma: float, iters: int = 10) -> np.ndarray:
    """Richardson-Lucy 去模糊(高斯 PSF), 在"墨迹量"域(255-灰度)上迭代。
    把镜头/对焦模糊反推回去: 低分辨率照片里比光斑还细的笔画不再被"摊宽", 淡而细的笔画对比度回升, 断笔减少。"""
    d = np.maximum(255.0 - gray.astype(np.float32), 1e-3)
    est = d.copy()
    for _ in range(iters):
        est *= cv2.GaussianBlur(d / np.maximum(cv2.GaussianBlur(est, (0, 0), sigma), 1e-3), (0, 0), sigma)
    return np.clip(255.0 - est, 0, 255).astype(np.uint8)


def upscale2(img: np.ndarray, method: str) -> tuple[np.ndarray, str]:
    """放大 2 倍。waifu2x(upconv_7_photo, 去噪 1)不可用时退回双三次。返回 (图, 实际方法)。"""
    if method == "waifu2x" and WAIFU2X:
        with tempfile.TemporaryDirectory() as td:
            src, dst = Path(td) / "in.png", Path(td) / "out.png"
            cv2.imwrite(str(src), img)
            r = subprocess.run([str(WAIFU2X), "-i", str(src), "-o", str(dst), "-n", "1", "-s", "2",
                                "-m", str(WAIFU2X.parent / "models-upconv_7_photo")], capture_output=True)
            up = cv2.imread(str(dst), cv2.IMREAD_COLOR) if r.returncode == 0 else None
        if up is not None:
            return up, "waifu2x"
        print("waifu2x 运行失败, 改用双三次放大", file=sys.stderr)
    return cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), "bicubic"


def hysteresis(norm: np.ndarray, B: np.ndarray, thr: float, k: float) -> np.ndarray:
    """滞后阈值: 在更宽松的弱阈值下与强墨迹相连的淡笔画也算墨迹(修补笔压轻处的断笔), 孤立的淡斑不收。"""
    if k <= 0:
        return B
    paper = float(np.median(norm))
    weak = binarize(norm, thr + k * (paper - thr))[0]
    n, lab, _, _ = cv2.connectedComponentsWithStats(weak, connectivity=8)
    good = np.zeros(n, bool)
    good[np.unique(lab[B > 0])] = True
    good[0] = False
    return (good[lab] * 255).astype(np.uint8)


def binarize(norm: np.ndarray, thr: float | None = None) -> tuple[np.ndarray, float]:
    blur = cv2.GaussianBlur(norm, (3, 3), 0)
    if thr is None:
        t, _ = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        thr = float(np.clip(t, 110, 225))
    return ((blur < thr) * 255).astype(np.uint8), thr


def estimate_skew(B: np.ndarray) -> float:
    """先用长直线(横线纸的横线)估计倾角; 没有长直线时用水平投影方差最大化(按文字行)。"""
    h, w = B.shape
    f = max(1.0, w / 1500)
    small = cv2.resize(B, (int(w / f), int(h / f)), interpolation=cv2.INTER_AREA)
    small = ((small > 64) * 255).astype(np.uint8)
    sh, sw_ = small.shape
    segs = cv2.HoughLinesP(small, 1, np.pi / 720, threshold=max(20, int(sw_ * 0.15)),
                           minLineLength=int(sw_ * 0.3), maxLineGap=int(sw_ * 0.02) + 2)
    angs, wts = [], []
    if segs is not None:
        for x1, y1, x2, y2 in segs.reshape(-1, 4):  # OpenCV 4 为 (N,1,4), 5 为 (N,4)
            a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
            if abs(a) < 15:
                angs.append(a)
                wts.append(np.hypot(x2 - x1, y2 - y1))
    def score(a):
        M = cv2.getRotationMatrix2D((sw_ / 2, sh / 2), a, 1)
        r = cv2.warpAffine(small, M, (sw_, sh), flags=cv2.INTER_NEAREST)
        return float(np.var(r.sum(1, dtype=np.float64)))

    if len(angs) >= 2:
        order = np.argsort(angs)
        cw = np.cumsum(np.asarray(wts)[order])
        coarse = float(np.asarray(angs)[order][np.searchsorted(cw, cw[-1] / 2)])
    else:
        coarse = max(np.arange(-6, 6.01, 0.5), key=score)
    return float(max(np.arange(coarse - 0.5, coarse + 0.51, 0.05), key=score))


# ---------------------------------------------------------------- 去横线 / 竖线

def vrun(Bb: np.ndarray) -> np.ndarray:
    """每个墨迹像素所在竖向连续黑段的长度(印刷线只有两三像素厚, 笔画通常更粗)。"""
    b = (Bb > 0).astype(np.int32)
    up, dn = np.zeros_like(b), np.zeros_like(b)
    up[0], dn[-1] = b[0], b[-1]
    for i in range(1, len(b)):
        up[i] = (up[i - 1] + 1) * b[i]
    for i in range(len(b) - 2, -1, -1):
        dn[i] = (dn[i + 1] + 1) * b[i]
    return np.where(b > 0, up + dn - 1, 0)


def find_rules(B: np.ndarray, min_cover: float, L: int | None = None):
    """检测贯穿版面的水平印刷线。返回 (掩膜, 线带列表[(y0,y1)], 线宽) 或 None。
    竖线检测时由调用方传入转置后的图。"""
    h, w = B.shape
    L = L or max(25, w // 20)
    # 纸面弯曲/透视会让横线变成 1 像素的台阶: 先竖向膨胀把台阶连成连续长条, 开运算后再取回原像素
    Bd = cv2.dilate(B, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 3)))
    M = cv2.morphologyEx(Bd, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (L, 1)))
    M = cv2.bitwise_and(M, B)
    # 统计覆盖率时上下各容忍 2 像素, 照片透视/纸面弯曲造成的轻微倾斜也能整条认出
    cover = (cv2.dilate(M, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5))) > 0).sum(1)
    bands = runs(cover >= min_cover * w)
    if not bands:
        return None
    merged = [list(bands[0])]
    for a, b in bands[1:]:  # 一根粗线可能被拆成相邻两段
        if a - merged[-1][1] <= 3:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    bands = [tuple(x) for x in merged]
    med = float(np.median([b - a for a, b in bands]))
    bands = [(a, b) for a, b in bands if b - a <= max(12, 3 * med)]  # 粗暗带是阴影/纸边, 不是印刷线
    if not bands:
        return None
    rowmask = np.zeros(h, bool)
    for a, b in bands:
        rowmask[max(0, a - 1):b + 1] = True
    mask = M.copy()
    mask[~rowmask] = 0
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5)))  # 盖住线条的抗锯齿边缘
    thick = max(1, int(np.median([b - a for a, b in bands])))
    return mask, bands, thick


def refine_rules(rules, B: np.ndarray, norm: np.ndarray, run: np.ndarray, sw: float):
    """区分印刷线与手画的长笔画(横线/波浪线/下划线), 后者不当横线删。判据:
    - 不在等距网格上: 印刷横线等距, 手画的线一般画在两条横线之间, 上下都找不到相距约一个线距的邻线
    - 明显更黑(线芯灰度) 或 明显更粗(竖向黑段长度)
    - 不直: 只看细像素(排除穿线的笔画)拟合直线的残差
    返回 (掩膜, 线带, 线宽, 各线的光度分界, 印刷线厚度) 或 None。
    光度分界: 线上比它更黑的像素视为压线笔迹; 无法区分(比如扫描件把线和字都压成纯黑)时为 None。"""
    mask, bands, thick = rules
    mask = cv2.bitwise_and(mask, B)
    def mode(v):  # 直方图众数: 字压在线上时, 线上大半像素可能属于笔画, 中位数会被带偏
        hist = np.convolve(np.bincount(v.astype(np.int64) // 4, minlength=64), [1, 2, 1], mode="same")
        return (int(np.argmax(hist)) + 0.5) * 4

    mins, runs_ = [], []
    for a0, b0 in bands:  # 每条线逐列取最暗值(扫描件会把印刷线增强得很深, 不能用平均灰度)
        col = np.where(mask[a0:b0 + 1] > 0, norm[a0:b0 + 1], 255).min(0)
        mins.append(col[col < 255])
        rr = run[a0:b0 + 1][mask[a0:b0 + 1] > 0]
        runs_.append(float(np.percentile(rr, 25)) if rr.size else 0.0)  # 线本身最薄, 取低分位
    cores = [mode(m) if m.size else 255.0 for m in mins]
    ref = float(np.median(cores))
    t = float(np.median(runs_))  # 印刷线在二值图里的厚度
    fat = t + max(3.0, 0.5 * sw)

    centers = [(a0 + b0) / 2 for a0, b0 in bands]
    off = [False] * len(bands)
    if len(bands) >= 4:
        d = float(np.median(np.diff(sorted(centers))))
        for i, c in enumerate(centers):
            off[i] = min(abs(abs(c - o) - d) for j, o in enumerate(centers) if j != i) > 0.15 * d

    relative = len(bands) >= 4  # 线太少时"和其他线比"的判据不可靠, 只保留绝对判据(不直)
    kept, kept_mins = [], []
    for (a0, b0), core, m, rt, of in zip(bands, cores, mins, runs_, off):
        thin = (mask[a0:b0 + 1] > 0) & (run[a0:b0 + 1] < fat)
        cnt = thin.sum(0)
        xs = np.flatnonzero(cnt)
        wavy = False
        if xs.size > 20:
            ys = (thin * np.arange(thin.shape[0])[:, None]).sum(0)[xs] / cnt[xs]
            res = ys - np.polyval(np.polyfit(xs, ys, 1), xs)
            wavy = 1.4826 * float(np.median(np.abs(res - np.median(res)))) > max(2.0, 0.6 * t)  # 随分辨率(线宽)缩放
        if wavy or (relative and (of or core < ref - 40 or rt > fat)):
            mask[max(0, a0 - 3):b0 + 4] = 0
        else:
            kept.append((a0, b0))
            kept_mins.append(m)
    if not kept:
        return None
    keep = cv2.bitwise_and(B, cv2.bitwise_not(mask))
    i_ink = float(np.percentile(norm[keep > 0], 25)) if (keep > 0).any() else 0.0
    cuts = []
    for m in kept_mins:
        # 逐条线估计印刷线灰度: 字压在线上时很多列的最暗值是墨迹, 所以线芯取直方图众数,
        # 离散度只用众数以上(不受墨迹污染)的一侧
        if not m.size:
            cuts.append(None)
            continue
        core = mode(m)
        up = m[m >= core] - core
        sigma = float(np.median(up)) / 0.674 if up.size else 5.0
        floor = core - max(10.0, 3 * sigma)  # 印刷线自身最暗大致到这里
        cuts.append(floor if floor - i_ink > 25 else None)
    thick = max(1, int(np.median([b - a for a, b in kept])))
    return mask, kept, thick, cuts, t


def strip_rules(B: np.ndarray, norm: np.ndarray, rules, run: np.ndarray, sw: float) -> np.ndarray:
    """删掉横线像素, 再把被横线截断或压在线上的笔画补回来:
    1) 几何: 横线上下都有墨迹的位置(竖向闭运算可桥接) -> 穿过横线的笔画
    2) 粗细: 竖向黑段明显比印刷线厚 -> 压在线上的笔画(扫描件线和字都是纯黑时只能靠这条)
    3) 光度: 比印刷线最暗处还黑的像素 -> 压在线上的笔画"""
    mask, bands, thick, cuts, t = rules
    fat = t + max(3.0, 0.5 * sw)
    keep = cv2.bitwise_and(B, cv2.bitwise_not(mask))
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (1, thick + 4))
    restore = cv2.bitwise_and(cv2.morphologyEx(keep, cv2.MORPH_CLOSE, k), mask)
    restore[(mask > 0) & (run >= fat)] = 255
    dark = np.zeros_like(B)
    for (a0, b0), cut in zip(bands, cuts):
        if cut is not None:
            r0, r1 = max(0, a0 - 3), b0 + 4
            dark[r0:r1] = ((norm[r0:r1] < cut) & (mask[r0:r1] > 0)) * 255
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    restore = cv2.bitwise_or(restore, dark)
    out = cv2.bitwise_or(keep, restore)

    # 残渣: 落在横线带上的扁平碎片(断线头、虚线段、线上的印刷圆点): 和印刷线一样薄, 且不比它更黑
    orun = vrun(out)
    n, lab, st, _ = cv2.connectedComponentsWithStats(out, connectivity=8)
    drop = np.zeros(n, bool)
    for i in range(1, n):
        x, y, cw, ch, a = st[i]
        near = [j for j, (b0, b1) in enumerate(bands) if abs(y + ch / 2 - (b0 + b1) / 2) <= thick + 2]
        if ch > 2 * thick + 4 or not near:
            continue
        sel = lab[y:y + ch, x:x + cw] == i
        if float(np.median(orun[y:y + ch, x:x + cw][sel])) >= fat:
            continue  # 和笔画一样粗: 是压线的笔画(如“一”), 不是残渣
        cut = cuts[near[0]]
        drop[i] = cut is None or float(norm[y:y + ch, x:x + cw][sel].mean()) > cut
    out[drop[lab]] = 0
    return out


def strip_all(B: np.ndarray, norm: np.ndarray, Bs: np.ndarray, min_cover: float, sw: float):
    """分竖条检测并去除横线: 纸面弯曲/透视时各处横线倾角不同, 每条约 700 像素宽, 条内近似水平。
    返回 (去线后的 B, 去掉的线像素)。竖线由调用方转置后调用。"""
    h, w = B.shape
    L = max(25, w // 20)  # 核长仍按整页宽度取, 免得把长横笔画当成线
    run = vrun(B)
    out, px = B.copy(), np.zeros_like(B)
    xs = np.linspace(0, w, max(1, int(round(w / 700))) + 1).astype(int)
    for x0, x1 in zip(xs, xs[1:]):
        c = lambda arr: np.ascontiguousarray(arr[:, x0:x1])
        r = find_rules(c(Bs), min_cover, L)
        r = refine_rules(r, c(B), c(norm), c(run), sw) if r else None
        if r:
            out[:, x0:x1] = strip_rules(c(B), c(norm), r, c(run), sw)
            px[:, x0:x1] = r[0]
    return out, px


def drop_blobs(B: np.ndarray, min_area: float) -> np.ndarray:
    """去掉笔画粗细远超正常笔迹的大块(线圈、夹子、阴影、纸边): 最大内切圆直径 > 2.5 倍典型笔画粗细。
    典型粗细只按面积 >= min_area 的连通域估计, 免得被成片的细小噪点拉低。"""
    dt = cv2.distanceTransform(B, cv2.DIST_L2, 3)
    n, lab, st, _ = cv2.connectedComponentsWithStats(B, connectivity=8)
    thick = np.zeros(n)
    for i in range(1, n):
        x, y, cw, ch, a = st[i]
        if a >= min_area:
            thick[i] = 2 * dt[y:y + ch, x:x + cw][lab[y:y + ch, x:x + cw] == i].max()
    sized = thick[1:][st[1:, 4] >= min_area]
    if sized.size < 3:
        return B
    ref = float(np.median(sized))
    bad = (thick > 2.5 * ref) & (st[:, 4] > 30 * ref * ref)
    bad[0] = False
    return np.where(bad[lab], 0, B).astype(np.uint8)


def ink_weight(norm: np.ndarray, B: np.ndarray, sw: float, suppress: np.ndarray, edge: float) -> np.ndarray:
    """每个像素的墨迹浓度(0~1), 矢量轮廓取在 0.5 处: 轮廓放在"附近一个半笔画宽内最深处"与纸色之间的 edge 位置
    (默认正中, 即局部半高, Bernsen 式), 而不是二值化阈值处。
    - 二值化阈值为了不漏淡笔画定得偏宽, 会把灰晕、锐化黑边、JPEG 噪点算进笔画, 字就变粗变脏;
    - 逐像素取局部最深处: 笔压深浅不一的笔画, 深处和浅处都按各自的深浅定边界, 浅处不会断, 深处不会肥;
      也不会被单个噪点像素带偏(噪点大的照片若取全笔画最暗像素, 半高线压得太深, 细笔画会碎)。"""
    g = cv2.GaussianBlur(norm, (0, 0), 0.8).astype(np.float32)  # 轻度去噪, 边缘位置不变
    paper = float(np.median(g[B == 0]))
    r = max(2, int(round(1.5 * sw)))
    core = cv2.erode(g, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
    span = np.maximum(paper - core, 1.0)
    w = np.clip(0.5 + (core + edge * span - g) / span, 0, 1)
    w[core >= paper - 10] = 0
    w[(suppress > 0) & (B == 0)] = 0  # 已去掉的横线不能借灰度"长回来"
    return w.astype(np.float32)


def soft_mask(M: np.ndarray, Bc: np.ndarray, Wc: np.ndarray) -> np.ndarray:
    """元素掩膜 -> 带灰度边缘的浮点图: 掩膜及外圈 1 像素内取墨迹浓度, 排除相邻字/行的墨迹。"""
    ring = cv2.dilate(M, np.ones((3, 3), np.uint8)) > 0
    return np.where(ring & ~((Bc > 0) & (M == 0)), Wc, 0.0).astype(np.float32)


def stroke_width(mask: np.ndarray) -> float:
    cnts, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    per = sum(cv2.arcLength(c, True) for c in cnts)
    return max(1.0, 2.0 * cv2.countNonZero(mask) / per) if per > 0 else 2.0


def clean_components(B: np.ndarray, min_area: float, near: int, drop_border: bool) -> np.ndarray:
    """去噪: 小于 min_area 的斑点只有孤立时才删; 挨着大笔画(near 像素内)的多是点画(文、字、讠的点), 保留。"""
    n, lab, st, _ = cv2.connectedComponentsWithStats(B, connectivity=8)
    h, w = B.shape
    keep = np.zeros(n, bool)
    small = np.zeros(n, bool)
    for i in range(1, n):
        x, y, cw, ch, a = st[i]
        if a < 4:
            continue
        touches = x == 0 or y == 0 or x + cw >= w or y + ch >= h
        edge = (cw > 0.4 * w and ch < 0.02 * h) or (ch > 0.4 * h and cw < 0.02 * w)
        if drop_border and touches and (edge or (cw > 0.4 * w and ch > 0.4 * h)):  # 纸边/桌沿/大片阴影
            continue
        keep[i] = True
        small[i] = a < min_area
    if small.any():
        big = ((keep & ~small)[lab] * 255).astype(np.uint8)
        zone = cv2.dilate(big, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * near + 1, 2 * near + 1)))
        hit = np.zeros(n, bool)
        hit[np.unique(lab[(zone > 0) & (lab > 0)])] = True
        keep &= ~small | hit
    return (keep[lab] * 255).astype(np.uint8)


# ---------------------------------------------------------------- 分行

def assign_to_bands(comps: list[Comp], bands) -> list[list[Comp]]:
    lines = [[] for _ in bands]
    for c in comps:
        best = min(range(len(bands)), key=lambda i: 0 if bands[i][0] <= c.cy < bands[i][1]
                   else min(abs(c.cy - bands[i][0]), abs(c.cy - bands[i][1])))
        lines[best].append(c)
    return [ln for ln in lines if ln]


def profile_bands(mask: np.ndarray, comps: list[Comp], sw: float):
    big = [c.h for c in comps if c.h > 3 * sw]
    ch = float(np.median(big)) if big else 20.0
    prof = (mask > 0).sum(1).astype(np.float32)
    win = max(3, int(ch / 4) | 1)
    ps = np.convolve(prof, np.ones(win) / win, mode="same")
    bands = runs(ps > 0.5)  # 用绝对阈值: 横贯整页的长笔画不该把稀疏的行(勾号、短句)压没; 粘连的行下面在谷底切开
    if not bands:
        return []
    hs = [b - a for a, b in bands if b - a >= ch]
    ref = float(np.median(hs)) if hs else ch * 1.5

    out, stack = [], list(bands)
    while stack:  # 过高的行带在最深谷底切开(行距紧时相邻两行会粘连)
        a, b = stack.pop(0)
        if b - a > 1.7 * ref:
            lo, hi = a + (b - a) // 4, b - (b - a) // 4
            j = lo + int(np.argmin(ps[lo:hi]))
            if ps[j] < 0.5 * ps[a:b].mean():
                stack[:0] = [(a, j), (j, b)]
                continue
        out.append((a, b))

    changed = True
    while changed and len(out) > 1:  # 过矮的行带(标点、i 的点)并入最近的行
        changed = False
        for i, (a, b) in enumerate(out):
            if b - a < 0.35 * ref:
                gl = a - out[i - 1][1] if i > 0 else 1e9
                gr = out[i + 1][0] - b if i + 1 < len(out) else 1e9
                if min(gl, gr) < 0.6 * ref:
                    if gl <= gr:
                        out[i - 1] = (out[i - 1][0], b)
                    else:
                        out[i + 1] = (a, out[i + 1][1])
                    del out[i]
                    changed = True
                    break
    return out


def settle_lines(lines: list[list[Comp]]) -> list[list[Comp]]:
    """修正按行带硬分配的错误: 垂到下一格的标点/笔画、只有零星碎片的“伪行”。
    每行取主体笔画的上下沿作为行芯, 每个连通域归到行芯离它重心最近的那一行。"""
    if len(lines) < 2:
        return lines
    areas = [sum(c.area for c in L) for L in lines]
    med = float(np.median(areas))
    cores, comps, loose = [], [], set()
    for L, ar in zip(lines, areas):
        comps += L
        if ar < 0.2 * med:  # 伪行: 打散后重新分配
            loose.update(c.idx for c in L)
            continue
        hmax = max(c.h for c in L)
        big = [c for c in L if c.h >= 0.5 * hmax]
        cores.append((float(np.median([c.y for c in big])), float(np.median([c.y1 for c in big])),
                      min(c.x for c in L), max(c.x1 for c in L)))
    if not cores:
        return lines

    def dist(c, core):
        top, bot, x0, x1 = core
        dy = 0.0 if top <= c.cy <= bot else min(abs(c.cy - top), abs(c.cy - bot))
        dx = max(0.0, x0 - c.cx, c.cx - x1)
        return dy + 0.25 * dx

    out = [[] for _ in cores]
    far = []
    for c in comps:
        i = min(range(len(cores)), key=lambda i: dist(c, cores[i]))
        if c.idx in loose and dist(c, cores[i]) > 0.6 * (cores[i][1] - cores[i][0]):
            far.append(c)  # 远离所有文字行的零星笔迹(勾号、批注): 自成一行, 不去干扰正文的字号估计
        else:
            out[i].append(c)
    out = [L for L in out if L]
    for c in sorted(far, key=lambda c: c.cy):
        hit = next((L for L in out if L[0].idx in loose and
                    min(c.y1, max(x.y1 for x in L)) - max(c.y, min(x.y for x in L)) > 0), None)
        (hit.append(c) if hit else out.append([c]))

    # 字骑在横线上写时, 按横线分格会把同一行字的上下部件拆成两"行": 竖直范围重叠过半的合并
    changed = True
    while changed:
        changed = False
        spans = [(min(c.y for c in L), max(c.y1 for c in L)) for L in out]
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                ov = min(spans[i][1], spans[j][1]) - max(spans[i][0], spans[j][0])
                if ov > 0.5 * min(spans[i][1] - spans[i][0], spans[j][1] - spans[j][0]):
                    out[i] += out.pop(j)
                    changed = True
                    break
            if changed:
                break
    return out


def rule_bands(rules, H: int):
    centers = sorted((a + b) / 2 for a, b in rules[1])
    if len(centers) >= 3:  # 横线等距: 漏检的淡线按间距补上, 首尾外推
        d = float(np.median(np.diff(centers)))
        filled = [centers[0]]
        for c in centers[1:]:
            k = int(round((c - filled[-1]) / d))
            if k >= 2:
                filled += [filled[-1] + (c - filled[-1]) * t / k for t in range(1, k)]
            filled.append(c)
        while filled[0] - d > 0:
            filled.insert(0, filled[0] - d)
        while filled[-1] + d < H:
            filled.append(filled[-1] + d)
        centers = filled
    edges = [0.0] + centers + [float(H)]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


# ---------------------------------------------------------------- 分字

def refine_cut(proj: np.ndarray, x: float, win: float) -> float:
    """在 x±win 范围内找墨迹最少的列。proj 为行内逐列墨迹像素数。"""
    lo, hi = max(0, int(x - win)), min(len(proj), int(x + win) + 1)
    if hi <= lo:
        return float(x)
    seg = proj[lo:hi]
    idx = np.flatnonzero(seg == seg.min())
    return float(lo + idx[np.argmin(np.abs(idx + lo - x))])


def column_groups(comps: list[Comp]) -> list[list]:
    """水平方向明显重叠的连通域并成一列(上下结构、点画等)。元素: [x0,x1,y0,y1]。"""
    groups = []
    for c in sorted(comps, key=lambda c: c.x):
        if groups:
            g = groups[-1]
            ov = min(g[1], c.x1) - max(g[0], c.x)
            if ov > 0.15 * min(g[1] - g[0], c.w) or c.x1 <= g[1]:
                g[0], g[1] = min(g[0], c.x), max(g[1], c.x1)
                g[2], g[3] = min(g[2], c.y), max(g[3], c.y1)
                continue
        groups.append([c.x, c.x1, c.y, c.y1])
    return groups


def word_gap(groups: list[list], sw: float) -> float:
    """词间距阈值: 间隙呈双峰(字母间/单词间)时取 Otsu 分界, 否则按 0.3 倍 x 高度。"""
    xh = float(np.median([g[3] - g[2] for g in groups]))
    gaps = sorted(max(0, b[0] - a[1]) for a, b in zip(groups, groups[1:]))
    thr = 0.3 * xh
    if len(gaps) >= 3:
        best, k_best = -1.0, 1
        for k in range(1, len(gaps)):
            lo, hi = np.mean(gaps[:k]), np.mean(gaps[k:])
            score = k * (len(gaps) - k) * (hi - lo) ** 2
            if score > best:
                best, k_best = score, k
        lo, hi = np.mean(gaps[:k_best]), np.mean(gaps[k_best:])
        if hi > 2 * lo + sw:
            thr = (gaps[k_best - 1] + gaps[k_best]) / 2
    return max(thr, 2 * sw)


def cc_cuts(comps: list[Comp], proj: np.ndarray, x0: int, script: str, unit: str, sw: float):
    """无 OCR 时的切字: 返回 (切分线 x 列表(页面坐标), 字号估计 S, 字块列表[x0,x1,y0,y1])。"""
    groups = column_groups(comps)
    line_h = max(c.y1 for c in comps) - min(c.y for c in comps)
    tall = [g for g in groups if g[3] - g[2] >= 0.5 * line_h] or groups
    S = float(np.median([g[3] - g[2] for g in tall]))
    core_top = float(np.median([g[2] for g in tall]))
    core_bot = float(np.median([g[3] for g in tall]))

    if script == "latin" and unit == "char":  # 拉丁文逐字母: 每个独立的字母块(连笔则整段)各成一个
        pass
    elif script == "latin":  # 拉丁文按单词切: 间隙大于阈值才断开
        thr = word_gap(groups, sw)
        merged = [groups[0]]
        for g in groups[1:]:
            if g[0] - merged[-1][1] < thr:
                m = merged[-1]
                m[1], m[2], m[3] = max(m[1], g[1]), min(m[2], g[2]), max(m[3], g[3])
            else:
                merged.append(g)
        groups = merged
    else:  # 汉字近似方块: 动态规划把偏旁部首等窄块组合成宽度最接近 S 的字(全行最优, 不会把相邻两字的偏旁拼错)
        def punct(g):  # 贴着行底的小块视为标点(，。、), 单独成字; 居中的小点(小、不、辶)照常参与组合
            low = (g[2] + g[3]) / 2 > core_top + 0.6 * (core_bot - core_top)
            return g[1] - g[0] < 0.35 * S and g[3] - g[2] < 0.35 * S and low

        n = len(groups)
        best, back = [0.0] + [np.inf] * n, [0] * (n + 1)
        for j in range(1, n + 1):
            x1 = groups[j - 1][1]
            for i in range(j - 1, -1, -1):
                seg = groups[i:j]
                x1 = max(x1, groups[i][1])
                width = x1 - groups[i][0]
                if j - i > 1 and (width > 1.35 * S or any(punct(g) for g in seg)):
                    break
                if j - i == 1 and punct(seg[0]):
                    cost = 0.0
                else:
                    k = max(1, round(width / S))  # 单块过宽 = 粘连字, 之后会再切开
                    cost = k * ((width / k - S) / S) ** 2
                    cost += 0.5 * sum(max(0, b[0] - a[1]) for a, b in zip(seg, seg[1:])) / S
                if best[i] + cost < best[j]:
                    best[j], back[j] = best[i] + cost, i
        segs, j = [], n
        while j > 0:
            i = back[j]
            seg = groups[i:j]
            segs.append([seg[0][0], max(g[1] for g in seg), min(g[2] for g in seg), max(g[3] for g in seg)])
            j = i
        groups = segs[::-1]

    cuts = []
    for i, g in enumerate(groups):
        if i:
            cuts.append((groups[i - 1][1] + g[0]) / 2)
        width = g[1] - g[0]
        if script != "latin" and width > 1.45 * S:  # 粘连字: 按字宽等分后找最细处切
            n = int(round(width / S))
            for k in range(1, n):
                guess = g[0] + k * width / n - x0
                cuts.append(refine_cut(proj, guess, 0.25 * S) + x0)
    return sorted(cuts), S, groups


def ocr_cuts(comps: list[Comp], chs: list, proj: np.ndarray, x0: int):
    """按 OCR 字框指导切字: 把笔画列(column_groups)按顺序对齐分配给各字框(动态规划, 比较中心与宽度),
    切点只落在真实空隙里; 只有一个笔画列里落着两个以上字心(连笔粘连)时才在投影最细处切开。
    返回 (切分线, 每段对应的 (文字, 置信度))。"""
    S = float(np.median([c[3] - c[2] for c in chs])) or 1.0
    centers = [(c[2] + c[3]) / 2 for c in chs]
    groups = []
    for g in column_groups(comps):
        inside = [k for k, cx in enumerate(centers) if g[0] <= cx <= g[1]]
        if len(inside) >= 2:
            xs = [float(g[0])]
            for k0, k1 in zip(inside, inside[1:]):
                p, q = chs[k0], chs[k1]
                b = (p[3] + q[2]) / 2 if p[3] <= q[2] else (centers[k0] + centers[k1]) / 2
                xs.append(refine_cut(proj, b - x0, 0.2 * S) + x0)
            xs.append(float(g[1]))
            groups += [[a, b] for a, b in zip(xs, xs[1:])]
        else:
            groups.append([float(g[0]), float(g[1])])
    n, N = len(groups), len(chs)
    D = np.full((n + 1, N + 1), np.inf)
    D[0, 0] = 0.0
    back = {}
    for k in range(1, N + 1):
        cx, wk = centers[k - 1], chs[k - 1][3] - chs[k - 1][2]
        for j in range(n + 1):
            best, arg = D[j, k - 1] + 1.0, None  # 该字框没分到笔画(OCR 多认了字)
            right = -np.inf
            for i in range(j - 1, -1, -1):
                right = max(right, groups[i][1])
                width = right - groups[i][0]
                if j - i > 1 and width > 2.5 * S:
                    break
                if np.isfinite(D[i, k - 1]):
                    cost = D[i, k - 1] + (((groups[i][0] + right) / 2 - cx) ** 2 + 0.5 * (width - wk) ** 2) / S ** 2
                    if cost < best:
                        best, arg = cost, i
            D[j, k], back[j, k] = best, arg
    segs, j, k = [], n, N
    while k > 0:
        i = back[j, k]
        if i is not None:
            segs.append((groups[i][0], max(g[1] for g in groups[i:j]), (chs[k - 1][0], chs[k - 1][1])))
            j = i
        k -= 1
    segs.reverse()
    cuts = [(a[1] + b[0]) / 2 for a, b in zip(segs, segs[1:])]
    return cuts, [sg[2] for sg in segs]


# ---------------------------------------------------------------- OCR (可选)

_OCR = None


def ocr_raw(clean_bgr: np.ndarray, single_char: bool):
    """RapidOCR(PP-OCR) 原始结果; 不可用时返回 None。single_char=True 时英文也给逐字母框。"""
    global _OCR
    try:
        if _OCR is None:
            from rapidocr import RapidOCR
            _OCR = RapidOCR(params={"Global.log_level": "error", "Global.text_score": 0.1})
        res = _OCR(clean_bgr, return_word_box=True, return_single_char_box=single_char)
    except Exception as e:  # 模型下载失败等: 退回纯几何流程
        print(f"OCR 不可用, 跳过: {e}", file=sys.stderr)
        return None
    return res if res.boxes is not None and len(res.boxes) else None


def text_roi(res, W: int, H: int, min_score: float):
    """书写区域 = 可信文字行框的外接矩形, 左右留 1 倍、上下留 0.6 倍行高。线圈、桌面、鼠标等杂物在框外。
    可信 = 置信度 >= min_score 且至少 2 个字(线圈/纸边常被高置信度地读成 "N" "in" "x" 这类一两个字符);
    这样的行不足 2 条时(字很潦草)放宽到 0.5。贴着图像边缘或明显矮于正文的框不采信。"""
    m = 0.015 * min(W, H)
    items = []
    for b, txt, sc in zip(res.boxes, res.txts, res.scores):
        b = np.asarray(b, np.float32)
        if b[:, 0].min() > m and b[:, 1].min() > m and b[:, 0].max() < W - m and b[:, 1].max() < H - m:
            items.append((b, float(sc), len(re.sub(r"\s", "", txt))))
    for floor in (min_score, 0.5):
        boxes = [b for b, sc, n in items if sc >= floor and n >= 2]
        if len(boxes) >= 2:
            break
    if not boxes:
        return None
    hs = [float(np.ptp(b[:, 1])) for b in boxes]
    mh = float(np.median(hs))
    boxes = [b for b, bh in zip(boxes, hs) if 0.4 * mh <= bh <= 3 * mh]
    pts = np.vstack(boxes)
    return (max(0, int(pts[:, 0].min() - mh)), max(0, int(pts[:, 1].min() - 0.6 * mh)),
            min(W, int(pts[:, 0].max() + mh)), min(H, int(pts[:, 1].max() + 0.6 * mh)))


def ocr_lines(res, min_score: float):
    """返回 OCR 行列表: {quad, y0, y1, text, chars:[(text, score, x0, x1)]}。"""
    lines = []
    for quad, txt, sc in zip(res.boxes, res.txts, res.scores):
        if sc < min_score:  # 低置信度多为线圈、纸边等误检
            continue
        q = np.asarray(quad, np.float32)
        lines.append({"quad": q, "x0": float(q[:, 0].min()), "x1": float(q[:, 0].max()),
                      "y0": float(q[:, 1].min()), "y1": float(q[:, 1].max()),
                      "parts": [(float(q[:, 0].min()), txt)], "chars": []})
    if not lines:
        return []
    words = [w for line in (res.word_results or ()) for w in (line or ()) if w and w[2] is not None]
    for txt, score, box in words:
        b = np.asarray(box, np.float32).reshape(-1, 2)
        cx, cy = float(b[:, 0].mean()), float(b[:, 1].mean())
        dist = [-cv2.pointPolygonTest(L["quad"], (cx, cy), True) for L in lines]
        j = int(np.argmin(dist))
        if dist[j] > 0.5 * (lines[j]["y1"] - lines[j]["y0"]):  # 属于被滤掉的误检行
            continue
        lines[j]["chars"].append((txt, float(score), float(b[:, 0].min()), float(b[:, 0].max())))

    lines.sort(key=lambda L: L["y0"])
    merged = []
    for L in lines:  # 同一行被 OCR 拆成几段时合并
        if merged:
            M = merged[-1]
            ov = min(M["y1"], L["y1"]) - max(M["y0"], L["y0"])
            if ov > 0.5 * min(M["y1"] - M["y0"], L["y1"] - L["y0"]):
                M["quad"] = cv2.convexHull(np.vstack([M["quad"], L["quad"]])).reshape(-1, 2)
                M["x0"], M["x1"] = min(M["x0"], L["x0"]), max(M["x1"], L["x1"])
                M["y0"], M["y1"] = min(M["y0"], L["y0"]), max(M["y1"], L["y1"])
                M["chars"] += L["chars"]
                M["parts"] += L["parts"]
                continue
        merged.append(L)
    for L in merged:
        L["chars"].sort(key=lambda c: (c[2] + c[3]) / 2)
        L["text"] = " ".join(t for _, t in sorted(L["parts"]))
    return merged


# ---------------------------------------------------------------- 矢量化

def trace(soft: np.ndarray, scale: int, smooth: float, alphamax: float, turd: int, opttol: float) -> str:
    """墨迹浓度图(0~1) -> potrace 路径片段, 坐标已换算回输入像素坐标。
    先三次插值放大并轻微高斯平滑, 再在 0.5 处取等值线, 得到亚像素级的平滑边缘。"""
    if not (soft >= 0.5).any():
        return ""
    pad = 2
    m = cv2.copyMakeBorder(soft, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    if scale > 1:
        m = cv2.resize(m, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    if smooth > 0:
        m = cv2.GaussianBlur(m, (0, 0), smooth * scale)
    bmp = np.where(m >= 0.5, 0, 255).astype(np.uint8)  # potrace: 黑色为前景
    h, w = bmp.shape
    pgm = b"P5\n%d %d\n255\n" % (w, h) + bmp.tobytes()
    r = subprocess.run([POTRACE, "-", "-b", "svg", "-o", "-", "--flat", "-t", str(turd),
                        "-a", str(alphamax), "-O", str(opttol)],
                       input=pgm, capture_output=True, check=True)
    hit = re.search(rb'<g transform="([^"]+)"[^>]*>(.*?)</g>', r.stdout, re.S)
    if not hit:
        return ""
    tf, paths = hit.group(1).decode(), hit.group(2).decode().strip()
    return f'<g transform="translate({-pad},{-pad}) scale({1 / scale:g}) {tf}">{paths}</g>'


def svg_doc(w: int, h: int, body: str, color: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}">\n<g fill="{color}" stroke="none">\n{body}\n</g>\n</svg>\n')


def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s\x00-\x1f]', "", s)[:16]


# ---------------------------------------------------------------- 主流程

def process(name: str, img: np.ndarray, src: Path, out: Path, a) -> dict:
    if max(img.shape[:2]) > a.max_side:
        f = a.max_side / max(img.shape[:2])
        img = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur_sigma = None
    if a.deblur != "off":
        if a.deblur == "auto":
            n0 = flatten_background(gray)
            b0, _ = binarize(n0, a.threshold)
            blur_sigma = estimate_blur(n0, b0, stroke_width(b0))
        else:
            blur_sigma = float(a.deblur)
        if blur_sigma and blur_sigma >= 0.3:
            gray = deblur(gray, blur_sigma)
    norm = flatten_background(gray)
    B, thr = binarize(norm, a.threshold)

    angle = 0.0
    if not a.no_deskew:
        angle = estimate_skew(B)
        if abs(angle) > 0.05:
            img = rotate(img, angle, (255, 255, 255))
            norm = rotate(norm, angle, 255)
            B, _ = binarize(norm, thr)
    B = hysteresis(norm, B, thr, a.hysteresis)
    H, W = B.shape
    dbg = out / "debug" if a.debug else None
    if dbg:
        dbg.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(dbg / "1_norm.png"), norm)
        cv2.imwrite(str(dbg / "2_binary.png"), 255 - B)

    hrules = vrules = None
    rule_px = np.zeros_like(B)
    if not a.keep_rules:
        # 印刷线常比笔迹淡, 二值化时可能只剩断断续续的残段: 用更靠近纸色的阈值单独检测横线位置
        paper = float(np.median(norm))
        Bs = binarize(norm, paper - 0.3 * (paper - thr))[0] if paper > thr + 10 else B
        T = np.ascontiguousarray
        hrules = find_rules(Bs, a.rule_cover)
        sw0 = stroke_width(cv2.bitwise_and(B, cv2.bitwise_not(hrules[0]))) if hrules else stroke_width(B)
        hrules = refine_rules(hrules, B, norm, vrun(B), sw0) if hrules else None  # 整页的横线位置: 用于分行
        B, px = strip_all(B, norm, Bs, a.rule_cover, sw0)
        rule_px |= px
        vr = find_rules(T(Bs.T), a.rule_cover)
        vrules = refine_rules(vr, T(B.T), T(norm.T), vrun(T(B.T)), sw0) if vr else None
        if vrules:
            Bt, pxt = strip_all(T(B.T), T(norm.T), T(Bs.T), a.rule_cover, sw0)
            B, rule_px = T(Bt.T), rule_px | T(pxt.T)

    sw = stroke_width(B)
    B = drop_blobs(B, sw * sw)  # 先去线圈等粗黑块, 它们的碎屑才不会被当成"挨着笔画的点"留下
    sw = stroke_width(B)
    B = clean_components(B, a.min_area if a.min_area is not None else max(4.0, 0.4 * sw * sw),
                         int(3 * sw), not a.keep_border)
    if dbg:
        cv2.imwrite(str(dbg / "3_clean.png"), 255 - B)

    # ---- 书写区域: 照片里的线圈、桌面、鼠标、纸边等杂物在区域外, 按连通域重心整块丢弃
    ocr_res = None
    if a.ocr or a.roi == "auto":
        ocr_res = ocr_raw(cv2.cvtColor(255 - B, cv2.COLOR_GRAY2BGR), a.unit == "char")
    roi = None
    if a.roi == "auto":
        roi = text_roi(ocr_res, W, H, a.roi_score) if ocr_res else None
    elif a.roi != "off":
        roi = tuple(int(v) for v in a.roi.split(","))
    if roi:
        _, lab0, _, cen0 = cv2.connectedComponentsWithStats(B, connectivity=8)
        rx0, ry0, rx1, ry1 = roi
        inside = (cen0[:, 0] >= rx0) & (cen0[:, 0] <= rx1) & (cen0[:, 1] >= ry0) & (cen0[:, 1] <= ry1)
        inside[0] = False
        B = (inside[lab0] * 255).astype(np.uint8)

    n, labels, st, cen = cv2.connectedComponentsWithStats(B, connectivity=8)
    comps = [Comp(i, *map(int, st[i]), float(cen[i][0]), float(cen[i][1])) for i in range(1, n)]
    if not comps:
        print(f"[{name}] 没有检测到笔迹", file=sys.stderr)
        return {}
    Wmap = ink_weight(norm, B, sw, cv2.dilate(rule_px, np.ones((3, 3), np.uint8)), a.edge)

    # 墨水颜色
    if a.color == "auto":
        core = cv2.erode(B, np.ones((3, 3), np.uint8))
        px = img[core > 0] if core.any() else img[B > 0]
        b_, g_, r_ = np.median(px, axis=0).astype(int)
        color = f"#{r_:02x}{g_:02x}{b_:02x}"
    else:
        color = a.color

    # ---- 分行
    olines = ocr_lines(ocr_res, 0.5) if (a.ocr and ocr_res) else []
    lines: list[dict] = []
    if olines:
        line_mode = "ocr"
        buckets = [[] for _ in olines]
        rest = []
        for c in comps:
            d = [-cv2.pointPolygonTest(L["quad"].astype(np.float32), (c.cx, c.cy), True)
                 for L in olines]
            j = int(np.argmin(d))
            lh = olines[j]["y1"] - olines[j]["y0"]
            (buckets[j] if d[j] < 0.35 * lh else rest).append(c)
        for L, cs in zip(olines, buckets):
            if cs:
                lines.append({"comps": cs, "ocr": L})
        if rest:  # OCR 漏掉的笔迹: 用投影法自成新行
            rmask = np.isin(labels, [c.idx for c in rest]).astype(np.uint8) * 255
            for cs in assign_to_bands(rest, profile_bands(rmask, rest, sw) or [(0, H)]):
                lines.append({"comps": cs, "ocr": None})
    else:
        use_rules = a.lines in ("auto", "rules") and hrules is not None and len(hrules[1]) >= 3
        if use_rules and a.lines == "auto":
            spacing = float(np.median(np.diff([(p + q) / 2 for p, q in hrules[1]])))
            tall = [c.h for c in comps if c.h > 3 * sw]
            if tall and np.percentile(tall, 75) > 1.1 * spacing:  # 字比格子还高: 没按横线写
                use_rules = False
        line_mode = "rules" if use_rules else "profile"
        bands = rule_bands(hrules, H) if use_rules else profile_bands(B, comps, sw)
        for cs in settle_lines(assign_to_bands(comps, bands or [(0, H)])):
            lines.append({"comps": cs, "ocr": None})
    lines.sort(key=lambda L: float(np.median([c.cy for c in L["comps"]])))

    comp_line = np.zeros(n, np.int32)
    for li, L in enumerate(lines, 1):
        for c in L["comps"]:
            comp_line[c.idx] = li
        L["bbox"] = (min(c.x for c in L["comps"]), min(c.y for c in L["comps"]),
                     max(c.x1 for c in L["comps"]), max(c.y1 for c in L["comps"]))
    line_map = comp_line[labels]
    tr = dict(scale=a.scale, smooth=a.smooth, alphamax=a.alphamax, turd=a.turd, opttol=a.opttol)
    char_map = np.zeros_like(labels)

    # ---- 轮廓放大: 结构(去线/分行/分字)已在原分辨率完成, 只把轮廓放到 2 倍图上算, 笔画更连贯、边缘更准
    up_method = None
    lh = float(np.median([L["bbox"][3] - L["bbox"][1] for L in lines]))
    if a.upscale != "off" and (a.upscale != "auto" or lh < 120):
        img_up, up_method = upscale2(img, "bicubic" if a.upscale == "bicubic" else "waifu2x")
        g_up = cv2.cvtColor(img_up, cv2.COLOR_BGR2GRAY)
        n_up = flatten_background(g_up)
        B_up0 = cv2.resize(B, (n_up.shape[1], n_up.shape[0]), interpolation=cv2.INTER_NEAREST)
        if a.deblur != "off":
            s_up = estimate_blur(n_up, B_up0, 2 * sw) if a.deblur == "auto" else 2 * float(a.deblur)
            if s_up and s_up >= 0.3:
                n_up = flatten_background(deblur(g_up, s_up))
        rp_up = cv2.resize(cv2.dilate(rule_px, np.ones((3, 3), np.uint8)), (n_up.shape[1], n_up.shape[0]),
                           interpolation=cv2.INTER_NEAREST)
        B_up = cv2.dilate(B_up0, np.ones((3, 3), np.uint8))  # 原分辨率掩膜放大后是台阶边, 外扩一点交给灰度定轮廓
        W_up = ink_weight(n_up, B_up, 2 * sw, rp_up, a.edge)
        W_up[(rp_up > 0) & (B_up0 == 0)] = 0
        del g_up, img_up
        ts = max(1, round(a.scale / 2))
        tr_up = dict(scale=ts, smooth=a.smooth, alphamax=a.alphamax, opttol=a.opttol,
                     turd=max(a.turd, int(0.4 * (2 * sw * ts) ** 2)))

    def vector(id_map, ident, bx0, by0, bx1, by1):
        M = (crop(id_map, bx0, by0, bx1, by1) == ident).astype(np.uint8) * 255
        if not up_method:
            return trace(soft_mask(M, crop(B, bx0, by0, bx1, by1), crop(Wmap, bx0, by0, bx1, by1)), **tr)
        size = (2 * (bx1 - bx0), 2 * (by1 - by0))
        k3 = np.ones((3, 3), np.uint8)
        Mu = cv2.dilate(cv2.resize(M, size, interpolation=cv2.INTER_NEAREST), k3)
        Bu = cv2.dilate(cv2.resize(crop(B, bx0, by0, bx1, by1), size, interpolation=cv2.INTER_NEAREST), k3)
        body = trace(soft_mask(Mu, Bu, crop(W_up, 2 * bx0, 2 * by0, 2 * bx1, 2 * by1)), **tr_up)
        return f'<g transform="scale(0.5)">{body}</g>' if body else ""

    # ---- 页面画布
    pad = a.pad
    if a.page == "content":
        px0 = max(0, min(L["bbox"][0] for L in lines) - 4 * pad)
        py0 = max(0, min(L["bbox"][1] for L in lines) - 4 * pad)
        px1 = min(W, max(L["bbox"][2] for L in lines) + 4 * pad)
        py1 = min(H, max(L["bbox"][3] for L in lines) + 4 * pad)
    else:
        px0, py0, px1, py1 = 0, 0, W, H

    for sub in ("lines", "chars"):
        for old in (out / sub).glob("*.svg") if (out / sub).is_dir() else ():
            old.unlink()
    (out / "lines").mkdir(parents=True, exist_ok=True)
    if not a.no_chars:
        (out / "chars").mkdir(parents=True, exist_ok=True)
    preview = img.copy()
    if roi:
        cv2.rectangle(preview, roi[:2], roi[2:], (220, 120, 0), 2)
    page_body, manifest_lines = [], []
    gid = 0

    for li, L in enumerate(lines, 1):
        lid = f"L{li:02d}"
        x0, y0, x1, y1 = L["bbox"]
        lab_crop = labels[y0:y1, x0:x1]
        lmask = line_map[y0:y1, x0:x1] == li

        cx0, cy0, cx1, cy1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
        body = vector(line_map, li, cx0, cy0, cx1, cy1)
        (out / "lines" / f"{lid}.svg").write_text(svg_doc(cx1 - cx0, cy1 - cy0, body, color), "utf-8")
        page_body.append(f'<g id="{lid}" transform="translate({cx0 - px0},{cy0 - py0})">{body}</g>')
        cv2.rectangle(preview, (x0, y0), (x1, y1), (0, 170, 0), 2)
        cv2.putText(preview, lid, (x0, max(12, y0 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 140, 0), 1)

        entry = {"id": lid, "svg": f"lines/{lid}.svg",
                 "bbox": [cx0 - px0, cy0 - py0, cx1 - cx0, cy1 - cy0],
                 "text": L["ocr"]["text"] if L["ocr"] else None, "chars": []}

        if not a.no_chars:
            proj = lmask.sum(0).astype(np.float32)
            labels_txt = None
            if L["ocr"] and L["ocr"]["chars"]:
                chs = list(L["ocr"]["chars"])
                # OCR 漏识的字(孤立的“一”、生僻字等): 几何切分出的字块若没被任何 OCR 字框覆盖, 单独成字
                for g in cc_cuts(L["comps"], proj, x0, a.script, a.unit, sw)[2]:
                    gw = g[1] - g[0]
                    if not any(min(g[1], c[3]) - max(g[0], c[2]) > 0.3 * min(gw, c[3] - c[2]) for c in chs):
                        chs.append((None, None, float(g[0]), float(g[1])))
                chs.sort(key=lambda c: (c[2] + c[3]) / 2)
                cuts, labels_txt = ocr_cuts(L["comps"], chs, proj, x0)
                S = float(y1 - y0)
            else:
                cuts, S, _ = cc_cuts(L["comps"], proj, x0, a.script, a.unit, sw)
            cuts = np.asarray(sorted(cuts), np.float64)

            charlab = np.zeros(lmask.shape, np.int32)  # 行内像素 -> 区间号+1
            for c in L["comps"]:
                sub = lab_crop == c.idx
                ys, xs = np.nonzero(sub)
                iv = np.searchsorted(cuts, xs + x0, side="right")
                cnt = np.bincount(iv, minlength=len(cuts) + 1)
                dom = int(np.argmax(cnt))
                if cnt[dom] >= 0.75 * len(xs) or c.w < 0.8 * S:  # 主要落在一个字里: 整块归它
                    charlab[ys, xs] = dom + 1
                else:  # 跨字粘连的笔画: 沿切分线按像素拆开
                    charlab[ys, xs] = iv + 1

            ci = 0
            for k in range(len(cuts) + 1):
                cm = charlab == k + 1
                if not cm.any():
                    continue
                ci += 1
                ys, xs = np.nonzero(cm)
                gx0, gx1 = x0 + int(xs.min()), x0 + int(xs.max()) + 1
                gy0, gy1 = y0 + int(ys.min()), y0 + int(ys.max()) + 1
                if a.char_canvas == "tight":
                    bx0, by0, bx1, by1 = gx0 - pad, gy0 - pad, gx1 + pad, gy1 + pad
                elif a.char_canvas == "square":
                    side = max(y1 - y0, gx1 - gx0) + 2 * pad
                    bx0 = (gx0 + gx1) // 2 - side // 2
                    by0 = (y0 + y1) // 2 - side // 2
                    bx1, by1 = bx0 + side, by0 + side
                else:  # line: 纵向沿用整行范围, 横向贴字, 横排拼接时基线自然对齐
                    bx0, by0, bx1, by1 = gx0 - pad, y0 - pad, gx1 + pad, y1 + pad
                gid += 1
                char_map[y0:y1, x0:x1][cm] = gid
                txt, score = (labels_txt[k] if labels_txt and k < len(labels_txt) else (None, None))
                cid = f"{lid}_C{ci:03d}"
                fname = f"{cid}_{safe_name(txt)}.svg" if txt and safe_name(txt) else f"{cid}.svg"
                (out / "chars" / fname).write_text(
                    svg_doc(bx1 - bx0, by1 - by0, vector(char_map, gid, bx0, by0, bx1, by1), color), "utf-8")
                cv2.rectangle(preview, (gx0, gy0), (gx1, gy1), (0, 0, 230), 1)
                entry["chars"].append({
                    "id": cid, "svg": f"chars/{fname}", "text": txt,
                    "score": round(score, 3) if score is not None else None,
                    "bbox": [bx0 - px0, by0 - py0, bx1 - bx0, by1 - by0],
                    "ink_bbox": [gx0 - px0, gy0 - py0, gx1 - gx0, gy1 - gy0]})
        manifest_lines.append(entry)

    (out / "page.svg").write_text(svg_doc(px1 - px0, py1 - py0, "\n".join(page_body), color), "utf-8")

    # 透明底 PNG(方便预览/直接贴图)与检查用预览图
    alpha = soft_mask(B[py0:py1, px0:px1], B[py0:py1, px0:px1], Wmap[py0:py1, px0:px1])
    rgba = np.zeros((*alpha.shape, 4), np.uint8)
    hexc = color.lstrip("#")
    rgba[..., 0], rgba[..., 1], rgba[..., 2] = int(hexc[4:6], 16), int(hexc[2:4], 16), int(hexc[0:2], 16)
    rgba[..., 3] = np.clip(alpha * 255, 0, 255).astype(np.uint8)
    cv2.imwrite(str(out / "page_clean.png"), rgba)
    cv2.imwrite(str(out / "preview.jpg"), preview[py0:py1, px0:px1], [cv2.IMWRITE_JPEG_QUALITY, 85])

    manifest = {
        "source": str(src), "deskew_deg": round(angle, 2), "threshold": round(thr, 1),
        "roi": list(roi) if roi else None,
        "deblur_sigma": round(blur_sigma, 2) if blur_sigma else None,
        "upscale": up_method,
        "stroke_width": round(sw, 2), "color": color,
        "rules": {"horizontal": len(hrules[1]) if hrules else 0,
                  "vertical": len(vrules[1]) if vrules else 0},
        "line_mode": line_mode, "char_mode": "ocr" if olines else ("off" if a.no_chars else a.script),
        "unit": a.unit,
        "char_canvas": a.char_canvas,
        "page": {"svg": "page.svg", "width": px1 - px0, "height": py1 - py0,
                 "origin": [px0, py0]},  # page.svg 左上角在纠偏后原图中的位置
        "lines": manifest_lines,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    nchar = sum(len(L["chars"]) for L in manifest_lines)
    print(f"[{name}] 纠偏 {angle:+.2f}°, 横线 {manifest['rules']['horizontal']} 条, "
          f"竖线 {manifest['rules']['vertical']} 条, {len(manifest_lines)} 行, {nchar} 字 -> {out}")
    return manifest


def main(argv=None):
    p = argparse.ArgumentParser(description="手写字迹 -> 透明无横线 SVG(整页/逐行/逐字)")
    p.add_argument("inputs", nargs="+", type=Path, help="图片/PDF 文件或目录")
    p.add_argument("-o", "--out", type=Path, default=Path("hw_svg_out"), help="输出目录")
    p.add_argument("--ocr", action="store_true", help="用 RapidOCR 引导分行分字, 并按识别结果给文件命名")
    p.add_argument("--unit", choices=["char", "word"], default="char",
                   help="英文/数字的切分粒度: char=逐字母, word=按单词 (中文始终逐字)")
    p.add_argument("--roi", default="auto",
                   help="书写区域: auto=用 OCR 文字框自动圈定(去掉线圈/桌面等杂物), off=整张图, 或 x0,y0,x1,y1(纠偏后像素坐标)")
    p.add_argument("--roi-score", type=float, default=0.85, help="自动圈定书写区域时采信的 OCR 行置信度")
    p.add_argument("--script", choices=["cjk", "latin"], default="cjk",
                   help="无 OCR 时按哪种文字切分: cjk=汉字(偏旁合并成方块字), latin=拉丁字母; 开 --ocr 时自动处理")
    p.add_argument("--lines", choices=["auto", "rules", "profile"], default="auto",
                   help="分行依据: 横线位置 / 水平投影 (auto: 有横线且按线书写时用横线)")
    p.add_argument("--char-canvas", choices=["line", "tight", "square"], default="line",
                   help="单字 SVG 画布: line=高度同整行(便于横排拼接), tight=紧贴, square=正方形")
    p.add_argument("--no-chars", action="store_true", help="只输出整页和逐行")
    p.add_argument("--color", default="#000000", help='填充色, 如 "#1a1a1a"; auto=取原墨水颜色')
    p.add_argument("--page", choices=["content", "full"], default="content",
                   help="整页 SVG 画布: content=裁到笔迹范围, full=原图尺寸")
    p.add_argument("--threshold", type=float, default=None, help="二值化阈值 0-255(默认自动)")
    p.add_argument("--upscale", choices=["auto", "waifu2x", "bicubic", "off"], default="auto",
                   help="轮廓放大 2 倍再计算(结构步骤仍在原分辨率): auto=行高 <120px 时用 waifu2x(未安装则双三次)")
    p.add_argument("--deblur", default="auto",
                   help="去模糊: auto=自动估计模糊程度后做 Richardson-Lucy 去模糊, off=关闭, 或直接给高斯 σ(像素)")
    p.add_argument("--hysteresis", type=float, default=0.0,
                   help="滞后阈值(默认关): 弱阈值 = 阈值 + k*(纸色-阈值), 与强墨迹相连的淡笔画也保留。"
                        "铅笔/淡色笔写在白纸上断笔多时可设 0.5; 横线纸上会干扰去线, 不建议")
    p.add_argument("--rule-cover", type=float, default=0.25,
                   help="判定为印刷线所需的最小横向覆盖率(占图宽比例)")
    p.add_argument("--keep-rules", action="store_true", help="不去横线/竖线")
    p.add_argument("--keep-border", action="store_true", help="保留贴边的大块暗区")
    p.add_argument("--no-deskew", action="store_true", help="不自动纠偏")
    p.add_argument("--min-area", type=float, default=None, help="小于此面积(像素)的斑点视为噪声")
    p.add_argument("--pad", type=int, default=4, help="SVG 留白(像素)")
    p.add_argument("--scale", type=int, default=3, help="矢量化前放大倍数(越大边缘越精细)")
    p.add_argument("--smooth", type=float, default=1.0, help="边缘平滑强度(原图像素), 0=不平滑; 毛刺多可调大, 细节糊可调小")
    p.add_argument("--edge", type=float, default=0.5,
                   help="轮廓位置: 0.5=笔画最深处与纸色正中(还原真实粗细); 调大变粗(如 0.65), 调小变细(如 0.35)")
    p.add_argument("--alphamax", type=float, default=1.0, help="potrace 拐角平滑度, 0=折线 1.33=最圆滑")
    p.add_argument("--opttol", type=float, default=0.2, help="potrace 曲线优化容差")
    p.add_argument("--turd", type=int, default=2, help="potrace 忽略的最小斑点面积")
    p.add_argument("--max-side", type=int, default=5000, help="超过此边长先缩小")
    p.add_argument("--debug", action="store_true", help="输出中间结果到 debug/")
    a = p.parse_args(argv)

    if not Path(POTRACE).exists():
        raise SystemExit("找不到 potrace, 请先运行: brew install potrace")
    files = []
    for x in a.inputs:
        if x.is_dir():
            files += sorted(f for f in x.iterdir() if f.suffix.lower() in IMG_EXTS)
        else:
            files.append(x)
    if not files:
        raise SystemExit("没有找到输入图片")
    pages = [(name, img, f) for f in files for name, img in load_pages(f)]
    for name, img, f in pages:
        process(name, img, f, a.out / name if len(pages) > 1 else a.out, a)


if __name__ == "__main__":
    main()
