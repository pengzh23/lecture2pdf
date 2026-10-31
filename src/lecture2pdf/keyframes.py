"""
本地关键帧提取（不调用任何 AI）。

思路：
  1. 一次解码同时输出两路：分析用的低分辨率灰度原始帧 small.raw，
     以及原分辨率 JPG（frames/），默认每 2 秒一帧。
  2. 每 window 帧（默认 5 帧 = 10 秒）取逐像素时间中位数 → 「稳定帧」，
     走动的老师基本会被抹掉。
  3. 稳定帧上取「笔迹图」：像素与局部均值的差 > 阈值（粉笔字、PPT 文字都适用），
     并扣掉几乎全程都存在的静态细节（黑板边框、讲台、台标水印）。
  4. 只关注已经存在一段时间（min_age）的「老笔迹」——板书会停留几分钟，老师身上的边缘不会。
     画面切成网格，若某个格子里大部分老笔迹在随后两个稳定帧里都不再出现，
     判定为「擦板 / 换页 / 机位变化」事件。按块判断是因为老师常常只擦一块黑板。
  5. 事件前若干稳定帧里挑内容最满的一帧作为关键帧；超过 max_gap 没有事件时补一张；
     视频结尾补一张。
  6. 去冗余：若一张关键帧的内容几乎被下一张完全包含（只写不擦），删掉前一张。
  7. 关键帧用原分辨率帧的时间中位数合成（去遮挡），再做光照均衡与锐化。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

GRID_Y, GRID_X = 6, 8


# ------------------------------------------------------------------ 图像小工具

def box_blur(img: np.ndarray, r: int) -> np.ndarray:
    """积分图实现的均值滤波（不依赖 OpenCV/SciPy）。支持 (H,W) 与 (H,W,C)。"""
    img = img.astype(np.float32)
    pad = [(r + 1, r), (r + 1, r)] + [(0, 0)] * (img.ndim - 2)
    p = np.pad(img, pad, mode="edge")
    c = p.cumsum(0).cumsum(1)
    k = 2 * r + 1
    s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    return s / (k * k)


def dilate(m: np.ndarray, r: int) -> np.ndarray:
    """二值膨胀（可分离的方形结构元）。"""
    out = m.copy()
    for d in range(1, r + 1):
        out[d:] |= m[:-d]
        out[:-d] |= m[d:]
    m2 = out.copy()
    for d in range(1, r + 1):
        out[:, d:] |= m2[:, :-d]
        out[:, :-d] |= m2[:, d:]
    return out


def cell_sum(m: np.ndarray) -> np.ndarray:
    h, w = m.shape[0] // GRID_Y, m.shape[1] // GRID_X
    return m[:GRID_Y * h, :GRID_X * w].reshape(GRID_Y, h, GRID_X, w).sum((1, 3))


# ------------------------------------------------------------------ 1. 抽帧

def sample_frames(video: Path, out_dir: Path, interval: float, width: int,
                  src_w: int, src_h: int) -> dict:
    meta_path = out_dir / "frames.json"
    if meta_path.exists() and (out_dir / "small.raw").exists() and (out_dir / "frames").is_dir():
        return json.loads(meta_path.read_text())
    frames_dir = out_dir / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)
    height = int(round(width * src_h / src_w / 2) * 2)
    raw = out_dir / "small.raw"
    fps = f"1/{interval:g}"
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-v", "error", "-i", str(video),
        "-filter_complex",
        f"fps={fps},split=2[a][b];[a]scale={width}:{height},format=gray[s]",
        "-map", "[s]", "-f", "rawvideo", str(raw),
        "-map", "[b]", "-q:v", "2", str(frames_dir / "%05d.jpg"),
    ]
    logger.info(f"抽帧中（每 {interval:g}s 一帧；长视频需要几分钟，主要耗时在视频解码）")
    subprocess.run(cmd, check=True)
    n = raw.stat().st_size // (width * height)
    n_jpg = len(list(frames_dir.glob("*.jpg")))
    meta = {"interval": interval, "width": width, "height": height, "n": int(min(n, n_jpg)),
            "src_w": src_w, "src_h": src_h}
    meta_path.write_text(json.dumps(meta))
    logger.info(f"抽帧完成：{meta['n']} 帧")
    return meta


# ------------------------------------------------------------------ 2~6. 检测

@dataclass
class Keyframe:
    index: int          # 序号（从 1 开始）
    time: float         # 关键帧对应时刻（秒）：该版面「最满」的时刻
    seg_start: float    # 该版面对应讲解的起止时间
    seg_end: float
    reason: str         # erase / periodic / end
    image: str          # 相对工作目录的图片路径


def detect_keyframes(out_dir: Path, meta: dict, duration: float, s) -> list[Keyframe]:
    """s: Settings"""
    W, H, n, itv = meta["width"], meta["height"], meta["n"], meta["interval"]
    raw = np.memmap(out_dir / "small.raw", dtype=np.uint8, mode="r", shape=(n, H, W))
    win = max(1, s.window)
    K = n // win
    if K < 3:
        raise RuntimeError("视频太短，无法提取关键帧")
    win_sec = win * itv

    # 稳定帧与笔迹图
    D = np.empty((K, H, W), dtype=bool)
    for k in range(K):
        st = np.median(raw[k * win:(k + 1) * win], axis=0).astype(np.float32)
        D[k] = np.abs(st - box_blur(st, 6)) > 10
    static = D.mean(0) > 0.9
    D &= ~static
    Dd = np.stack([dilate(d, 2) for d in D])

    min_age = max(1, int(round(s.min_age / win_sec)))
    cell_area = (H // GRID_Y) * (W // GRID_X)
    age = np.zeros((H, W), dtype=np.int32)
    aged_count = np.zeros(K, dtype=np.int64)
    events: list[int] = []
    for k in range(K):
        age = (age + 1) * D[k]
        aged = D[k] & (age >= min_age)
        aged_count[k] = aged.sum()
        if k >= K - 2:
            continue
        lost = aged & ~Dd[k + 1] & ~Dd[k + 2]
        ca, cl = cell_sum(aged), cell_sum(lost)
        frac = np.where(ca > 0.01 * cell_area, cl / np.maximum(ca, 1), 0.0)
        if (frac > s.lost_ratio).any() and lost.sum() > s.min_lost_area * H * W:
            events.append(k)

    # 合并相邻事件（擦一块黑板通常持续十几秒到半分钟）
    merged: list[int] = []
    for k in events:
        if not merged or (k - merged[-1]) * win_sec >= s.min_gap:
            merged.append(k)
    logger.info(f"检测到 {len(events)} 个变化窗口，合并后 {len(merged)} 个事件")

    # 每个事件前挑内容最满的稳定帧
    # (最满的稳定帧, 原因, 事件所在稳定帧)
    cand: list[tuple[int, str, int]] = []
    look = max(1, int(round(30 / win_sec)))
    prev = -1
    for k in merged:
        lo = max(prev + 1, k - look)
        best = lo + int(np.argmax(aged_count[lo:k + 1] + D[lo:k + 1].sum((1, 2)) * 0.25))
        cand.append((best, "erase", k))
        prev = k
    # 视频结尾
    if not cand or cand[-1][0] < K - 1:
        cand.append((K - 1, "end", K - 1))

    # 兜底：间隔过长时补帧
    max_gap_k = max(1, int(s.max_gap / win_sec))
    filled: list[tuple[int, str, int]] = []
    last = -1
    for k, why, ev in cand:
        while k - last > max_gap_k:
            last += max_gap_k
            filled.append((last, "periodic", last))
        filled.append((k, why, ev))
        last = k
    cand = filled

    # 去冗余：前一张被后一张包含
    kept: list[tuple[int, str, int]] = []
    for i, (k, why, ev) in enumerate(cand):
        if i + 1 < len(cand):
            nk = cand[i + 1][0]
            base = D[k]
            tot = base.sum()
            cover = (base & Dd[nk]).sum() / tot if tot else 1.0
            if cover >= s.redundant_cover:
                logger.debug(f"  关键帧 @{k * win_sec:.0f}s 被下一张覆盖（{cover:.2f}），跳过")
                continue
        kept.append((k, why, ev))
    logger.info(f"去冗余后保留 {len(kept)} 张关键帧")

    # 合成原分辨率图像
    kf_dir = out_dir / "keyframes"
    if kf_dir.exists():
        shutil.rmtree(kf_dir)
    kf_dir.mkdir()
    crop = s.crop or (auto_crop(D, [k for k, _, _ in kept], meta) if s.auto_crop else None)
    if crop:
        logger.info(f"裁剪区域（原分辨率）: x={crop[0]} y={crop[1]} w={crop[2]} h={crop[3]}")

    result: list[Keyframe] = []
    seg_start = 0.0
    for i, (k, why, ev) in enumerate(kept, 1):
        idx = range(k * win, min((k + 1) * win, n))
        img = compose(out_dir / "frames", idx)
        if crop:
            x, y, w, h = crop
            img = img[y:y + h, x:x + w]
        if s.enhance:
            img = enhance(img)
        rel = f"keyframes/kf_{i:02d}.jpg"
        from PIL import Image

        Image.fromarray(img).save(out_dir / rel, quality=92)
        t = (k + 0.5) * win_sec
        # 讲解时段：从上一段结束到「该版面开始被擦掉」为止
        seg_end = duration if i == len(kept) else min(duration, (ev + 1) * win_sec)
        result.append(Keyframe(i, round(min(t, duration), 1), round(seg_start, 1),
                               round(seg_end, 1), why, rel))
        seg_start = seg_end
    (out_dir / "keyframes.json").write_text(
        json.dumps([asdict(r) for r in result], ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def load_keyframes(out_dir: Path) -> list[Keyframe]:
    data = json.loads((out_dir / "keyframes.json").read_text(encoding="utf-8"))
    return [Keyframe(**d) for d in data]


# ------------------------------------------------------------------ 7. 合成与增强

def compose(frames_dir: Path, idx) -> np.ndarray:
    """原分辨率帧逐像素中位数：去掉在窗口内移动的遮挡物。"""
    from PIL import Image

    imgs = [np.asarray(Image.open(frames_dir / f"{i + 1:05d}.jpg").convert("RGB")) for i in idx]
    if len(imgs) == 1:
        return imgs[0]
    return np.median(np.stack(imgs), axis=0).astype(np.uint8)


def enhance(img: np.ndarray) -> np.ndarray:
    """光照均衡（除以大尺度亮度）+ 自动对比度 + 锐化。用于缓解「黑板一边亮一边暗」。"""
    from PIL import Image, ImageFilter, ImageOps

    f = img.astype(np.float32)
    gray = f.mean(2)
    r = max(8, img.shape[1] // 25)
    small = gray[::4, ::4]
    illum = box_blur(small, max(2, r // 4))
    illum = np.kron(illum, np.ones((4, 4), np.float32))[:gray.shape[0], :gray.shape[1]]
    target = float(np.median(illum))
    gain = np.clip(target / np.maximum(illum, 1), 0.6, 2.5)[..., None]
    out = np.clip(f * gain, 0, 255).astype(np.uint8)
    pil = ImageOps.autocontrast(Image.fromarray(out), cutoff=0.5)
    pil = pil.filter(ImageFilter.UnsharpMask(radius=2, percent=70, threshold=2))
    return np.asarray(pil)


def auto_crop(D: np.ndarray, ks: list[int], meta: dict) -> tuple[int, int, int, int] | None:
    """按关键帧上笔迹的分布裁掉地面、讲台等无内容区域（保留 98% 的笔迹）。"""
    acc = np.zeros(D.shape[1:], dtype=np.float32)
    for k in ks:
        acc += D[k]
    if acc.sum() == 0:
        return None
    H, W = acc.shape

    def span(profile):
        c = np.cumsum(profile) / profile.sum()
        lo = int(np.searchsorted(c, 0.01))
        hi = int(np.searchsorted(c, 0.99))
        return lo, hi

    y0, y1 = span(acc.sum(1))
    x0, x1 = span(acc.sum(0))
    my, mx = int(0.04 * H), int(0.03 * W)
    y0, y1 = max(0, y0 - my), min(H, y1 + my)
    x0, x1 = max(0, x0 - mx), min(W, x1 + mx)
    if (y1 - y0) * (x1 - x0) > 0.85 * H * W:
        return None  # 裁不掉多少，不裁
    sx, sy = meta["src_w"] / W, meta["src_h"] / H
    return (int(x0 * sx), int(y0 * sy), int((x1 - x0) * sx), int((y1 - y0) * sy))
