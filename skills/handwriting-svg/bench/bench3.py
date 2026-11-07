"""第三轮评测: 超分辨率(waifu2x)放大后再走当前 skill 流程。对照: 不放大 / 双三次放大。"""
import subprocess, sys
from pathlib import Path
import cv2, numpy as np
ARGS = list(sys.argv); sys.argv = ["x"]
exec(open(Path(__file__).parent / "bench2.py").read().split("V = {")[0])
sys.argv = ARGS
W2X = Path.home() / ".claude/skills/handwriting-svg/tools/waifu2x-ncnn-vulkan-20250915-macos"
CACHE = Path(__file__).parent / "w2x_cache"; CACHE.mkdir(exist_ok=True)

def up(f, mode):
    if mode == "none":
        return cv2.imread(str(f), 0), 1
    if mode == "bicubic":
        return cv2.resize(cv2.imread(str(f), 0), None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 2
    model, n, sc = mode.split(":")
    o = CACHE / f"{f.stem}_{model}_{n}_{sc}.png"
    if not o.exists():
        subprocess.run([str(W2X / "waifu2x-ncnn-vulkan"), "-i", str(f), "-o", str(o), "-n", n, "-s", sc, "-m", str(W2X / f"models-{model}")],
                       capture_output=True, check=True)
    return cv2.imread(str(o), 0), int(sc)

MODES = {"不放大(当前 skill)": "none", "双三次放大2x": "bicubic",
         "waifu2x cunet 2x 去噪0": "cunet:0:2", "waifu2x cunet 2x 去噪1": "cunet:1:2", "waifu2x cunet 2x 去噪2": "cunet:2:2",
         "waifu2x cunet 2x 去噪3": "cunet:3:2", "waifu2x photo 2x 去噪1": "upconv_7_photo:1:2",
         "waifu2x anime 2x 去噪1": "upconv_7_anime_style_art_rgb:1:2", "waifu2x cunet 4x 去噪1": "cunet:1:4"}
names = sys.argv[2].split("|") if len(sys.argv) > 2 else list(MODES)
for name in names:
    rows = {"32": [], "64": []}
    for f in sorted(Path(sys.argv[1]).glob("img_*.png")):
        gt = cv2.imread(str(f).replace("img_", "gt4_"), 0) > 127
        img, sc = up(f, MODES[name])
        out = run2(img, {"rl_auto": (1.0, 10)}, 4.0 / sc)
        out = out[:gt.shape[0], :gt.shape[1]]
        if out.shape != gt.shape:
            pad = np.zeros_like(gt); pad[:out.shape[0], :out.shape[1]] = out; out = pad
        f1, th, br, nz, n = metrics(out, gt)
        rows["32" if "_32_" in f.name else "64"].append((f1, th, br, nz))
    msg = []
    for k, r in rows.items():
        a = np.array(r)
        msg.append(f"字高{k}px: F1 {a[:,0].mean():.3f} 粗细 {a[:,1].mean():.2f} 断笔 {a[:,2].sum():3.0f} 噪点 {a[:,3].sum():2.0f}")
    print(f"{name:22s} | " + " | ".join(msg), flush=True)
