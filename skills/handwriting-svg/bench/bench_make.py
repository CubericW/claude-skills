"""合成评测集: 已知字形(标准答案) + 真实拍照/扫描的退化。输出 img_*.png 与 gt_*.png。"""
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
A = "/System/Library/AssetsV2/com_apple_MobileAsset_Font8"
FONTS = {"hanzipen": f"{A}/a3c69464b629577766c23bcdb12ffbfe3759b923.asset/AssetData/Hanzipen.ttc",
         "hannotate": f"{A}/00bfc46ccb002b730e29def5116e0a571fb617d8.asset/AssetData/Hannotate.ttc"}
TEXT = ["这是一段用来比较排板效果的测试文字", "春眠不觉晓处处闻啼鸟夜来风雨声", "江河湖海川流不息小草生长明月",
        "ABCDEFGHIJ 2846 65 47 33 xyz"]


def render_hi(font_path, scale=8):
    """在高分辨率下渲染干净字形, 返回 0/1 掩膜(行高约 26*scale)。"""
    size = 26 * scale
    font = ImageFont.truetype(font_path, size)
    W, H = int(size * 17.5), int(size * 1.9 * len(TEXT) + size)
    im = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(im)
    for i, t in enumerate(TEXT):
        d.text((size // 2, size // 2 + i * int(size * 1.9)), t, font=font, fill=255)
    return np.asarray(im, np.float32) / 255.0


def degrade(gt_hi, char_px, rules, rng, quality):
    f = char_px / (26 * 8)
    h, w = int(gt_hi.shape[0] * f), int(gt_hi.shape[1] * f)
    cov = cv2.resize(gt_hi, (w, h), interpolation=cv2.INTER_AREA)          # 抗锯齿覆盖率
    gt = (cov >= 0.5).astype(np.uint8) * 255
    gt4 = (cv2.resize(gt_hi, (w * 4, h * 4), interpolation=cv2.INTER_AREA) >= 0.5).astype(np.uint8) * 255  # 矢量输出按 4 倍比较
    # 笔压: 低频随机场, 墨色在 35%~100% 之间变化(淡的地方容易断笔)
    field = cv2.resize(rng.random((max(2, h // 40), max(2, w // 40))).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    darkness = np.clip(0.35 + 0.9 * field, 0.35, 1.0)
    paper, ink = 238.0, 35.0
    img = paper - cov * darkness * (paper - ink)
    if rules:                                                                 # 印刷横线(浅灰, 2px)
        step = int(char_px * 1.9 / (26 * 8) * 26 * 8)
        for y in range(int(char_px * 1.25), h, int(char_px * 1.9)):
            img[y:y + 2] = np.minimum(img[y:y + 2], 150)
    yy, xx = np.mgrid[0:h, 0:w]
    img = img * (0.85 + 0.15 * (xx / w))                                      # 光照不均
    img = cv2.GaussianBlur(img, (0, 0), 0.9 if quality == "photo" else 0.5)  # 镜头/对焦模糊
    img = img + rng.normal(0, 7 if quality == "photo" else 3, img.shape)     # 传感器噪点 + 纸纹
    img = np.clip(img, 0, 255).astype(np.uint8)
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 65 if quality == "photo" else 88])
    return cv2.imdecode(enc, cv2.IMREAD_GRAYSCALE), gt, gt4


rng = np.random.default_rng(42)
for fname, fpath in FONTS.items():
    hi = render_hi(fpath)
    for char_px, quality in ((32, "photo"), (64, "scan")):
        for rules in (False, True):
            img, gt, gt4 = degrade(hi, char_px, rules, rng, quality)
            tag = f"{fname}_{char_px}_{'rules' if rules else 'blank'}"
            cv2.imwrite(str(out / f"img_{tag}.png"), img)
            cv2.imwrite(str(out / f"gt_{tag}.png"), gt)
            cv2.imwrite(str(out / f"gt4_{tag}.png"), gt4)
            print(tag, img.shape)
