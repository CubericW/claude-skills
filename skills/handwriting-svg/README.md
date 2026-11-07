# handwriting-svg

手写照片/扫描件 → 无背景、无横线的透明矢量 SVG(整页 / 逐行 / 逐字), 并可按字重新拼接。
用法见 [SKILL.md](SKILL.md)。

## 选型调研(2026-10)

GitHub 上没有一个公认成熟、能一站式完成"去横线 → 矢量化 → 逐字切分"的项目(相关关键词搜到的基本是
0 star 的个人仓库), 因此用三个各自领域里最成熟的组件拼成流水线:

| 组件 | 角色 | 认可度 |
|---|---|---|
| [OpenCV](https://github.com/opencv/opencv) | 光照拉平、二值化、纠偏、横线/竖线检测与去除、连通域分行分字 | ~91k★, Apache-2.0 |
| [Potrace](https://potrace.sourceforge.net/) (Peter Selinger) | 位图 → 贝塞尔曲线 SVG | 事实标准; Inkscape“描摹位图”、FontForge 自动描边都用它; GPL |
| [waifu2x-ncnn-vulkan](https://github.com/nihui/waifu2x-ncnn-vulkan)(可选) | 轮廓放大 2 倍, 减少低分辨率照片的断笔 | 原版 waifu2x 28k★ 的 ncnn/Vulkan 移植, 3.5k★, MIT; Apple GPU 运行 |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) + [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) 模型 | 可选: 行框/单字框 + 识别文字, 用于引导切分和命名 | 8.3k★ / 91.6k★, Apache-2.0; ONNX Runtime 纯 CPU 运行 |

考察过但没采用的:

| 项目 | 不选的原因 |
|---|---|
| [visioncortex/vtracer](https://github.com/visioncortex/vtracer) (~7.4k★, MIT) | 强项是彩色图分层矢量化; 黑白手写用 Potrace 曲线更干净、节点更少 |
| [mzucker/noteshrink](https://github.com/mzucker/noteshrink) (~4.8k★, MIT) | 清理手写笔记扫描件并输出 PDF; 横线会当作一种颜色保留, 不切分、不出 SVG, 2024 年起停更 |
| [mittagessen/kraken](https://github.com/mittagessen/kraken) (~1.1k★, Apache-2.0) | 面向历史文献的行分割/识别, 依赖 PyTorch, 只到行级, 不适合中文逐字 |
| [JaidedAI/EasyOCR](https://github.com/JaidedAI/EasyOCR) (~30k★) | 只有行/词框, 没有单字坐标 |
| [LingDong-/qiji-font](https://github.com/LingDong-/qiji-font) (~1.7k★) | 思路相同(阈值→连通域→OCR→potrace), 但为特定刻本定制, 不能直接复用 |

## 实测(2026-10-25, 同一页横线笔记)

| | iOS 扫描 PDF | 手机直接拍照 |
|---|---|---|
| 汉字高度 | 约 66 px | 约 33 px |
| OCR 字符准确率 | 100% (111/111) | 96.4% (107/111) |
| 杂物 | 线圈 | 线圈、桌面、鼠标、书签、纸边 |
| 观感 | 笔画结构清楚 | 细笔画被模糊晕粗, 放大后偏肥 |

结论: 流水线两种输入都能处理, 但输入质量决定上限, 推荐先用手机“扫描文稿”。
PDF 输入优先无损抽取内嵌 JPEG; 没有内嵌图像的 PDF 需要 Ghostscript(`brew install ghostscript`)。

## 本地文件

```
SKILL.md          技能说明(Claude 读取)
setup.sh          安装/修复环境: brew potrace + uv venv + 固定版本依赖
requirements.txt  opencv-python-headless / numpy / rapidocr / onnxruntime
scripts/hw2svg.py 主流程
scripts/stitch.py 拼接工具
scripts/svg2png.py 精确渲染输出的 SVG(检查效果用)
tools/            可选: waifu2x-ncnn-vulkan 20250915 (MIT, nihui/waifu2x-ncnn-vulkan 官方 macOS 发布版), 轮廓放大用
bench/            合成评测集: bench_make.py 生成(已知字形+模糊/噪点/JPEG/笔压/横线), bench_run.py 对比各方案的 F1/粗细/断笔/噪点
.venv/            Python 3.12 虚拟环境(setup.sh 生成)
```
