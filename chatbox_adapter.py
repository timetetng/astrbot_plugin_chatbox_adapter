import asyncio
import json
import time
import uuid

try:
    from aiohttp import web
except ImportError:
    print("缺少 aiohttp 依赖，请在插件的 requirements.txt 中添加 aiohttp")
    raise

from astrbot.api.platform import Platform, AstrBotMessage, MessageMember, PlatformMetadata, MessageType, register_platform_adapter
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Image
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.api import logger

# 导入自定义事件
from .chatbox_event import ChatboxEvent

DEFAULT_CONFIG = {
    "api_key": "your_secret_key",
    "port": 8080,
    "host": "127.0.0.1",
    "timeout": 300,
    "default_user_id": "chatbox_api_user",
    "default_nickname": "Chatbox User",
    "spoof_platform": "",
    "spoof_user_id": "",
    "spoof_nickname": "",
    "spoof_self_id": ""
}

@register_platform_adapter("chatbox", "Chatbox (OpenAI API) 适配器", default_config_tmpl=DEFAULT_CONFIG)
class ChatboxAdapter(Platform):

    def __init__(self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue) -> None:
        super().__init__(event_queue)
        self.config = platform_config
        self.settings = platform_settings
        
        self.port = self.config.get('port', 8080)
        self.host = self.config.get('host', '127.0.0.1')
        self.api_key = self.config.get('api_key')
        self.timeout = self.config.get('timeout', 300)
        self.default_user_id = self.config.get('default_user_id', 'chatbox_api_user')
        self.default_nickname = self.config.get('default_nickname', 'Chatbox User')
        self.spoof_platform = self.config.get('spoof_platform')
        self.spoof_user_id = self.config.get('spoof_user_id')
        self.spoof_nickname = self.config.get('spoof_nickname')
        self.spoof_self_id = self.config.get('spoof_self_id')
        
        self.instance_id = self.settings.get('id') or 'chatbox'
        
        self.pending_requests = {}
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata("chatbox", "Chatbox (OpenAI API) 适配器", logo_path="icon.png")

    async def send_by_session(self, session: MessageSesion, message_chain: MessageChain):
        logger.warning("ChatboxAdapter 不支持主动消息 (send_by_session)")
        pass

    async def run(self):
        app = web.Application()
        app.router.add_get("/v1/models", self.handle_list_models)
        app.router.add_post("/v1/chat/completions", self.handle_chat_completions)
        
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        
        self.site = web.TCPSite(
            self.runner,
            self.host,
            self.port,
            reuse_address=True,
            reuse_port=True
        )
        
        logger.info(f"Chatbox (OpenAI API) 适配器尝试在 http://{self.host}:{self.port} 上监听...")
        
        if not self.site:
            logger.error("Chatbox 适配器: site 未初始化")
            return
            
        try:
            await self.site.start()
            logger.info(f"Chatbox (OpenAI API) 适配器成功在 http://{self.host}:{self.port} 上监听。")

            while True:
                await asyncio.sleep(3600)
                
        except asyncio.CancelledError:
            logger.info("Chatbox 适配器 run 任务被取消...")
        except OSError as e:
            if e.errno == 98:
                logger.error(f"端口 {self.port} 仍被占用。即使设置了 reuse_port，也无法绑定。")
                logger.error("这强烈表明有一个 *旧的* 适配器实例(没有此修复) 仍在运行。")
                logger.error("请从命令行 'kill' AstrBot 进程来清理僵尸进程，然后重试。")
            else:
                logger.error(f"启动服务器时发生 OSError: {e}")
        except Exception as e:
            logger.error(f"Chatbox 适配器 run 循环中发生未知错误: {e}")
        finally:
            logger.info(f"正在终止 Chatbox (OpenAI API) 适配器 http://{self.host}:{self.port} ...")
            if self.runner:
                try:
                    await asyncio.wait_for(self.runner.cleanup(), timeout=3.0)
                    logger.info(f"Chatbox (OpenAI API) 适配器已在 http://{self.host}:{self.port} 上停止 (优雅)")
                except asyncio.TimeoutError:
                    logger.warning(f"Chatbox 适配器: cleanup() 在 {self.host}:{self.port} 上超时。强制终止。")
                except Exception as e:
                    logger.error(f"Chatbox 适配器停止失败: {e}")
            
            self.runner = None
            self.site = None

    async def handle_list_models(self, request: web.Request):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return web.json_response({"error": "Missing Authorization header"}, status=401)
        
        token = auth_header.split(" ")[1]
        if self.api_key and token != self.api_key:
            return web.json_response({"error": "Invalid API key"}, status=401)

        model_data = {
            "object": "list",
            "data": [
                {
                    "id": "astrbot",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "astrbot"
                }
            ]
        }
        return web.json_response(model_data)

    async def handle_chat_completions(self, request: web.Request):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return web.json_response({"error": "Missing Authorization header"}, status=401)
        
        token = auth_header.split(" ")[1]
        if self.api_key and token != self.api_key:
            return web.json_response({"error": "Invalid API key"}, status=401)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        is_stream = body.get("stream", False)
        
        try:
            abm, model_name = await self.convert_openai_to_abm(body)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

        response_queue = asyncio.Queue()
        self.pending_requests[abm.message_id] = response_queue

        if self.spoof_platform:
            platform_meta = PlatformMetadata(self.spoof_platform, f"Spoofed {self.spoof_platform}")
        else:
            platform_meta = self.meta()

        message_event = ChatboxEvent(
            message_str=abm.message_str,
            message_obj=abm,
            platform_meta=platform_meta,
            session_id=abm.session_id,
            client=self,
            is_stream=is_stream,
            model_name=model_name
        )
        
        self.commit_event(message_event)

        if is_stream:
            try:
                empty_chunk = self.format_as_openai_chunk({}, abm.message_id, model_name)
                asyncio.create_task(self.safe_queue_put(response_queue, empty_chunk))
            except Exception:
                pass # 忽略心跳发送失败
            return await self.handle_stream_response(request, abm.message_id, response_queue)
        else:
            return await self.handle_non_stream_response(abm.message_id, response_queue)

    async def safe_queue_put(self, queue: asyncio.Queue, item: any):
        """ 异步安全地向队列放入元素，忽略可能的队列关闭错误 """
        try:
            await queue.put(item)
        except Exception as e:
            logger.warning(f"向队列安全放入元素时出错 (可能已关闭): {e}")


    async def handle_non_stream_response(self, message_id: str, queue: asyncio.Queue):
        try:
            final_response = await asyncio.wait_for(queue.get(), timeout=self.timeout)
            return web.json_response(final_response)
        except asyncio.TimeoutError:
            logger.warning(f"【Chatbox 适配器】: (Non-Stream) 队列 {message_id} 等待超时。")
            return web.json_response({"error": "Request timed out"}, status=504)
        finally:
            self.pending_requests.pop(message_id, None)

    async def handle_stream_response(self, request: web.Request, message_id: str, queue: asyncio.Queue):
        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache'}
        )
        await response.prepare(request)

        try:
            while True:
                chunk = await asyncio.wait_for(queue.get(), timeout=self.timeout)
                if chunk == "[DONE]":
                    await response.write(b'data: [DONE]\n\n')
                    break
                
                if chunk.get("choices") and chunk["choices"][0].get("delta") == {}:
                    continue
                    
                chunk_json = json.dumps(chunk)
                await response.write(f'data: {chunk_json}\n\n'.encode('utf-8'))
        except asyncio.TimeoutError:
            logger.warning(f"【Chatbox 适配器】: (Stream) 队列 {message_id} 等待超时。")
            try:
                await response.write(b'data: [DONE]\n\n')
            except Exception:
                pass
        except Exception as e:
            logger.error(f"【Chatbox 适配器】: (Stream) 队列 {message_id} 发生错误: {e}")
        finally:
            self.pending_requests.pop(message_id, None)
            
        await response.write_eof()
        return response

    async def convert_openai_to_abm(self, body: dict) -> tuple[AstrBotMessage, str]:
        messages = body.get("messages", [])
        if not messages:
            raise ValueError("Missing 'messages' field")

        last_user_msg = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user_msg = msg
                break
        
        if not last_user_msg:
            raise ValueError("No 'user' role message found")

        content = last_user_msg.get("content")
        chain = []
        
        if isinstance(content, str):
            chain.append(Plain(text=content))
        elif isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    chain.append(Plain(text=part.get("text", "")))
                elif part.get("type") == "image_url":
                    img_url = part.get("image_url", {}).get("url", "")
                    if img_url:
                        chain.append(Image(file=img_url))
        
        if not chain:
            raise ValueError("User message content is empty or unsupported")

        abm = AstrBotMessage()
        
        abm.type = MessageType.FRIEND_MESSAGE
        
        user_id = self.spoof_user_id if self.spoof_user_id else body.get("user", self.default_user_id)
        nickname = self.spoof_nickname if self.spoof_nickname else self.default_nickname
        
        abm.session_id = user_id
        abm.sender = MessageMember(user_id=user_id, nickname=nickname)
        
        if self.spoof_platform and self.spoof_self_id:
            abm.self_id = self.spoof_self_id
        else:
            abm.self_id = self.instance_id
        
        abm.message_id = f"chatcmpl-{uuid.uuid4()}"
        abm.message = chain
        abm.message_str = " ".join([p.text for p in chain if isinstance(p, Plain)])
        abm.raw_message = body

        model_name = body.get("model", "astrbot-default-model")
        
        return abm, model_name

    def format_as_openai_response(self, content: str, msg_id: str, model: str, finish_reason: str = "stop", tool_calls: list = None) -> dict:
        message = {
            "role": "assistant",
            "content": content if not tool_calls else None,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        return {
            "id": msg_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }

    def format_as_openai_chunk(self, delta: dict, msg_id: str, model: str) -> dict:
        choice_delta = {}
        if "content" in delta:
            choice_delta = {"role": "assistant", "content": delta["content"]}
        elif "tool_calls" in delta:
            choice_delta = {"role": "assistant", "tool_calls": delta["tool_calls"]}
        
        finish_reason = delta.get("finish_reason")
        
        return {
            "id": msg_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": choice_delta if choice_delta else {}, # 确保 delta 至少是 {}
                    "logprobs": None,
                    "finish_reason": finish_reason
                }
            ]
        }