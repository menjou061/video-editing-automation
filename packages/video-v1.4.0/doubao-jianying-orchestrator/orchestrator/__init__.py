"""豆包剪映智能编排引擎（跨平台模块化包）。

模块：
- platform_env   跨平台环境/剪映目录/进程识别
- voice_catalog  TTS 音色矩阵与剧情推荐
- voice_tts      TTS 并发合成与增量缓存
- bgm_selector   云曲库选曲/下载/铺底响度闭环
- stability      帧间运动稳定选点与转场建议
- script_polish  口播衔接与整稿覆盖校验
- draft_safety   草稿写入前占用检查/写后校验/索引同步
- engine         主编排流水线
- cli            命令行入口
"""
from .engine import OrchestrationEngine

__version__ = "1.4.0"

__all__ = ["OrchestrationEngine"]
