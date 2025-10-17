# AstrBot Chatbox 适配器 (astrbot_plugin_chatbox_adapter)

[![GitHub](https://img.shields.io/badge/GitHub-timetetng/astrbot_plugin_chatbox_adapter-blue)](https://github.com/timetetng/astrbot_plugin_chatbox_adapter)

这是一个为 [AstrBot](https://github.com/AstrTools/AstrBot) 提供的平台适配器插件。

它的核心功能是在本地启动一个 Web 服务器，该服务器兼容 **OpenAI API** 标准。这使得您可以将 [Chatbox](https://chatbox.app/) 或任何其他支持 OpenAI API 格式的客户端（例如 ChatGPT-Next-Web, LobeChat, One-API 等）连接到 AstrBot。

简而言之，您可以**在 Chatbox 客户端里，与您的 AstrBot 机器人进行对话**，并使用 AstrBot 强大的 Agentic 能力和插件生态。

---

## 什么是 AstrBot？

AstrBot 致力于成为一个开源的一站式 Agentic 聊天机器人平台及开发框架。通过它，你能够在多种消息平台上部署和开发一个支持大语言模型（LLM）的聊天机器人。

* **大模型对话**。支持接入多种大模型服务。支持多模态、工具调用、MCP、原生知识库、人设等功能。
* **多消息平台支持**。支持接入 QQ、企业微信、微信公众号、飞书、Telegram、钉钉、Discord、KOOK 等平台。支持速率限制、白名单、百度内容审核。
* **Agent**。完善适配的 Agentic 能力。支持多轮工具调用、内置沙盒代码执行器、网页搜索等功能。
* **插件扩展**。深度优化的插件机制，支持开发插件扩展功能，社区插件生态丰富。
* **WebUI**。可视化配置和管理机器人，功能齐全。

## 什么是 Chatbox？

Chatbox AI 是一款 AI 客户端应用和智能助手，支持众多先进的 AI 模型和 API，可在 Windows、MacOS、Android、iOS、Linux 和网页版上使用。

---

## ✨ 功能特性

* **模拟 OpenAI API**：提供 `/v1/models` 和 `/v1/chat/completions` 接口，兼容 Chatbox 等客户端。
* **双向消息转换**：将 Chatbox 的 API 请求转换为 AstrBot 消息事件，并将 AstrBot 的回复（包括文本和图片）转换回 OpenAI 响应格式。
* **支持流式响应**：完全支持 Chatbox 的流式打字机效果。
* **支持工具调用 (Tool Calls)**：当 AstrBot 中的大模型（如 Kimi, GLM4）决定使用工具时，适配器能正确地将其转换为 OpenAI 格式的 `tool_calls` 响应，Chatbox 客户端可以正确解析并执行后续流程。
* **身份模拟 (Spoofing)**：允许配置适配器，使其模拟成另一个平台（如 `aiocqhttp`）的机器人，以便触发那些为特定平台编写的插件。
* **安全验证**：支持配置 `api_key` 进行 Bearer Token 验证。

## 🚀 安装

1.  **安装插件**：
    * 通过 AstrBot 插件市场搜索 `astrbot_plugin_chatbox_adapter` 并安装。
    * 或者，将本项目克隆或下载到 AstrBot 的 `plugins` 目录下。
2.  **安装依赖**：
    * 本插件依赖 `aiohttp`。通常 AstrBot 会自动检测并提示安装。
    * 如果未自动安装，请在 AstrBot 的环境中手动运行：`pip install aiohttp`
3.  **重启 AstrBot**。

## ⚙️ 配置

插件首次加载后，请在 AstrBot 的 `config/platform_config.py` 文件中添加 `chatbox` 平台的配置。

如果您使用 WebUI，请在“平台管理”中新建一个 `chatbox` 适配器实例。

以下是配置项说明：

```python
# config/platform_config.py
PLATFORM_CONFIG = {
    "chatbox": [
        {
            "id": "my_chatbox_server", # 实例 ID，保持唯一
            "enable": True,
            "config": {
                "api_key": "your_secret_key", # 客户端连接时使用的 API Key，留空则不验证
                "port": 8080,               # 监听端口
                "host": "127.0.0.1",        # 监听主机
                "timeout": 300,             # 等待回复的超时时间 (秒)
                
                # --- 默认用户信息 (可选) ---
                "default_user_id": "chatbox_api_user",
                "default_nickname": "Chatbox User",
                
                # --- 平台身份模拟，用于绑定QQ ID的插件 (可选) ---
                "spoof_platform": "",       # 要模拟的平台 ID, 例如 "aiocqhttp"
                "spoof_self_id": "",        # 要模拟的机器人 Bot ID (例如 QQ 号)
                "spoof_user_id": "",        # 要模拟的发送者 ID (例如 QQ 号)
                "spoof_nickname": ""        # 要模拟的发送者昵称
            }
        }
    ]
}