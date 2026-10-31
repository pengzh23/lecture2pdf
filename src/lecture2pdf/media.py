"""获取视频：网址用 yt-dlp 下载（保留画面，默认 ≤1080p），本地文件直接使用。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi", ".m4v", ".ts")


@dataclass
class MediaInfo:
    video_path: str
    title: str
    duration: float
    width: int
    height: int
    source: str

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> MediaInfo:
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def safe_name(title: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^\w\-]+", "_", title, flags=re.UNICODE).strip("_")
    return (s[:maxlen] or "untitled")


def check_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise RuntimeError(f"未找到 {tool}，请先安装 ffmpeg")


def probe(path: Path) -> tuple[float, int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"{path} 中没有视频流（讲义需要画面；纯音频请改用 transcribe.py）")
    duration = float(data.get("format", {}).get("duration") or 0)
    return duration, int(streams[0]["width"]), int(streams[0]["height"])


def _ydl_base_opts(cookies: str | None, cookies_from_browser: str | None) -> dict:
    opts: dict = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if cookies:
        opts["cookiefile"] = cookies
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    return opts


def default_work_dir(source: str, root: Path, cookies=None, cookies_from_browser=None) -> Path:
    """未指定工作目录时：本地文件用文件名，网址用「标题_网址哈希」。"""
    if not is_url(source):
        return root / safe_name(Path(source).stem)
    h = hashlib.sha1(source.encode()).hexdigest()[:6]
    title = "video"
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(_ydl_base_opts(cookies, cookies_from_browser)) as ydl:
            info = ydl.extract_info(source, download=False)
        title = info.get("title") or title
    except Exception as e:  # noqa: BLE001
        logger.warning(f"获取视频标题失败，使用默认目录名: {e}")
    return root / f"{safe_name(title, 40)}_{h}"


def acquire(source: str, work_dir: Path, max_height: int = 1080,
            cookies: str | None = None, cookies_from_browser: str | None = None) -> MediaInfo:
    meta_path = work_dir / "media.json"
    if meta_path.exists():
        info = MediaInfo.load(meta_path)
        if Path(info.video_path).exists():
            return info

    if is_url(source):
        video_path, title = _download(source, work_dir, max_height, cookies, cookies_from_browser)
    else:
        video_path = Path(source).expanduser().resolve()
        if not video_path.exists():
            raise RuntimeError(f"文件不存在: {video_path}")
        title = video_path.stem

    duration, w, h = probe(video_path)
    if h < 720:
        logger.warning(f"视频分辨率只有 {w}x{h}，板书文字可能难以辨认")
    info = MediaInfo(str(video_path), title, duration, w, h, source)
    info.save(meta_path)
    return info


def _download(url: str, work_dir: Path, max_height: int,
              cookies: str | None, cookies_from_browser: str | None) -> tuple[Path, str]:
    import yt_dlp

    fmt = (f"bestvideo[height<=?{max_height}]+bestaudio/"
           f"best[height<=?{max_height}]/best")
    opts = _ydl_base_opts(cookies, cookies_from_browser)
    opts.update({
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": str(work_dir / "video.%(ext)s"),
        "quiet": False,
        "noprogress": False,
    })
    logger.info(f"下载视频（≤{max_height}p）: {url}")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    title = (info or {}).get("title") or "untitled"
    height = (info or {}).get("height")
    if height and height < max_height:
        logger.warning(
            f"实际下载到 {height}p（请求 ≤{max_height}p）。B 站等平台的高清通常需要登录，"
            f"可用 --cookies-from-browser chrome 或 --cookies cookies.txt"
        )
    for ext in VIDEO_EXT:
        p = work_dir / f"video{ext}"
        if p.exists():
            return p, title
    raise RuntimeError("视频下载完成但未找到输出文件")
