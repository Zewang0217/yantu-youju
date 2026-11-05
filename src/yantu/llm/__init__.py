"""LLM 客户端层:OpenAI 兼容 /chat/completions,零第三方依赖(urllib)。

配置文件:config/llm.json(已 gitignore;模板见 config/llm.example.json)。
未配置或配置缺 key 时 is_configured()=False,上层(结构化Agent)走确定性
基线/降级,不假装调用了模型。
"""

from .client import ChatClient, LLMError, load_client

__all__ = ["ChatClient", "LLMError", "load_client"]
