"""ComfyUI-PostFX 节点实现。

全部为容器级操作，不把视频拆成帧序列：
  - AudioCut / AudioJoin 直接操作 AUDIO 的 waveform tensor
  - VideoBGM 通过 VIDEO.get_stream_source() 拿文件路径后交给 ffmpeg，-c:v copy
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any

import folder_paths
import torch

try:
    from comfy_api.latest import io
except ImportError:  # 旧版 ComfyUI 无 V3 API 时的兜底
    io = None

logger = logging.getLogger("PostFX")


# --------------------------------------------------------------------------- 工具

_FFMPEG: str | None = None


def ffmpeg_path() -> str:
    """定位 ffmpeg：PATH 优先，其次 Windows 常见安装位置。"""
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    found = shutil.which("ffmpeg")
    if found:
        _FFMPEG = found
        return found
    for cand in (
        r"C:\ffmpeg\ffmpeg_full_build\ffmpeg_full_build\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ):
        if os.path.isfile(cand):
            _FFMPEG = cand
            return cand
    raise RuntimeError(
        "找不到 ffmpeg。请把 ffmpeg 加入 PATH，或安装到 C:/ffmpeg/bin/。"
    )


def run_ffmpeg(args: list[str], timeout: int = 600) -> tuple[int, str]:
    """执行 ffmpeg，返回 (返回码, 合并后的输出)。"""
    cmd = [ffmpeg_path(), "-hide_banner", "-y", *args]
    logger.info("ffmpeg: %s", " ".join(cmd[1:]))
    proc = subprocess.run(
        cmd, capture_output=True, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = (proc.stdout or b"").decode("utf-8", "replace") + (proc.stderr or b"").decode("utf-8", "replace")
    return proc.returncode, out


def audio_duration(audio: dict) -> float:
    """AUDIO 时长（秒）。"""
    wav = audio["waveform"]
    sr = int(audio["sample_rate"])
    # waveform: [B, C, T]
    return float(wav.shape[-1]) / float(sr) if sr else 0.0


def audio_to_wav(audio: dict, path: str) -> float:
    """把 AUDIO 写成 32-bit float WAV，返回时长（秒）。

    不依赖 torchaudio / torchcodec：手写 RIFF/WAVE 头（fmt 格式 3 = IEEE float）。
    这样节点零额外依赖，也避开新版 torchaudio 强制要求 torchcodec 的问题。
    """
    import struct

    wav = audio["waveform"]
    sr = int(audio["sample_rate"])
    if wav.dim() == 3:
        wav = wav[0]            # [B, C, T] -> [C, T]
    if wav.dim() == 1:
        wav = wav.unsqueeze(0)

    data = wav.detach().cpu().float().contiguous().numpy()
    ch, frames = int(data.shape[0]), int(data.shape[1])
    payload = data.T.reshape(-1).astype("<f4").tobytes()   # 交错 float32
    byte_rate = sr * ch * 4
    block_align = ch * 4

    fmt = struct.pack("<HHIIHH", 3, ch, sr, byte_rate, block_align, 32)  # 3 = IEEE float
    fmt_chunk = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    data_chunk = b"data" + struct.pack("<I", len(payload)) + payload
    riff_size = 4 + len(fmt_chunk) + len(data_chunk)

    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", riff_size) + b"WAVE")
        f.write(fmt_chunk)
        f.write(data_chunk)

    return float(frames) / float(sr) if sr else 0.0


def split_audio_channels(wav: torch.Tensor) -> torch.Tensor:
    """确保 waveform 是 [B, C, T]。"""
    if wav.dim() == 1:
        return wav.unsqueeze(0).unsqueeze(0)
    if wav.dim() == 2:
        return wav.unsqueeze(0)
    return wav


def unwrap(value, default=None):
    """is_input_list=True 时所有输入都会被包成单元素列表，这里取出来。

    ComfyUI 的官方节点（如 ConcatenateVideo）用 codec[0] 就是同样的处理。
    """
    if isinstance(value, (list, tuple)):
        return value[0] if len(value) else default
    return value


def as_float(value, default: float = 0.0) -> float:
    """把可能是 list 的数值输入转成 float。"""
    v = unwrap(value, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def as_str(value, default: str = "") -> str:
    """把可能是 list 的字符串输入转成 str。"""
    v = unwrap(value, default)
    if v is None:
        return default
    return str(v)


def crossfade_join(pieces: list[torch.Tensor], sr: int, crossfade: float) -> torch.Tensor:
    """把多段 [B,C,T] 音频按顺序拼接，段间用等功率交叉淡化。

    crossfade 秒数会被限制在相邻两段较短者的一半以内，避免重叠超过片段本身。
    """
    if len(pieces) == 1:
        return pieces[0]

    fade = max(0.0, float(crossfade))
    if fade <= 0.0:
        return torch.cat(pieces, dim=-1)

    out = pieces[0]
    for nxt in pieces[1:]:
        fade_samples = int(round(fade * sr))
        max_overlap = min(out.shape[-1], nxt.shape[-1]) // 2
        overlap = min(fade_samples, max_overlap)
        if overlap <= 0:
            out = torch.cat([out, nxt], dim=-1)
            continue

        tail = out[:, :, -overlap:]
        head = nxt[:, :, :overlap]

        # 等功率淡入淡出：cos/sin 曲线，避免中间音量塌陷
        t = torch.linspace(0.0, 1.0, overlap, dtype=out.dtype, device=out.device)
        fade_out_curve = torch.cos(t * math.pi / 2)   # 1 -> 0
        fade_in_curve = torch.sin(t * math.pi / 2)    # 0 -> 1

        blended = tail * fade_out_curve + head * fade_in_curve
        out = torch.cat([out[:, :, :-overlap], blended, nxt[:, :, overlap:]], dim=-1)
    return out


def apply_head_tail_fade(wav: torch.Tensor, sr: int, fade_in: float, fade_out: float) -> torch.Tensor:
    """给整条音频加头尾淡入淡出（线性）。"""
    length = wav.shape[-1]

    if fade_in > 0:
        n = min(int(round(fade_in * sr)), length)
        if n > 0:
            t = torch.linspace(0.0, 1.0, n, dtype=wav.dtype, device=wav.device)
            wav = wav.clone()
            wav[:, :, :n] *= t

    if fade_out > 0:
        n = min(int(round(fade_out * sr)), wav.shape[-1])
        if n > 0:
            t = torch.linspace(1.0, 0.0, n, dtype=wav.dtype, device=wav.device)
            wav = wav.clone()
            wav[:, :, -n:] *= t

    return wav


# --------------------------------------------------------------------------- 节点

class AudioCut:
    """音频切片：按秒裁出指定区间。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "audio": ("AUDIO",),
                "start": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 86400.0, "step": 0.01,
                    "tooltip": "起点（秒）。",
                }),
                "end": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 86400.0, "step": 0.01,
                    "tooltip": "终点（秒）。0 表示到结尾。",
                }),
            }
        }

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("audio",)
    FUNCTION = "cut"
    CATEGORY = "PostFX/audio"

    def cut(self, audio, start, end):
        wav = split_audio_channels(audio["waveform"])
        sr = int(audio["sample_rate"])
        total = wav.shape[-1]

        s = max(0, int(round(start * sr)))
        e = total if end <= 0 else int(round(end * sr))
        e = min(e, total)

        if s >= e:
            raise RuntimeError(
                "音频切片范围无效：start=%.2fs end=%.2fs，音频总长 %.2fs"
                % (start, end, total / sr)
            )
        if s >= total:
            raise RuntimeError("起点 %.2fs 超出音频时长 %.2fs" % (start, total / sr))

        return ({"waveform": wav[:, :, s:e].contiguous(), "sample_rate": sr},)


class AudioJoin(io.ComfyNode):
    """音频拼接：把多段音频按顺序接成一条，段间可交叉淡化（叠化）。

    输入管道随连接自动增长，和 Concatenate Video 节点一致。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="PostFXAudioJoin",
            display_name="音频拼接 (PostFX)",
            category="PostFX/audio",
            description="按输入顺序把多段音频接成一条，输入口随连接自动增加，段间可叠化。",
            inputs=[
                io.Autogrow.Input(
                    "audios",
                    template=io.Autogrow.TemplatePrefix(
                        io.Audio.Input("audio", tooltip="一段待拼接的音频。"),
                        prefix="audio",
                        min=1,
                        max=64,
                    ),
                    tooltip="按输入顺序拼接的音频段。",
                ),
                io.Float.Input(
                    "crossfade",
                    default=0.0,
                    min=0.0,
                    max=10.0,
                    step=0.01,
                    tooltip="段间叠化（交叉淡化）时长，秒。0 = 硬切。",
                ),
                io.Float.Input(
                    "fade_in",
                    default=0.0,
                    min=0.0,
                    max=10.0,
                    step=0.01,
                    tooltip="整条音频开头淡入时长，秒。0 = 不淡入。",
                ),
                io.Float.Input(
                    "fade_out",
                    default=0.0,
                    min=0.0,
                    max=10.0,
                    step=0.01,
                    tooltip="整条音频结尾淡出时长，秒。0 = 不淡出。",
                ),
                io.String.Input(
                    "order",
                    default="",
                    multiline=False,
                    placeholder="留空=按端口顺序; 也可填 3,1,5,2",
                    tooltip="拼接顺序。留空按 audio0,audio1,audio2... 的端口顺序；"
                           "填逗号分隔的端口序号可重排，如 3,1,5,2 表示第4口、第2口、第6口、第3口。",
                ),
            ],
            outputs=[
                io.Audio.Output(display_name="audio", tooltip="拼接后的音频。"),
                io.String.Output(display_name="order_used", tooltip="实际使用的拼接顺序（端口序号，逗号分隔）。"),
            ],
            is_input_list=True,
        )

    @classmethod
    def execute(cls, audios, crossfade=0.0, fade_in=0.0, fade_out=0.0, order="") -> io.NodeOutput:
        # is_input_list=True 时普通输入也会被包成单元素列表，先解开
        crossfade = as_float(crossfade, 0.0)
        fade_in = as_float(fade_in, 0.0)
        fade_out = as_float(fade_out, 0.0)
        order = as_str(order, "")

        # 只保留真正接到线的端口，按端口序号（audio0 -> 0）排序。
        # ComfyUI 对 is_input_list 节点会把动态输入收成 dict 后再转成 list，
        # 两种形态都要兼容。
        connected = []

        # audios 可能被包成 [dict] 或直接是 dict / list
        audio_source = audios
        if isinstance(audio_source, (list, tuple)) and len(audio_source) == 1 \
                and isinstance(audio_source[0], dict):
            audio_source = audio_source[0]

        if isinstance(audio_source, dict):
            items = audio_source.items()
        elif isinstance(audio_source, (list, tuple)):
            items = list(enumerate(audio_source))
        else:
            items = []

        for key, group in items:
            # group 可能是 list（is_input_list 包装）或单个对象
            if not isinstance(group, (list, tuple)):
                group = [group]
            for a in group:
                if a is None:
                    continue
                m = re.search(r"(\d+)$", str(key))
                idx = int(m.group(1)) if m else len(connected)
                connected.append((idx, a))

        if not connected:
            raise RuntimeError("音频拼接：没有有效的音频输入。")
        connected.sort(key=lambda x: x[0])

        # 用户指定的顺序（逗号分隔的端口序号）
        if order and str(order).strip():
            wanted = [int(x.strip()) for x in str(order).replace("，", ",").split(",") if x.strip().isdigit()]
            if wanted:
                by_idx = {i: a for i, a in connected}
                picked = []
                for w in wanted:
                    if w not in by_idx:
                        raise RuntimeError(
                            "顺序参数里的端口 %d 没有连线。当前已连接端口：%s"
                            % (w, sorted(by_idx.keys()))
                        )
                    picked.append((w, by_idx[w]))
                # 未在顺序里列出的端口按原顺序追加在后面
                for i, a in connected:
                    if i not in wanted:
                        picked.append((i, a))
                connected = picked

        segments = [a for _, a in connected]
        order_used = ",".join(str(i) for i, _ in connected)

        base_sr = int(segments[0]["sample_rate"])
        for idx, seg in enumerate(segments, 1):
            sr = int(seg["sample_rate"])
            if sr != base_sr:
                raise RuntimeError(
                    "采样率不一致：第 1 路 %d Hz，第 %d 路 %d Hz。"
                    "请先统一采样率再拼接。" % (base_sr, idx, sr)
                )

        max_ch = max(split_audio_channels(s["waveform"]).shape[1] for s in segments)
        pieces = []
        for seg in segments:
            w = split_audio_channels(seg["waveform"])
            if w.shape[1] < max_ch:
                pad = max_ch - w.shape[1]
                w = torch.cat([w, w[:, -1:, :].expand(-1, pad, -1)], dim=1)
            pieces.append(w)

        merged = crossfade_join(pieces, base_sr, crossfade)
        merged = apply_head_tail_fade(merged, base_sr, fade_in, fade_out)
        return io.NodeOutput(
            {"waveform": merged, "sample_rate": base_sr},
            order_used,
        )


class VideoBGM:
    """给视频加 BGM。

    BGM 作为背景音乐混入（或替换）原音轨；画面走 -c:v copy，绝不重新编码。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video": ("VIDEO",),
                "bgm": ("AUDIO",),
                "mode": (["mix 叠加(保留原音效)", "replace 替换(只留BGM)"], {
                    "default": "mix 叠加(保留原音效)",
                    "tooltip": "mix：原音效与 BGM 混合；replace：丢弃原音效只保留 BGM。",
                }),
                "bgm_volume": ("FLOAT", {
                    "default": 0.15, "min": 0.0, "max": 4.0, "step": 0.01,
                    "tooltip": "BGM 音量倍数。0.15 = 压到 15%，不盖住原音效。",
                }),
                "bgm_start": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.01,
                    "tooltip": "BGM 从视频第几秒开始进入（秒）。",
                }),
                "fade_in": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 60.0, "step": 0.1,
                    "tooltip": "BGM 淡入时长（秒），0 = 不淡入。",
                }),
                "fade_out": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 60.0, "step": 0.1,
                    "tooltip": "BGM 淡出时长（秒），0 = 不淡出。",
                }),
                "bgm_loop": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "BGM 短于视频时循环填充；关闭则只播放一遍，之后静音。",
                }),
                "filename_prefix": ("STRING", {
                    "default": "postfx/bgm",
                    "tooltip": "输出文件名前缀，相对 ComfyUI 的 output 目录。",
                }),
                "codec": (["h264_nvenc", "libx264", "copy 原编码"], {
                    "default": "copy 原编码",
                    "tooltip": "copy 原编码 = 画面零重新编码（最快）。仅在需要重编码时选编码器。",
                }),
            }
        }

    RETURN_TYPES = ("VIDEO", "STRING", "STRING")
    RETURN_NAMES = ("video", "file_path", "log")
    FUNCTION = "add_bgm"
    CATEGORY = "PostFX/video"
    OUTPUT_NODE = False

    def add_bgm(self, video, bgm, mode, bgm_volume, bgm_start, fade_in, fade_out,
                bgm_loop, filename_prefix, codec):
        workdir = tempfile.mkdtemp(prefix="postfx_")
        try:
            # 1. 取视频源（文件路径，不解码）
            src = video.get_stream_source()
            if not isinstance(src, str):
                vid_path = os.path.join(workdir, "src.mp4")
                video.save_to(vid_path)
                src = vid_path
            vid_path = src

            # 2. BGM 写盘
            bgm_path = os.path.join(workdir, "bgm.wav")
            bgm_dur = audio_to_wav(bgm, bgm_path)

            # 3. 视频时长
            vdur = self._probe_duration(vid_path)
            if vdur <= 0:
                raise RuntimeError("无法读取视频时长，源文件可能损坏：%s" % vid_path)

            # 4. 输出路径
            out_dir = os.path.join(folder_paths.get_output_directory(), os.path.dirname(filename_prefix) or "")
            os.makedirs(out_dir, exist_ok=True)
            base = os.path.basename(filename_prefix) or "bgm"
            out_path = os.path.join(out_dir, base + "_%05d.mp4" % 1)
            n = 1
            while os.path.exists(out_path):
                n += 1
                out_path = os.path.join(out_dir, base + "_%05d.mp4" % n)

            # 5. 组装 ffmpeg
            args = ["-i", vid_path, "-i", bgm_path]

            has_src_audio = self._has_audio(vid_path)
            is_mix = mode.startswith("mix")

            # BGM 链：延后进入 / 缩放 / 循环 / 淡入淡出 / 裁到视频长度
            bgm_chain = []
            if bgm_start > 0:
                bgm_chain.append("adelay=%d|%d" % (int(bgm_start * 1000), int(bgm_start * 1000)))
            if bgm_volume != 1.0:
                bgm_chain.append("volume=%.4f" % bgm_volume)
            if bgm_loop and bgm_dur < vdur:
                loops = max(1, int((vdur / max(bgm_dur, 0.01)) + 1))
                bgm_chain.append("aloop=loop=%d:size=%d" % (loops, int(bgm_dur * 44100)))
            if fade_in > 0:
                bgm_chain.append("afade=t=in:st=%.2f:d=%.2f" % (bgm_start, fade_in))
            if fade_out > 0:
                st = max(0.0, vdur - fade_out)
                bgm_chain.append("afade=t=out:st=%.2f:d=%.2f" % (st, fade_out))
            bgm_chain.append("atrim=0:%.3f" % vdur)
            bgm_expr = "[1:a]" + ",".join(bgm_chain) + "[bgm]"

            if is_mix:
                if has_src_audio:
                    filt = "[0:a]anull[orig];%s;[orig][bgm]amix=inputs=2:duration=first:normalize=0[aout]" % bgm_expr
                else:
                    filt = "%s;[bgm]anull[aout]" % bgm_expr
            else:
                filt = "%s;[bgm]anull[aout]" % bgm_expr

            args += ["-filter_complex", filt, "-map", "0:v", "-map", "[aout]", "-shortest"]

            if codec == "copy 原编码":
                args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
            else:
                args += ["-c:v", codec, "-c:a", "aac", "-b:a", "192k"]
                if codec == "h264_nvenc":
                    args += ["-preset", "p4", "-rc", "vbr", "-cq", "19"]

            args += ["-movflags", "+faststart", out_path]

            rc, log_text = run_ffmpeg(args)
            if rc != 0:
                raise RuntimeError("ffmpeg 失败（退出码 %d）：\n%s" % (rc, log_text[-3000:]))

            # 6. 载入为 VIDEO 输出，便于下游继续用
            vid_out = self._load_video(out_path)

            summary = (
                "输出 %s\n视频时长 %.2fs | BGM %.2fs | 模式 %s | 音量 %.2f | 编码 %s"
                % (out_path, vdur, bgm_dur, mode, bgm_volume, codec)
            )
            logger.info(summary)
            return (vid_out, out_path, summary + "\n\n" + log_text[-2000:])
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # -- 辅助 -------------------------------------------------------------

    @staticmethod
    def _probe_duration(path: str) -> float:
        try:
            import av
            with av.open(path) as c:
                if c.duration and c.duration / av.time_base > 0:
                    return float(c.duration / av.time_base)
                st = c.streams.video[0]
                if st.duration and st.time_base:
                    return float(st.duration * st.time_base)
        except Exception:  # noqa: BLE001
            pass
        # 兜底走 ffprobe
        try:
            probe = shutil.which("ffprobe")
            if probe:
                r = subprocess.run(
                    [probe, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=nw=1:nk=1", path],
                    capture_output=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return float((r.stdout or b"").decode().strip() or 0)
        except Exception:  # noqa: BLE001
            pass
        return 0.0

    @staticmethod
    def _has_audio(path: str) -> bool:
        try:
            import av
            with av.open(path) as c:
                return bool(c.streams.audio)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _load_video(path: str):
        from comfy_api.input_impl import VideoFromFile
        return VideoFromFile(path)


NODE_CLASS_MAPPINGS = {
    "PostFXAudioCut": AudioCut,
    "PostFXAudioJoin": AudioJoin,
    "PostFXVideoBGM": VideoBGM,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PostFXAudioCut": "音频切片 (PostFX)",
    "PostFXAudioJoin": "音频拼接 (PostFX)",
    "PostFXVideoBGM": "视频加BGM (PostFX)",
}
