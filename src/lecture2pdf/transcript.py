"""
转写：抽音轨 → faster-whisper（带时间戳）→ 繁转简。也支持导入已有文字稿。

产物 transcript.json: [{"start": 秒, "end": 秒, "text": "..."}]
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

Segment = dict  # {"start": float, "end": float, "text": str}


def extract_audio(video: Path, out: Path) -> Path:
    if out.exists() and out.stat().st_size > 0:
        return out
    tmp = out.with_suffix(".tmp.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-v", "error", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp)],
        check=True,
    )
    tmp.rename(out)
    return out


def _t2s():
    try:
        import opencc

        return opencc.OpenCC("t2s").convert
    except ImportError:
        logger.warning("未安装 opencc，跳过繁转简（pip install opencc-python-reimplemented）")
        return lambda s: s


def _resolve_device(device: str, compute_type: str) -> tuple[str, str]:
    if device == "auto":
        try:
            import ctranslate2

            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:  # noqa: BLE001
            device = "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    return device, compute_type


_MS_REPOS = {  # ModelScope 上 Systran 官方模型的镜像（国内网络访问 Hugging Face 常超时）
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
}
_MS_FILES = ("config.json", "preprocessor_config.json", "tokenizer.json",
             "vocabulary.json", "model.bin")


def _download_from_modelscope(model_size: str) -> str | None:
    """从 ModelScope 下载 CTranslate2 格式的 Whisper 模型（支持断点续传），返回本地目录。"""
    import urllib.request

    repo = _MS_REPOS.get(model_size)
    if not repo:
        return None
    dst = models_root() / repo.replace("/", "--")
    dst.mkdir(parents=True, exist_ok=True)
    for name in _MS_FILES:
        target = dst / name
        if target.exists():
            continue
        part = dst / (name + ".part")
        url = f"https://www.modelscope.cn/models/{repo}/resolve/master/{name}"
        done = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(url, headers={"Range": f"bytes={done}-"} if done else {})
        logger.info(f"从 ModelScope 下载 {repo}/{name} …")
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0) + done
            mode = "ab" if done and r.status == 206 else "wb"
            if mode == "wb":
                done = 0
            with open(part, mode) as f:
                last = time.time()
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if total > 50 << 20 and time.time() - last > 15:
                        last = time.time()
                        logger.info(f"  {name}: {done / total:5.1%} of {total / 2**30:.2f} GB")
        part.rename(target)
    return str(dst)


def _hf_reachable(url: str) -> bool:
    import os
    import urllib.request

    if os.getenv("HF_HUB_OFFLINE") == "1":
        return True  # 离线模式下交给 faster-whisper 自己读本地缓存
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


def _load_whisper(model_size: str, device: str, compute_type: str):
    from faster_whisper import WhisperModel

    if Path(model_size).is_dir():
        return WhisperModel(model_size, device=device, compute_type=compute_type, cpu_threads=_threads())
    local = models_root()
    repo = _MS_REPOS.get(model_size)
    if repo and (local / repo.replace("/", "--") / "model.bin").exists():
        return WhisperModel(str(local / repo.replace("/", "--")), device=device,
                            compute_type=compute_type, cpu_threads=_threads())
    try:
        if repo and not _hf_reachable(f"https://huggingface.co/{repo}/resolve/main/config.json"):
            raise RuntimeError("Hugging Face 5 秒内无响应")
        return WhisperModel(model_size, device=device, compute_type=compute_type, cpu_threads=_threads())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"从 Hugging Face 加载模型失败（{str(e)[:120]}），改用 ModelScope 镜像")
        path = _download_from_modelscope(model_size)
        if not path:
            raise
        return WhisperModel(path, device=device, compute_type=compute_type, cpu_threads=_threads())


def whisper_transcribe(audio: Path, model_size: str, language: str | None,
                       terms: list[str], device: str = "auto",
                       compute_type: str = "auto", duration: float = 0) -> list[Segment]:
    device, compute_type = _resolve_device(device, compute_type)
    logger.info(f"加载 Whisper 模型 {model_size}（{device}/{compute_type}），首次使用会自动下载")
    model = _load_whisper(model_size, device, compute_type)

    # hotwords 在每个 30s 窗口都会作为提示注入（initial_prompt 在
    # condition_on_previous_text=False 时只作用于第一个窗口），
    # 用它同时约束「简体中文 + 标点」和课程术语。
    hint = ""
    if language and language.startswith("zh"):
        hint = "以下是普通话的课堂录音，使用简体中文。"
    if terms:
        hint += "本课术语：" + "、".join(terms) + "。"

    segments, info = model.transcribe(
        str(audio),
        language=language or None,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 700, "speech_pad_ms": 300},
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        compression_ratio_threshold=2.4,
        hotwords=hint or None,
    )
    conv = _t2s() if (info.language or "").startswith("zh") else (lambda s: s)
    out: list[Segment] = []
    t0, last_log = time.time(), 0.0
    for s in segments:
        text = conv(s.text.strip())
        if text:
            out.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text})
        if duration and time.time() - last_log > 30:
            last_log = time.time()
            el = time.time() - t0
            pct = s.end / duration
            eta = el / pct - el if pct > 0 else 0
            logger.info(f"  转写进度 {pct:5.1%}（已用 {el/60:.1f} 分钟，预计还需 {eta/60:.1f} 分钟）")
    logger.info(f"转写完成：{len(out)} 段，语言 {info.language}（{info.language_probability:.2f}）")
    return out


# ---------------------------------------------------------------- 导入已有文字稿

def _ts(s: str) -> float:
    s = s.strip().replace(",", ".")
    parts = [float(p) for p in s.split(":")]
    v = 0.0
    for p in parts:
        v = v * 60 + p
    return v


def import_transcript(path: Path) -> list[Segment]:
    """支持 .json（本工具格式）、.srt、.vtt、以及本项目 transcribe.py 的 raw_*.md。"""
    text = path.read_text(encoding="utf-8")
    suf = path.suffix.lower()
    conv = _t2s()
    if suf == ".json":
        segs = json.loads(text)
        return [{"start": float(s["start"]), "end": float(s["end"]), "text": conv(s["text"])}
                for s in segs]

    out: list[Segment] = []
    if suf in (".srt", ".vtt"):
        time_re = re.compile(r"([\d:.,]+)\s*-->\s*([\d:.,]+)")
        for block in re.split(r"\n\s*\n", text):
            lines = [ln for ln in block.strip().splitlines() if ln.strip()]
            for i, ln in enumerate(lines):
                m = time_re.search(ln)
                if m:
                    body = re.sub(r"<[^>]+>", "", " ".join(lines[i + 1:])).strip()
                    if body:
                        out.append({"start": _ts(m.group(1)), "end": _ts(m.group(2)),
                                    "text": conv(body)})
                    break
        return out

    # Markdown：**[mm:ss - mm:ss]** 后接正文
    pat = re.compile(r"\*\*\[([\d:]+)\s*-\s*([\d:]+)\]\*\*\s*\n+(.*?)(?=\n\s*\*\*\[|\Z)", re.S)
    for m in pat.finditer(text):
        body = m.group(3).strip()
        if body:
            out.append({"start": _ts(m.group(1)), "end": _ts(m.group(2)), "text": conv(body)})
    if not out:
        raise RuntimeError(f"无法从 {path} 解析出带时间戳的文字稿")
    return out


def slice_text(segs: list[Segment], start: float, end: float) -> str:
    """取 [start, end) 区间内的文字稿，每 ~1 分钟插入一个时间标记方便模型对照。"""
    lines, last_mark = [], -1e9
    for s in segs:
        mid = (s["start"] + s["end"]) / 2
        if start <= mid < end:
            if s["start"] - last_mark >= 60:
                lines.append(f"\n[{fmt_time(s['start'])}] ")
                last_mark = s["start"]
            lines.append(s["text"] + " ")
    return "".join(lines).strip()


def fmt_time(t: float) -> str:
    t = int(t)
    h, m, s = t // 3600, t % 3600 // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _threads() -> int:
    """CPU 推理线程数：默认取物理核心数（超线程开满反而更慢），可用 LECTURE_WHISPER_THREADS 覆盖。"""
    env = os.getenv("LECTURE_WHISPER_THREADS")
    if env and env.isdigit():
        return int(env)
    return max(1, (os.cpu_count() or 8) // 2)


def models_root() -> Path:
    """所有本地模型的存放目录；可用 LECTURE2PDF_MODEL_DIR 指到其他磁盘。"""
    env = os.getenv("LECTURE2PDF_MODEL_DIR")
    return Path(env).expanduser() if env else Path.home() / ".cache" / "lecture2pdf" / "models"


def funasr_transcribe(audio: Path, terms: list[str], device: str = "auto") -> list[Segment]:
    """FunASR：SeACo-Paraformer（中文，支持热词）+ FSMN-VAD + CT-Transformer 标点。"""
    os.environ.setdefault("MODELSCOPE_CACHE", str(models_root() / "modelscope"))
    try:
        from funasr import AutoModel
    except ImportError as e:
        raise RuntimeError("未安装 FunASR：pip install funasr modelscope torch torchaudio "
                           "（CPU 版 torch 可加 --index-url https://download.pytorch.org/whl/cpu）") from e
    if device == "auto":
        try:
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"
    logger.info(f"加载 FunASR 模型（{device}），首次使用会下载约 2.2 GB 到 {os.environ['MODELSCOPE_CACHE']}")
    model = AutoModel(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc",
                      hub="ms", disable_update=True, device=device, log_level="ERROR",
                      ncpu=_threads())
    logger.info("FunASR 转写中（整段处理，CPU 上约为视频时长的 1/4~1/8）…")
    t0 = time.time()
    res = model.generate(input=str(audio), batch_size_s=300, sentence_timestamp=True,
                         hotword=" ".join(terms) if terms else None)
    sents = res[0].get("sentence_info") or []
    segs = _merge_sentences([(s["start"] / 1000, s["end"] / 1000, s["text"]) for s in sents])
    logger.info(f"转写完成：{len(segs)} 段，用时 {(time.time() - t0) / 60:.1f} 分钟")
    return segs


def _merge_sentences(items: list[tuple[float, float, str]], max_len: float = 25.0) -> list[Segment]:
    """FunASR 按逗号切得很碎，合并到句末标点或约 25 秒一段。"""
    out: list[Segment] = []
    cur: Segment | None = None
    for start, end, text in items:
        text = text.strip()
        if not text:
            continue
        if cur is None:
            cur = {"start": start, "end": end, "text": text}
        else:
            cur["end"], cur["text"] = end, cur["text"] + text
        if cur["text"][-1] in "。？！?!" or cur["end"] - cur["start"] >= max_len:
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return out
