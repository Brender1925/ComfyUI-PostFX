"""
ComfyUI-PostFX — 后期处理节点包

三个节点，全部容器级操作，不解码画面：
  • AudioCut      音频切片（纯 tensor 操作，零拷贝开销）
  • AudioJoin     音频拼接（tensor concat + 采样率检查）
  • VideoBGM      给视频加 BGM（ffmpeg -c:v copy，画面零解码）

设计原则：BGM 只当背景音乐用，不涉及配音/对白/音效生成。
"""

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

WEB_DIRECTORY = None
