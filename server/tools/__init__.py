"""小新的工具（function calling）。每个功能一个模块：SPECS（给模型的 schema）+ run(name, args) -> str。
注册表在 registry.py；提示词在 server/prompts/<功能>.md，和工具一一对应。"""
