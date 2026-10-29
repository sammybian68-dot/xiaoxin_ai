"""公司 LLM 代理的直连调用：单次 chat，和按行循环的批量任务。用法见 README.md。"""

from src.llm.client import (ALIASES, DEFAULT_MODEL, ProxyError, Reply, Timing, chat, chat_full, chat_many, chat_stream,
                            config, models, resolve_model)
from src.llm.batch import latest, run as run_batch

__all__ = ["ALIASES", "DEFAULT_MODEL", "Timing", "chat_stream", "ProxyError", "Reply", "chat", "chat_full", "chat_many", "config", "models",
           "resolve_model", "latest", "run_batch"]
