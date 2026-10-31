"""环境自检：lecture2pdf-doctor（或 python -m lecture2pdf.doctor）"""

from __future__ import annotations

import shutil
import subprocess
import sys

OK, WARN, FAIL = "✅", "⚠️ ", "❌"


def _font_ok(name: str) -> bool:
    if not shutil.which("fc-list"):
        return True
    out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
    return name.lower() in out.lower()


def _tex_pkg(name: str) -> bool:
    if not shutil.which("kpsewhich"):
        return False
    return bool(subprocess.run(["kpsewhich", name], capture_output=True, text=True).stdout.strip())


def main() -> int:
    from .config import Settings, endpoints_from_env, load_dotenv_file
    from .render import pandoc_bin

    env = load_dotenv_file()
    rows: list[tuple[str, str, str]] = []

    def add(ok, item, hint=""):
        rows.append((ok, item, hint))

    add(OK if env else WARN, f".env 配置文件: {env or '未找到'}",
        "" if env else "复制 .env.example 为 .env 并填写")
    for tool in ("ffmpeg", "ffprobe"):
        add(OK if shutil.which(tool) else FAIL, tool, "sudo apt install ffmpeg")
    add(OK if pandoc_bin() else FAIL, "pandoc", "pip install pypandoc_binary")
    add(OK if shutil.which("xelatex") else FAIL, "xelatex",
        "sudo apt install texlive-xetex texlive-lang-chinese")
    for pkg, hint in (("ctex.sty", "sudo apt install texlive-lang-chinese"),
                      ("tcolorbox.sty", "sudo apt install texlive-latex-extra"),
                      ("fancyhdr.sty", "sudo apt install texlive-latex-extra")):
        add(OK if _tex_pkg(pkg) else FAIL, f"LaTeX 宏包 {pkg[:-4]}", hint)
    s = Settings()
    if s.cjk_font:
        add(OK if _font_ok(s.cjk_font) else FAIL, f"中文字体 {s.cjk_font}",
            "安装该字体，或用 --cjk-font 指定已安装的字体")
    else:
        add(OK if _tex_pkg("FandolSong-Bold.otf") else FAIL, "中文字体 Fandol（TeX Live 自带）",
            "sudo apt install texlive-lang-chinese，或用 --cjk-font 指定已安装的字体")
    for mod, pip in (("numpy", "numpy"), ("PIL", "pillow"), ("faster_whisper", "faster-whisper"),
                     ("yt_dlp", "yt-dlp"), ("openai", "openai"), ("opencc", "opencc-python-reimplemented")):
        try:
            __import__(mod)
            add(OK, f"Python 包 {pip}")
        except ImportError:
            add(FAIL, f"Python 包 {pip}", f"pip install {pip}")

    vision, text = endpoints_from_env()
    add(OK if text.available else FAIL, f"文本模型 {text.model} @ {text.base_url or '默认'}",
        "" if text.available else "在 .env 设置 OPENAI_API_KEY / OPENAI_BASE_URL（或 TEXT_*）")
    add(OK if vision.available else WARN, f"视觉模型 {vision.model or '(未设置)'}",
        "" if vision.available else "在 .env 设置 VISION_API_KEY / VISION_BASE_URL / VISION_MODEL；"
        "不设置也能运行，但不会识别板书")
    import os

    from .transcript import models_root

    root = models_root()
    asr = os.getenv("LECTURE_ASR") or "funasr"
    add(OK, f"语音识别引擎: {asr}；模型目录: {root}")
    if asr == "funasr":
        for mod, pip in (("funasr", "funasr"), ("modelscope", "modelscope"), ("torch", "torch")):
            try:
                __import__(mod)
                add(OK, f"Python 包 {pip}")
            except ImportError:
                add(FAIL, f"Python 包 {pip}", "pip install funasr modelscope torch torchaudio")
        have = list((root / "modelscope" / "models").glob("iic--speech_seaco_paraformer*"))
        add(OK if have else WARN, f"FunASR 模型: {'已下载' if have else '未下载'}",
            "" if have else "首次转写会自动下载（约 2.2 GB）")
    else:
        have = sorted(p.name for p in root.glob("*") if (p / "model.bin").exists())
        add(OK if have else WARN, f"Whisper 模型: {', '.join(have) or '无'}",
            "" if have else "首次转写会自动下载 large-v3（约 3 GB）")

    for ok, item, hint in rows:
        print(f"{ok} {item}" + (f"\n     → {hint}" if hint and ok != OK else ""))
    return 1 if any(r[0] == FAIL for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
