"""精确栅格化 hw2svg 生成的 SVG(potrace 路径 + translate/scale 变换)。"""
import re
import cv2, numpy as np

def _tf(chain):
    M = np.eye(3)
    for op, args in re.findall(r"(translate|scale)\(([^)]*)\)", chain):
        v = [float(t) for t in re.split(r"[ ,]+", args.strip())]
        if op == "translate":
            T = np.array([[1, 0, v[0]], [0, 1, v[1] if len(v) > 1 else 0], [0, 0, 1]])
        else:
            T = np.array([[v[0], 0, 0], [0, v[1] if len(v) > 1 else v[0], 0], [0, 0, 1]])
        M = M @ T
    return M

def _polys(d):
    toks = re.findall(r"[MmCcLlZz]|-?\d+(?:\.\d+)?", d)
    polys, cur, pos, cmd, i = [], [], np.zeros(2), None, 0
    while i < len(toks):
        t = toks[i]
        if t in "MmCcLlZz":
            cmd = t; i += 1
            if t in "Zz":
                if cur: polys.append(np.array(cur)); cur = []
            continue
        if cmd in "Mm":
            p = np.array([float(toks[i]), float(toks[i + 1])]); i += 2
            pos = p if cmd == "M" or not cur and not polys else pos + p
            if cur: polys.append(np.array(cur))
            cur = [pos.copy()]; cmd = "l" if cmd == "m" else "L"
        elif cmd in "Cc":
            v = np.array([float(x) for x in toks[i:i + 6]]).reshape(3, 2); i += 6
            p0 = pos; c1, c2, p3 = (v + p0) if cmd == "c" else v
            for t_ in np.linspace(0, 1, 9)[1:]:
                cur.append((1 - t_) ** 3 * p0 + 3 * (1 - t_) ** 2 * t_ * c1 + 3 * (1 - t_) * t_ ** 2 * c2 + t_ ** 3 * p3)
            pos = p3
        elif cmd in "Ll":
            p = np.array([float(toks[i]), float(toks[i + 1])]); i += 2
            pos = pos + p if cmd == "l" else p; cur.append(pos.copy())
    if cur: polys.append(np.array(cur))
    return polys

def rasterize(svg_text, k=1.0, ss=4):
    """按 ss 倍超采样、非抗锯齿填充, 再按面积缩小: 得到准确的覆盖率(OpenCV 的 LINE_AA 填充会向外多涂约半个像素)。"""
    W, H = map(float, re.search(r'width="([\d.]+)" height="([\d.]+)"', svg_text).groups())
    out_w, out_h = int(W * k), int(H * k)
    k = k * ss
    img = np.full((out_h * ss, out_w * ss), 255, np.uint8)
    all_pts = []
    # 每个最内层 <g transform=...> 及其祖先的变换链
    for m in re.finditer(r"<path d=\"([^\"]+)\"", svg_text):
        head = svg_text[:m.start()]
        opens = [(g.start(), g.group(1)) for g in re.finditer(r'<g[^>]*?transform="([^"]+)"', head)]
        depth, chain = 0, []
        # 简单栈: 从后往前数未闭合的 <g
        stack = []
        for g in re.finditer(r"<g\b[^>]*>|</g>", head):
            if g.group(0) == "</g>": stack.pop()
            else:
                t = re.search(r'transform="([^"]+)"', g.group(0)); stack.append(t.group(1) if t else "")
        Mx = np.diag([k, k, 1.0]) @ _tf(" ".join(stack))
        for poly in _polys(m.group(1)):
            P = (Mx @ np.c_[poly, np.ones(len(poly))].T).T[:, :2]
            all_pts.append(np.round(P * 16).astype(np.int32))
    if all_pts:
        cv2.fillPoly(img, all_pts, 0, lineType=cv2.LINE_8, shift=4)
    return cv2.resize(img, (out_w, out_h), interpolation=cv2.INTER_AREA) if ss > 1 else img
