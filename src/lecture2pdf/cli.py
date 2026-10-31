"""命令行入口：lecture2pdf <网址或本地视频> [选项]"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import time
from pathlib import Path

from . import keyframes as kfmod
from . import media, notes, render, transcript
from .config import Settings, default_output_root, endpoints_from_env, load_dotenv_file
from .llm import LLMClient, LLMError

logger = logging.getLogger("lecture2pdf")

STEPS = ["media", "transcript", "frames", "keyframes", "vision", "correct", "notes", "concise",
         "render"]
# 每一步的缓存（相对工作目录）。--force 某一步会同时清掉它之后所有步骤的缓存。
CACHES = {
    "media": ["media.json"],
    "transcript": ["transcript.json", "transcript_raw.md"],
    "frames": ["frames.json", "small.raw", "frames"],
    "keyframes": ["keyframes.json", "keyframes_params.json", "keyframes"],
    "vision": ["vision"],
    "correct": ["corrected", "transcript_corrected.md"],
    "notes": ["sections", "overview.md"],
    "concise": ["concise"],
    "render": [f"{stem}.{ext}" for stem in ("handout", "handout_full", "handout_concise")
               for ext in ("md", "pdf", "html", "tex")],
}

EPILOG = """
示例：
  # 本地视频，先只跑到关键帧（不需要任何 API Key），检查截图效果
  lecture2pdf lecture.mp4 --until keyframes

  # 完整流程（视觉模型用 VISION_* 环境变量配置，文本模型沿用 OPENAI_*）
  lecture2pdf "https://www.bilibili.com/video/BVxxxx" --terms "算子,谱测度,希尔伯特空间"

  # 调整关键帧参数后只重做关键帧及之后的步骤
  lecture2pdf lecture.mp4 --force keyframes --max-gap 300

  # 另外输出附板书原图的详细稿，便于核对
  lecture2pdf lecture.mp4 --full

环境变量（可写在项目根目录 .env）：
  VISION_API_KEY / VISION_BASE_URL / VISION_MODEL   视觉模型（必须支持图片输入）
  OPENAI_API_KEY / OPENAI_BASE_URL / TEXT_MODEL     文本模型（默认沿用项目已有配置）
"""


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="lecture2pdf", description="把课程视频（网址或本地文件）整理成 PDF 讲义",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source", help="视频网址或本地视频文件路径")
    p.add_argument("-o", "--work-dir", type=Path, help="工作目录（默认 output/<视频名>）")

    g = p.add_argument_group("流程控制")
    g.add_argument("--until", choices=STEPS, help="执行到该步骤为止")
    g.add_argument("--force", metavar="STEP", choices=STEPS + ["all"],
                   help="忽略该步骤及之后步骤的缓存，重新执行")
    g.add_argument("--clean", action="store_true", help="完成后删除抽帧和音频等中间文件（约数百 MB）")
    g.add_argument("-v", "--verbose", action="store_true")

    g = p.add_argument_group("下载")
    g.add_argument("--max-height", type=int, default=1080, help="下载清晰度上限（默认 1080）")
    g.add_argument("--cookies", help="cookies.txt 路径（B 站等平台下载高清需要登录）")
    g.add_argument("--cookies-from-browser", metavar="BROWSER", help="直接读取浏览器 cookies，如 chrome/firefox/edge")

    g = p.add_argument_group("转写")
    g.add_argument("--asr", choices=["funasr", "whisper"], default=None,
                   help="语音识别引擎：funasr（默认，中文专用，CPU 上快约 5 倍）/ whisper（非中文课程用这个）")
    g.add_argument("--whisper-model", default=None,
                   help="faster-whisper 模型：large-v3（默认，最准）/ large-v3-turbo（快 3~4 倍）/ medium / small，或本地模型目录")
    g.add_argument("--device", default="auto", help="auto / cpu / cuda")
    g.add_argument("--language", default="zh", help="授课语言（默认 zh；填 auto 自动检测）")
    g.add_argument("--terms", default="", help="课程术语，逗号分隔，用于提示语音识别")
    g.add_argument("--terms-file", type=Path, help="术语文件，每行一个")
    g.add_argument("--transcript", type=Path, help="直接使用已有文字稿（.srt/.vtt/.json/本项目 raw_*.md），跳过转写")

    g = p.add_argument_group("关键帧")
    d = Settings()
    g.add_argument("--interval", type=float, default=d.sample_interval, help=f"抽帧间隔秒数（默认 {d.sample_interval:g}）")
    g.add_argument("--min-age", type=float, default=d.min_age,
                   help=f"笔迹至少存在多少秒才算板书（默认 {d.min_age:g}；PPT 翻页快的课程可设 10~20）")
    g.add_argument("--sensitivity", type=float, default=d.lost_ratio,
                   help=f"网格块内笔迹消失比例阈值，越小越敏感（默认 {d.lost_ratio:g}）")
    g.add_argument("--min-gap", type=float, default=d.min_gap, help=f"关键帧最小间隔秒数（默认 {d.min_gap:g}）")
    g.add_argument("--max-gap", type=float, default=d.max_gap, help=f"最长多少秒必须有一张关键帧（默认 {d.max_gap:g}）")
    g.add_argument("--crop", help="手动裁剪区域 x,y,w,h（原视频像素坐标），例如只保留黑板")
    g.add_argument("--no-auto-crop", action="store_true", help="关闭自动裁剪")
    g.add_argument("--no-enhance", action="store_true", help="关闭光照均衡/锐化")

    g = p.add_argument_group("模型")
    g.add_argument("--vision-model", help="视觉模型名（覆盖 VISION_MODEL）")
    g.add_argument("--vision-base-url", help="视觉模型接口地址（覆盖 VISION_BASE_URL）")
    g.add_argument("--text-model", help="文本模型名（覆盖 TEXT_MODEL / OPENAI_MODEL）")
    g.add_argument("--no-vision", action="store_true", help="不识别画面，仅根据语音生成讲义")
    g.add_argument("--vision-max-side", type=int, default=d.vision_max_side,
                   help=f"上传前图片长边上限（默认 {d.vision_max_side}；网络差时可设 1280 或 1024）")
    g.add_argument("--vision-quality", type=int, default=d.vision_quality, help=f"上传 JPEG 质量（默认 {d.vision_quality}）")
    g.add_argument("--concurrency", type=int, default=d.concurrency, help=f"并发请求数（默认 {d.concurrency}）")
    g.add_argument("--note-language", default="zh", help="讲义语言（默认 zh）")

    g = p.add_argument_group("输出")
    g.add_argument("--no-concise", action="store_true",
                   help="不按主题归并精简，handout.pdf 直接由各版面小节组成（更详细、篇幅更长）")
    g.add_argument("--full", action="store_true",
                   help="另外生成详细稿 handout_full.pdf：按时间顺序的全部小节，附板书原图，便于核对")
    g.add_argument("--appendix-transcript", action="store_true",
                   help="在详细稿末尾附上校对后的完整文字稿（隐含 --full）")
    g.add_argument("--no-images", action="store_true", help="详细稿中不附板书原图")
    g.add_argument("--pdf-engine", default="xelatex", choices=["xelatex", "lualatex", "typst"])
    g.add_argument("--cjk-font", default=d.cjk_font,
                   help="中文字体（默认使用 TeX Live 自带的 Fandol 字体），如 \"Noto Serif CJK SC\"、\"Songti SC\"")
    g.add_argument("--main-font", default=d.main_font, help="西文字体（默认 Latin Modern）")
    return p.parse_args(argv)


def build_settings(a: argparse.Namespace) -> Settings:
    import os

    s = Settings()
    s.max_height, s.cookies, s.cookies_from_browser = a.max_height, a.cookies, a.cookies_from_browser
    s.whisper_model = a.whisper_model or os.getenv("LECTURE_WHISPER_MODEL") or "large-v3"
    s.asr = a.asr or os.getenv("LECTURE_ASR") or "funasr"
    if s.asr == "funasr" and a.language not in ("zh", "auto"):
        s.asr = "whisper"  # FunASR 中文模型只适合中文课程
    s.whisper_device = a.device
    s.language = None if a.language == "auto" else a.language
    terms = [t for t in re.split(r"[,，、;；\n]+", a.terms) if t.strip()]
    if a.terms_file:
        terms += [ln.strip() for ln in a.terms_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    s.terms = [t.strip() for t in terms]
    s.transcript_file = a.transcript
    s.sample_interval, s.min_age, s.lost_ratio = a.interval, a.min_age, a.sensitivity
    s.min_gap, s.max_gap = a.min_gap, a.max_gap
    if a.crop:
        s.crop = tuple(int(x) for x in a.crop.split(","))  # type: ignore[assignment]
        if len(s.crop) != 4:
            raise SystemExit("--crop 需要 x,y,w,h 四个整数")
    s.auto_crop, s.enhance = not a.no_auto_crop, not a.no_enhance
    s.vision, s.text = endpoints_from_env()
    if a.vision_model:
        s.vision.model = a.vision_model
        if not s.vision.api_key:
            s.vision.api_key, s.vision.base_url = s.text.api_key, s.text.base_url
    if a.vision_base_url:
        s.vision.base_url = a.vision_base_url
    if a.text_model:
        s.text.model = a.text_model
    s.vision_max_side, s.vision_quality = a.vision_max_side, a.vision_quality
    s.concurrency, s.note_language = max(1, a.concurrency), a.note_language
    s.appendix_transcript, s.with_images = a.appendix_transcript, not a.no_images
    s.full = a.full or a.appendix_transcript
    s.pdf_engine, s.cjk_font, s.main_font = a.pdf_engine, a.cjk_font, a.main_font
    return s


def invalidate(work: Path, step: str) -> None:
    start = 0 if step == "all" else STEPS.index(step)
    for st in STEPS[start:]:
        # media 只删 media.json（重新探测），已下载的视频本身保留，yt-dlp 会自动复用
        for rel in CACHES[st]:
            p = work / rel
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()


def setup_logging(work: Path, verbose: bool) -> None:
    fmt = logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(work / "run.log", encoding="utf-8")
    fh.setFormatter(fmt)
    quiet = ("httpx", "httpcore", "openai", "urllib3", "faster_whisper")
    for h in (sh, fh):  # 第三方库可能在导入时重置级别，直接在 handler 上过滤
        h.addFilter(lambda r: verbose or r.levelno >= logging.WARNING
                    or not r.name.startswith(quiet))
    root.handlers[:] = [sh, fh]
    for noisy in ("httpx", "openai", "httpcore", "faster_whisper", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _segments_to_md(segs) -> str:
    lines = ["# 原始转写（语音识别）", ""]
    for s in segs:
        lines.append(f"[{transcript.fmt_time(s['start'])}–{transcript.fmt_time(s['end'])}] {s['text']}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    load_dotenv_file()
    a = parse_args(argv)
    s = build_settings(a)
    media.check_tools()

    work = a.work_dir or media.default_work_dir(a.source, default_output_root(),
                                                s.cookies, s.cookies_from_browser)
    work = work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    setup_logging(work, a.verbose)
    logger.info(f"工作目录: {work}")
    if a.force:
        invalidate(work, a.force)
    t_start = time.time()

    def stop_after(step: str) -> bool:
        if a.until == step:
            logger.info(f"已按 --until {step} 停止。产物位于 {work}")
            return True
        return False

    # 1. 视频
    info = media.acquire(a.source, work, s.max_height, s.cookies, s.cookies_from_browser)
    logger.info(f"视频: {info.title}  {info.width}x{info.height}  {info.duration / 60:.1f} 分钟")
    if stop_after("media"):
        return 0

    # 2. 文字稿
    tpath = work / "transcript.json"
    if tpath.exists():
        segs = json.loads(tpath.read_text(encoding="utf-8"))
        logger.info(f"复用已有文字稿（{len(segs)} 段）")
    else:
        if s.transcript_file:
            segs = transcript.import_transcript(s.transcript_file)
            logger.info(f"导入文字稿 {s.transcript_file}（{len(segs)} 段）")
        else:
            audio = transcript.extract_audio(Path(info.video_path), work / "audio.wav")
            if s.asr == "funasr":
                segs = transcript.funasr_transcribe(audio, s.terms, s.whisper_device)
            else:
                segs = transcript.whisper_transcribe(audio, s.whisper_model, s.language, s.terms,
                                                     s.whisper_device, s.whisper_compute_type,
                                                     info.duration)
        tpath.write_text(json.dumps(segs, ensure_ascii=False, indent=1), encoding="utf-8")
        (work / "transcript_raw.md").write_text(_segments_to_md(segs), encoding="utf-8")
    if not segs:
        logger.warning("文字稿为空（视频可能没有语音），讲义将只依据画面整理")
    if stop_after("transcript"):
        return 0

    # 3. 抽帧
    fmeta_path = work / "frames.json"
    if fmeta_path.exists():
        fm = json.loads(fmeta_path.read_text())
        if fm.get("interval") != s.sample_interval or fm.get("width") != s.analysis_width:
            logger.info("抽帧参数变化，重新抽帧")
            invalidate(work, "frames")
    params = json.loads(json.dumps(s.keyframe_params()))
    ppath = work / "keyframes_params.json"
    kf_cached = (work / "keyframes.json").exists() and ppath.exists() \
        and json.loads(ppath.read_text()) == params

    def frames() -> dict:  # 关键帧有效缓存时无需抽帧（例如 --clean 之后）
        return kfmod.sample_frames(Path(info.video_path), work, s.sample_interval,
                                   s.analysis_width, info.width, info.height)

    if a.until == "frames" or not kf_cached:
        fmeta = frames()
    if stop_after("frames"):
        return 0

    # 4. 关键帧
    if kf_cached:
        kfs = kfmod.load_keyframes(work)
        logger.info(f"复用已有关键帧（{len(kfs)} 张）")
    else:
        if (work / "keyframes.json").exists():
            logger.info("关键帧参数变化，重新检测（并清除之后步骤的缓存）")
            invalidate(work, "keyframes")
        kfs = kfmod.detect_keyframes(work, fmeta, info.duration, s)
        ppath.write_text(json.dumps(params))
    logger.info("关键帧: " + " ".join(transcript.fmt_time(k.time) for k in kfs))
    if stop_after("keyframes"):
        logger.info(f"关键帧图片在 {work / 'keyframes'}，可先检查截图质量再继续")
        return 0

    # 5~7. 模型
    try:
        text_client = LLMClient(s.text, "文本模型")
    except LLMError as e:
        logger.error(f"{e}。请在 .env 中配置 OPENAI_API_KEY / OPENAI_BASE_URL（或 TEXT_*）")
        return 2
    vision_client = None
    if not a.no_vision:
        try:
            vision_client = LLMClient(s.vision, "视觉模型")
        except LLMError as e:
            cached = (work / "vision").exists() and any((work / "vision").iterdir())
            if not cached:
                logger.warning(f"{e}：将仅根据语音整理讲义。"
                               f"配置 VISION_API_KEY / VISION_BASE_URL / VISION_MODEL 后用 --force vision 重跑即可补上画面识别")

    title = info.title
    logger.info(f"识别板书（{len(kfs)} 张）…")
    boards = notes.run_vision(work, kfs, segs, title, vision_client, s)
    if stop_after("vision"):
        return 0

    logger.info("校对文字稿…")
    corrected = notes.run_correct(work, kfs, segs, title, boards, text_client, s)
    (work / "transcript_corrected.md").write_text(
        "\n\n".join(f"## {transcript.fmt_time(k.seg_start)}–{transcript.fmt_time(k.seg_end)}\n\n"
                    f"{corrected.get(k.index, '')}" for k in kfs) + "\n", encoding="utf-8")
    if stop_after("correct"):
        return 0

    logger.info("整理讲义…")
    sections = notes.run_notes(work, kfs, title, boards, corrected, text_client, s)
    doc_title, overview = notes.run_overview(work, title, sections, text_client, s)
    if stop_after("notes"):
        return 0

    if a.no_concise:
        handout = (doc_title, "", [sections[k] for k in sorted(sections)])
    else:
        logger.info("按主题归并、精简讲义…")
        handout = notes.run_concise(work, title, sections, text_client, s)
    if stop_after("concise"):
        return 0

    # 8. 渲染
    md_path = render.build_handout(work, doc_title=handout[0], video_title=info.title,
                                   intro=handout[1], chapters=handout[2],
                                   cjk_font=s.cjk_font, main_font=s.main_font)
    leftover = notes.find_meta(md_path.read_text(encoding="utf-8"))
    if leftover:
        logger.warning(f"讲义中仍有 {len(leftover)} 处描述整理过程的措辞，可在 concise/ 中手工修改后 "
                       "--force render：\n  " + "\n  ".join(x[:80] for x in leftover[:10]))
    pdf = render.render_pdf(work, md_path, s.pdf_engine)
    full_pdf = None
    if s.full:
        fmd = render.build_full(work, doc_title, info.title, info.source, overview, kfs, sections,
                                corrected, s.with_images, s.appendix_transcript, s.cjk_font,
                                s.main_font)
        full_pdf = render.render_pdf(work, fmd, s.pdf_engine)

    for c in (vision_client, text_client):
        if c and c.calls:
            logger.info(c.usage_line())
    if a.clean:
        for rel in ("frames", "small.raw", "audio.wav"):
            p = work / rel
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        (work / "frames.json").unlink(missing_ok=True)
    logger.info(f"总耗时 {(time.time() - t_start) / 60:.1f} 分钟")
    print(json.dumps({
        "pdf": str(pdf) if pdf else None,
        "pdf_full": str(full_pdf) if full_pdf else None,
        "markdown": str(md_path),
        "html": str(work / "handout.html") if (work / "handout.html").exists() else None,
        "work_dir": str(work),
    }, ensure_ascii=False, indent=2))
    if not pdf:
        logger.error("PDF 生成失败；Markdown/HTML 已生成。查看 render_*.log 定位出错的公式，"
                     "在 concise/ch_XX.md（或 sections/sec_XX.md）中修正后运行 --force render")
        return 1
    return 0
