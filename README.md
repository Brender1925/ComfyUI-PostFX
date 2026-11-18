# ComfyUI-PostFX

**English** | [简体中文](README.zh-CN.md)

Post-production nodes for ComfyUI that operate at the **container level** — video
frames are never decoded unless a model actually needs them.

| Node | What it does |
|---|---|
| **音频切片 (PostFX)** / Audio Cut | Trim a segment out of an audio clip (pure tensor slice, zero overhead) |
| **音频拼接 (PostFX)** / Audio Join | Join several audio clips in order, with optional cross-fade between them |
| **视频加BGM (PostFX)** / Video Add BGM | Mix or replace the soundtrack with background music, **without re-encoding the picture** |

## Why this exists

Anything you can do with a container operation (concatenating clips, mixing
tracks, adding BGM, trimming) should not involve splitting a video into frames.
Decoding and re-encoding a 2-minute 1080p clip takes tens of seconds and
loses quality; the same operation done at the container level takes
**milliseconds and is bit-exact**.

These nodes follow that rule:

- `Audio Cut` / `Audio Join` work directly on the audio tensor.
- `Video Add BGM` takes the video's file path via `get_stream_source()` and
  hands it to ffmpeg with `-c:v copy`, so **the picture stream is never
  re-encoded**.

## Nodes

### 音频切片 (PostFX) — Audio Cut

| Input | Type | Default | Notes |
|---|---|---|---|
| `audio` | `AUDIO` | — | Source clip |
| `start` | FLOAT | 0.0 | Start position in seconds |
| `end` | FLOAT | 0.0 | End position in seconds. `0` = to the end |

**Output**: `audio`

### 音频拼接 (PostFX) — Audio Join

The input sockets grow automatically as you connect them, just like ComfyUI's
built-in *Concatenate Video* node.

| Input | Type | Default | Notes |
|---|---|---|---|
| `audio…` | `AUDIO` (autogrow) | — | Clips to join, in order |
| `crossfade` | FLOAT | 0.0 | Cross-fade between clips, in seconds. `0` = hard cut |
| `fade_in` | FLOAT | 0.0 | Fade in at the very start, in seconds |
| `fade_out` | FLOAT | 0.0 | Fade out at the very end, in seconds |
| `order` | STRING | `""` | Optional explicit order, e.g. `"13,0,5,1"` |

**Outputs**: `audio`, `order_used`

#### A note on ordering

ComfyUI's frontend sometimes renders the autogrow sockets out of order
(you may see `audio10…audio13` listed above `audio2…audio4`). **That is a
display bug only** — the backend always merges in ascending socket index
(`audio0`, `audio1`, `audio2`, …).

If you need certainty, read the `order_used` output, or set `order` explicitly.
Connecting a *contiguous* range of sockets (`audio0`…`audio5`) avoids the
confusion entirely.

#### Cross-fade

Cross-fades use an **equal-power** curve (`cos`/`sin`), not a linear ramp.
A linear cross-fade loses energy in the middle and audibly dips; the
equal-power curve keeps the perceived loudness steady through the transition.

Fade lengths are automatically clamped so an overlap never exceeds half of
the shorter of the two adjacent clips.

### 视频加BGM (PostFX) — Video Add BGM

| Input | Type | Default | Notes |
|---|---|---|---|
| `video` | `VIDEO` | — | The video to add music to |
| `bgm` | `AUDIO` | — | Background music track |
| `mode` | Combo | `mix 叠加(保留原音效)` | `mix` keeps the original audio, `replace` drops it |
| `bgm_volume` | FLOAT | 0.15 | BGM gain. `0.15` = 15%, so it stays under the original sound |
| `bgm_start` | FLOAT | 0.0 | Delay before the BGM enters, in seconds |
| `fade_in` | FLOAT | 0.0 | BGM fade-in duration, in seconds |
| `fade_out` | FLOAT | 0.0 | BGM fade-out duration, in seconds |
| `bgm_loop` | BOOLEAN | True | Loop the BGM if it is shorter than the video |
| `filename_prefix` | STRING | `postfx/bgm` | Output path relative to ComfyUI's `output/` |
| `codec` | Combo | `copy 原编码` | `copy` = picture is never re-encoded |

**Outputs**: `video`, `file_path`, `log`

## Installation

### ComfyUI Manager

Search for **ComfyUI-PostFX** in *Custom Nodes Manager* and install.

### Manual

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/Brender1925/ComfyUI-PostFX.git
```

Restart ComfyUI and hard-refresh the browser (Ctrl+F5).

## Requirements

- ComfyUI 0.45+ (the `Autogrow` input API is used by *Audio Join*)
- Python 3.10+
- **ffmpeg** on `PATH`, or installed at `C:\ffmpeg\bin\ffmpeg.exe` /
  `/usr/bin/ffmpeg`

No Python packages beyond what ComfyUI already ships.

## Typical pipeline

```
several BGM clips
      │
      ▼
音频拼接 (PostFX)   ← set crossfade here for transitions between clips
      │
      ▼
视频加BGM (PostFX)  ← set fade_in / fade_out here for the whole track
      │
      ▼
   finished mp4
```

The two fade controls live in different places on purpose: `crossfade` shapes
the joins *between* clips, while `fade_in` / `fade_out` shape the entry and
exit of the finished music against the video.

## License

MIT — see [LICENSE](LICENSE).
