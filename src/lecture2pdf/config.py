"""运行配置：命令行参数 > 环境变量 / .env > 默认值。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def find_env_file() -> Path | None:
    """依次查找：$LECTURE2PDF_ENV → 当前目录及其上级目录的 .env → ~/.config/lecture2pdf/.env。"""
    explicit = os.getenv("LECTURE2PDF_ENV")
    if explicit:
        return Path(explicit).expanduser()
    cwd = Path.cwd().resolve()
    for d in (cwd, *cwd.parents):
        if (d / ".env").is_file():
            return d / ".env"
    user = Path.home() / ".config" / "lecture2pdf" / ".env"
    return user if user.is_file() else None


def default_output_root() -> Path:
    return Path(os.getenv("LECTURE2PDF_OUTPUT") or Path.cwd() / "output")


def load_dotenv_file() -> Path | None:
    """加载 .env（不覆盖已存在的环境变量），返回实际使用的文件路径。"""
    env_path = find_env_file()
    if not env_path or not env_path.exists():
        return None
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
        return env_path
    except ImportError:
        pass
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env_path


@dataclass
class LLMEndpoint:
    api_key: str | None
    base_url: str | None
    model: str

    @property
    def available(self) -> bool:
        return bool(self.api_key)


@dataclass
class Settings:
    # ---- 输入/输出 ----
    work_dir: Path = Path("output")
    max_height: int = 1080
    cookies: str | None = None              # cookies.txt 路径
    cookies_from_browser: str | None = None  # 如 "chrome"、"firefox"

    # ---- 转写 ----
    asr: str = "funasr"            # funasr（中文，快）/ whisper（多语言）
    whisper_model: str = "large-v3"
    whisper_device: str = "auto"
    whisper_compute_type: str = "auto"
    language: str = "zh"
    terms: list[str] = field(default_factory=list)  # 课程术语，作为转写提示
    transcript_file: Path | None = None    # 直接导入已有文字稿（srt/vtt/json/本项目 md）

    # ---- 抽帧/关键帧 ----
    sample_interval: float = 2.0     # 抽帧间隔（秒）
    window: int = 5                  # 多少帧取一次时间中位数（去掉走动的老师）
    analysis_width: int = 480        # 分析用的灰度小图宽度
    min_age: float = 40.0            # 笔迹存在超过这么久（秒）才算「板书」，过滤老师身上的边缘
    lost_ratio: float = 0.6          # 某网格块内老笔迹持续消失的比例超过它 → 擦板/换页/机位变化
    min_gap: float = 45.0            # 相邻关键帧最小间隔（秒）
    min_lost_area: float = 0.001     # 擦除面积下限（占画面比例），过滤零星噪声
    max_gap: float = 600.0           # 超过这么久没有关键帧就补一张兜底
    redundant_cover: float = 0.92    # 前一张被后一张覆盖到这个比例就视为冗余
    crop: tuple[int, int, int, int] | None = None  # 手动裁剪 x,y,w,h（原分辨率像素）
    auto_crop: bool = True
    enhance: bool = True

    # ---- 模型 ----
    vision: LLMEndpoint = field(default_factory=lambda: LLMEndpoint(None, None, ""))
    text: LLMEndpoint = field(default_factory=lambda: LLMEndpoint(None, None, ""))
    concurrency: int = 4
    note_language: str = "zh"
    vision_max_side: int = 1600      # 发给视觉模型前把长边缩到不超过它（像素）
    vision_quality: int = 85         # JPEG 质量；网络差、上传超时（408）时可调低这两项
    max_transcript_chars: int = 6000     # 单次校正请求的文字稿上限，超出则分片
    max_notes_input_chars: int = 20000   # 单节讲义的文字稿上限
    full: bool = False                   # 另外生成附板书原图的详细稿
    appendix_transcript: bool = False
    with_images: bool = True

    # ---- 渲染 ----
    pdf_engine: str = "xelatex"
    cjk_font: str = ""               # 空 = ctex 的 Fandol 字体（TeX Live 自带）
    main_font: str = ""              # 空 = Latin Modern

    def keyframe_params(self) -> dict:
        return {k: getattr(self, k) for k in (
            "sample_interval", "window", "analysis_width", "min_age", "lost_ratio",
            "min_gap", "min_lost_area", "max_gap", "redundant_cover", "crop",
            "auto_crop", "enhance")}


def endpoints_from_env() -> tuple[LLMEndpoint, LLMEndpoint]:
    """
    文本模型沿用项目已有的 OPENAI_* 配置；
    视觉模型单独用 VISION_*，未设置时回退到 OPENAI_*（前提是该端点本身支持图片）。
    """
    text = LLMEndpoint(
        api_key=os.getenv("TEXT_API_KEY") or os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("TEXT_BASE_URL") or os.getenv("OPENAI_BASE_URL"),
        model=os.getenv("TEXT_MODEL") or os.getenv("OPENAI_MODEL") or _default_text_model(),
    )
    vision = LLMEndpoint(
        api_key=os.getenv("VISION_API_KEY") or None,
        base_url=os.getenv("VISION_BASE_URL") or None,
        model=os.getenv("VISION_MODEL") or "",
    )
    if not vision.api_key and os.getenv("VISION_MODEL"):
        # 只配了模型名：认为与文本模型同一个端点
        vision.api_key, vision.base_url = text.api_key, text.base_url
    return vision, text


def _default_text_model() -> str:
    base = (os.getenv("TEXT_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "").lower()
    if "deepseek" in base:
        return "deepseek-chat"
    if "dashscope" in base:
        return "qwen-plus"
    return "gpt-4o-mini"
