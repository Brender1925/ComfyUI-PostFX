# ComfyUI-PostFX

[English](README.md) | **简体中文**

ComfyUI 的后期处理节点包，全部在**容器层**操作 —— 除非模型真的需要，否则绝不把视频拆成帧序列。

| 节点 | 作用 |
|---|---|
| **音频切片 (PostFX)** | 从音频中裁出一段（纯 tensor 切片，几乎零开销） |
| **音频拼接 (PostFX)** | 按顺序拼接多段音频，段间可交叉淡化（叠化） |
| **视频加BGM (PostFX)** | 给视频混入或替换背景音乐，**画面绝不重新编码** |

## 为什么需要这个

能用容器层完成的事（拼接片段、混音、加 BGM、裁剪），就不该先把视频拆成帧。
解码再编码一段 2 分钟 1080p 视频要几十秒，而且掉画质；同样的操作在容器层做
只要**几毫秒，而且画面逐字节不变**。

这些节点遵循这个原则：

- `音频切片` / `音频拼接` 直接操作音频 tensor。
- `视频加BGM` 通过 `get_stream_source()` 取到视频文件路径，交给 ffmpeg 并加上
  `-c:v copy`，**画面流一次都不重编码**。

## 节点说明

### 音频切片 (PostFX)

| 输入 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `audio` | `AUDIO` | — | 源音频 |
| `start` | FLOAT | 0.0 | 起点（秒） |
| `end` | FLOAT | 0.0 | 终点（秒）。`0` 表示到结尾 |

**输出**：`audio`

### 音频拼接 (PostFX)

输入端口随连接自动增加，和 ComfyUI 内置的 *Concatenate Video* 一样。

| 输入 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `audio…` | `AUDIO`（自动增长） | — | 待拼接的音频段，按顺序 |
| `crossfade` | FLOAT | 0.0 | 段间叠化时长（秒）。`0` = 硬切 |
| `fade_in` | FLOAT | 0.0 | 整条音频开头淡入时长（秒） |
| `fade_out` | FLOAT | 0.0 | 整条音频结尾淡出时长（秒） |
| `order` | STRING | `""` | 可选的显式顺序，例如 `"13,0,5,1"` |

**输出**：`audio`、`order_used`

#### 关于顺序

ComfyUI 前端有时会把自动增长的端口显示成乱序（你可能看到 `audio10…audio13`
排在 `audio2…audio4` 前面）。**这只是显示层的 bug** —— 后端永远按端口序号升序
合并（`audio0`、`audio1`、`audio2`……）。

如果需要确定的结果，读 `order_used` 输出，或者用 `order` 参数显式指定。
把**连续**的端口连起来（`audio0`…`audio5`）可以完全避免这个困扰。

#### 关于叠化

叠化采用**等功率曲线**（`cos`/`sin`），而不是线性插值。线性交叉淡化会在中点
损失能量、听感上明显"凹"一下；等功率曲线能让过渡过程中的感知响度保持稳定。

叠化长度会自动收敛，保证重叠部分不超过相邻两段中较短者的一半。

### 视频加BGM (PostFX)

| 输入 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `video` | `VIDEO` | — | 要加 BGM 的视频 |
| `bgm` | `AUDIO` | — | 背景音乐 |
| `mode` | Combo | `mix 叠加(保留原音效)` | `mix` 保留原音效，`replace` 只留 BGM |
| `bgm_volume` | FLOAT | 0.15 | BGM 音量倍数。`0.15` = 15%，不盖住原音效 |
| `bgm_start` | FLOAT | 0.0 | BGM 延后进入的秒数 |
| `fade_in` | FLOAT | 0.0 | BGM 淡入时长（秒） |
| `fade_out` | FLOAT | 0.0 | BGM 淡出时长（秒） |
| `bgm_loop` | BOOLEAN | True | BGM 短于视频时循环填充 |
| `filename_prefix` | STRING | `postfx/bgm` | 输出路径，相对 ComfyUI 的 `output/` |
| `codec` | Combo | `copy 原编码` | `copy` = 画面绝不重新编码 |

**输出**：`video`、`file_path`、`log`

## 安装

### ComfyUI Manager

在 *Custom Nodes Manager* 里搜索 **ComfyUI-PostFX** 安装。

### 手动安装

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/Brender1925/ComfyUI-PostFX.git
```

重启 ComfyUI，浏览器硬刷新（Ctrl+F5）。

## 环境要求

- ComfyUI 0.45+（*音频拼接* 用到了 `Autogrow` 输入 API）
- Python 3.10+
- **ffmpeg** 在 `PATH` 中，或安装于 `C:\ffmpeg\bin\ffmpeg.exe` / `/usr/bin/ffmpeg`

不需要额外安装任何 Python 包。

## 典型流水线

```
多段 BGM
   │
   ▼
音频拼接 (PostFX)   ← 在这里设 crossfade，控制段与段之间的过渡
   │
   ▼
视频加BGM (PostFX)  ← 在这里设 fade_in / fade_out，控制整条音乐的首尾
   │
   ▼
  成品 mp4
```

两个淡入淡出参数分开是有意为之：`crossfade` 塑造的是片段**之间**的衔接，
而 `fade_in` / `fade_out` 塑造的是整条音乐相对视频的**进入与退出**。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。
