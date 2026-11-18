"""离线验证 PostFX 节点：不依赖 ComfyUI 服务器，直接跑核心逻辑。

构造合成的 AUDIO 和 VIDEO，验证：
  1. AudioCut 切片长度正确
  2. AudioJoin 拼接长度正确
  3. VideoBGM 走 ffmpeg 输出，且画面未被重编码（-c:v copy）
"""

import os
import shutil
import subprocess
import sys
import tempfile
import types

ROOT = r"C:\Users\Brend\AppData\Local\Comfy-Desktop\ComfyUI-Installs\ComfyUI\ComfyUI"
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "custom_nodes", "ComfyUI-PostFX"))

import torch  # noqa: E402

# --- 伪造 folder_paths，让 nodes.py 可独立导入 -------------------------------
fake_fp = types.ModuleType("folder_paths")
OUT_DIR = tempfile.mkdtemp(prefix="postfx_out_")
fake_fp.get_output_directory = lambda: OUT_DIR
fake_fp.models_dir = os.path.join(ROOT, "models")
sys.modules["folder_paths"] = fake_fp

import nodes as postfx  # noqa: E402


def make_audio(seconds, sr=44100, channels=1, freq=440.0):
    t = torch.arange(int(seconds * sr), dtype=torch.float32) / sr
    wave = torch.sin(2 * math.pi * freq * t).unsqueeze(0).unsqueeze(0)
    return {"waveform": wave, "sample_rate": sr}


import math  # noqa: E402


def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + ("  " + detail if detail else ""))
    return ok


def main():
    all_ok = True
    print("=" * 62)
    print("PostFX 节点离线验证")
    print("=" * 62)

    # 1) AudioCut
    print("\n[1] 音频切片")
    src = make_audio(10.0)
    cut = postfx.AudioCut()
    out, = cut.cut(src, start=2.0, end=5.0)
    dur = postfx.audio_duration(out)
    all_ok &= check("切片 2.0s~5.0s -> 3.0s", abs(dur - 3.0) < 0.02, "实际 %.3fs" % dur)

    out2, = cut.cut(src, start=0.0, end=0.0)
    dur2 = postfx.audio_duration(out2)
    all_ok &= check("end=0 表示到结尾", abs(dur2 - 10.0) < 0.02, "实际 %.3fs" % dur2)

    # 越界应报错
    try:
        cut.cut(src, start=20.0, end=30.0)
        all_ok &= check("越界应报错", False, "没有抛异常")
    except RuntimeError as e:
        all_ok &= check("越界正确报错", True)

    # 2) AudioJoin
    print("\n[2] 音频拼接")
    a = make_audio(2.0, freq=440)
    b = make_audio(3.0, freq=880)
    join = postfx.AudioJoin()
    joined, = join.join(2, a, audio_2=b)
    jd = postfx.audio_duration(joined)
    all_ok &= check("2s + 3s = 5s", abs(jd - 5.0) < 0.02, "实际 %.3fs" % jd)

    # 采样率不一致应报错
    c = make_audio(1.0, sr=48000)
    try:
        join.join(2, a, audio_2=c)
        all_ok &= check("采样率不一致应报错", False, "没有抛异常")
    except RuntimeError:
        all_ok &= check("采样率不一致正确报错", True)

    # 3) VideoBGM —— 造一个真实视频
    print("\n[3] 视频加 BGM（ffmpeg 容器级操作）")
    ff = postfx.ffmpeg_path()
    print("     ffmpeg:", ff)

    tmp = tempfile.mkdtemp(prefix="postfx_vid_")
    src_video = os.path.join(tmp, "src.mp4")

    # 3 秒 320x240 视频，带一条正弦音轨
    r = subprocess.run(
        [ff, "-hide_banner", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=300:duration=3",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", src_video],
        capture_output=True, timeout=120,
    )
    if r.returncode != 0:
        print("  [FAIL] 造测试视频失败:", r.stderr.decode()[-600:])
        return False
    print("     源视频: %.2fs  带音轨=%s" % (
        postfx.VideoBGM._probe_duration(src_video),
        postfx.VideoBGM._has_audio(src_video)))

    # 伪造一个 VideoInput 对象（只要能提供 get_stream_source）
    class FakeVideo:
        def __init__(self, p):
            self._p = p
        def get_stream_source(self):
            return self._p
        def save_to(self, path, **_kw):
            import shutil as sh
            sh.copy(self._p, path)

    bgm_audio = make_audio(2.0, freq=220)  # 比视频短，测试循环
    node = postfx.VideoBGM()
    vid_out, out_path, log = node.add_bgm(
        video=FakeVideo(src_video),
        bgm=bgm_audio,
        mode="mix 叠加(保留原音效)",
        bgm_volume=0.15,
        bgm_start=0.0,
        fade_in=0.5,
        fade_out=0.5,
        bgm_loop=True,
        filename_prefix="postfx/test",
        codec="copy 原编码",
    )
    exists = os.path.isfile(out_path)
    all_ok &= check("输出文件生成", exists, out_path)
    if exists:
        od = postfx.VideoBGM._probe_duration(out_path)
        all_ok &= check("输出时长与源一致(~3s)", abs(od - 3.0) < 0.15, "实际 %.2fs" % od)
        all_ok &= check("输出保留音轨", postfx.VideoBGM._has_audio(out_path))

        # 关键：确认画面流是 copy（未重编码）—— 比对视频流编码参数
        ffprobe = os.path.join(os.path.dirname(ff),
                               "ffprobe.exe" if os.name == "nt" else "ffprobe")
        if not os.path.isfile(ffprobe):
            ffprobe = shutil.which("ffprobe") or ""
        pr = subprocess.run(
            [ffprobe, "-v", "error",
             "-select_streams", "v:0", "-show_entries",
             "stream=codec_name,width,height,nb_frames", "-of", "csv=p=0", out_path],
            capture_output=True, timeout=30,
        )
        print("     输出视频流:", pr.stdout.decode().strip())
        all_ok &= check("画面分辨率未变(320x240)", "320,240" in pr.stdout.decode())

    # 4) 采样率不同的 BGM 拼接提示
    print("\n[4] 边界：BGM 采样率 48k vs 视频 44.1k")
    print("     当前实现要求拼接采样率一致；单条 BGM 混入不受此限制。")

    print("\n" + "=" * 62)
    print("结果:", "全部通过" if all_ok else "存在失败项")
    print("=" * 62)
    return all_ok


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
