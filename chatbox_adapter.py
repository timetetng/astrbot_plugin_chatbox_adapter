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

# 导入我们的自定义事件
from .chatbox_event import ChatboxEvent

# --- 新增：默认配置项 ---
DEFAULT_CONFIG = {
    "api_key": "your_secret_key", 
    "port": 8080,
    "host": "127.0.0.1",
    "timeout": 300, # Req 1: 可配置的超时时间 (秒)
    "default_user_id": "chatbox_api_user", # Req 2: 默认 user_id
    "default_nickname": "Chatbox User", # Req 2: 默认昵称
    "spoof_platform": "", # Req 3: 要模拟的平台 (例如 aiocqhttp)
    "spoof_user_id": "", # Req 3: 要模拟的 QQ ID
    "spoof_nickname": "", # Req 3: 模拟的昵称 (可选)
    "spoof_self_id": "" # Req 3 (修复): 您要模拟的适配器实例ID (例如 napcat)
}

@register_platform_adapter("chatbox", "Chatbox (OpenAI API) 适配器", default_config_tmpl=DEFAULT_CONFIG)
class ChatboxAdapter(Platform):

    def __init__(self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue) -> None:
        super().__init__(event_queue)
        self.config = platform_config
        self.settings = platform_settings
        
        # --- 新增：读取所有配置 ---
        self.port = self.config.get('port', 8080)
        self.host = self.config.get('host', '127.0.0.1')
        self.api_key = self.config.get('api_key')
        self.timeout = self.config.get('timeout', 300) 
        self.default_user_id = self.config.get('default_user_id', 'chatbox_api_user') 
        self.default_nickname = self.config.get('default_nickname', 'Chatbox User')
        self.spoof_platform = self.config.get('spoof_platform') 
        self.spoof_user_id = self.config.get('spoof_user_id') 
        self.spoof_nickname = self.config.get('spoof_nickname') 
        self.spoof_self_id = self.config.get('spoof_self_id') # <-- 修复
        
        # <-- 修复: 获取此适配器在UI上的 "机器人名称(id)"
        self.instance_id = self.settings.get('id', 'chatbox') 
        # --- 结束 ---
        
        self.pending_requests = {}
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata("chatbox", "Chatbox (OpenAI API) 适配器")

    async def send_by_session(self, session: MessageSesion, message_chain: MessageChain):
        logger.warning("ChatboxAdapter 不支持主动消息 (send_by_session)")
        pass

    async def run(self):
        app = web.Application()
        app.router.add_get("/v1/models", self.handle_list_models)
        app.router.add_post("/v1/chat/completions", self.handle_chat_completions)
        
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        
        # --- 修复 1: 强制 reuse_port ---
        # 允许新实例在旧实例未完全释放端口时抢占端口
        self.site = web.TCPSite(
            self.runner, 
            self.host, 
            self.port, 
            reuse_address=True, 
            reuse_port=True # <-- 保留此项
        )
        
        logger.info(f"Chatbox (OpenAI API) 适配器尝试在 http://{self.host}:{self.port} 上监听...")
        
        if not self.site:
             logger.error("Chatbox 适配器: site 未初始化")
             return
            
        try:
            await self.site.start()
            logger.info(f"Chatbox (OpenAI API) 适配器成功在 http://{self.host}:{self.port} 上监听。")

            # 服务器成功启动后，保持运行
            while True:
                await asyncio.sleep(3600)
                
        except asyncio.CancelledError:
            logger.info("Chatbox 适配器 run 任务被取消...")
        except OSError as e:
            # 增加对 [Errno 98] 的特定提示
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
                    # --- 修复 2: 强制 cleanup 超时 ---
                    # aiohttp.cleanup() 可能会因为活动连接而挂起。
                    # 我们给它一个很短的超时 (例如 3 秒) 来尝试正常关闭。
                    # 如果超时，它将引发 TimeoutError，我们捕获它并继续，
                    # 允许 finally 块退出，以便任务可以死亡。
                    logger.info("Chatbox 适配器: 正在尝试优雅关闭 (3秒超时)...")
                    await asyncio.wait_for(self.runner.cleanup(), timeout=3.0)
                    logger.info(f"Chatbox (OpenAI API) 适配器已在 http://{self.host}:{self.port} 上停止 (优雅)")
                except asyncio.TimeoutError:
                    logger.warning(f"Chatbox 适配器: cleanup() 在 {self.host}:{self.port} 上超时。强制终止。")
                    # 超时后，我们不再等待，直接退出 finally
                except Exception as e:
                    logger.error(f"Chatbox 适配器停止失败: {e}")
            
            logger.info(f"Chatbox 适配器: {self.host}:{self.port} 的 finally 块执行完毕。")
            self.runner = None
            self.site = None

    async def handle_list_models(self, request: web.Request):
        logger.info("【Chatbox 适配器】: 收到 /v1/models 请求。")
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
                    "id": "astrbot-default",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "astrbot"
                },
                {
                    "id": "gpt-4o-mini",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "astrbot"
                },
                {
                    "id": "gpt-4",
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
            logger.info(f"【Chatbox 适配器】: 收到 /v1/chat/completions 请求: {json.dumps(body)}")
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        is_stream = body.get("stream", False)
        
        try:
            abm, model_name = await self.convert_openai_to_abm(body)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

        response_queue = asyncio.Queue()
        self.pending_requests[abm.message_id] = response_queue

        # --- 身份模拟 (平台) ---
        if self.spoof_platform:
            platform_meta = PlatformMetadata(self.spoof_platform, f"Spoofed {self.spoof_platform}")
            logger.info(f"【Chatbox 适配器】: 身份模拟已激活。平台: {self.spoof_platform}, BotID: {abm.self_id}, 用户ID: {abm.session_id}")
        else:
            platform_meta = self.meta()
        # --- 结束 ---

        message_event = ChatboxEvent(
            message_str=abm.message_str,
            message_obj=abm,
            platform_meta=platform_meta, 
            session_id=abm.session_id,
            client=self,
            is_stream=is_stream,
            model_name=model_name
        )
        
        logger.info(f"【Chatbox 适配器】: 正在提交事件 (commit_event)。 Message_ID: {abm.message_id}")
        self.commit_event(message_event)
        logger.info(f"【Chatbox 适配器】: 事件提交完毕。正在等待队列响应 (is_stream={is_stream})。")

        # --- 心跳 ---
        if is_stream:
            try:
                logger.info("【Chatbox 适配器】: 发送流式“心跳”块以保持连接。")
                empty_chunk = self.format_as_openai_chunk({}, abm.message_id, model_name)
                # 使用 asyncio.create_task 确保心跳发送不会阻塞后续处理
                asyncio.create_task(self.safe_queue_put(response_queue, empty_chunk)) 
            except Exception as e:
                logger.warning(f"【Chatbox 适配器】: 发送心跳块失败: {e}")
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
            logger.info(f"【Chatbox 适配器】: (Non-Stream) 等待队列 {message_id} ...")
            final_response = await asyncio.wait_for(queue.get(), timeout=self.timeout) 
            logger.info(f"【Chatbox 适配器】: (Non-Stream) 收到队列数据，正在返回。")
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
        logger.info(f"【Chatbox 适配器】: (Stream) 开始监听队列 {message_id} ...")

        try:
            while True:
                chunk = await asyncio.wait_for(queue.get(), timeout=self.timeout)
                if chunk == "[DONE]":
                    logger.info(f"【Chatbox 适配器】: (Stream) 收到 [DONE] 标记。")
                    await response.write(b'data: [DONE]\n\n')
                    break
                
                # 跳过可能是心跳的空块
                if chunk.get("choices") and chunk["choices"][0].get("delta") == {}:
                    logger.info("【Chatbox 适配器】: (Stream) 跳过空的心跳块。")
                    continue
                    
                logger.info(f"【Chatbox 适配器】: (Stream) 收到数据块，正在发送: {chunk}")
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
        
        # --- 身份模拟 (用户) ---
        user_id = self.spoof_user_id if self.spoof_user_id else body.get("user", self.default_user_id)
        nickname = self.spoof_nickname if self.spoof_nickname else self.default_nickname
        
        abm.session_id = user_id
        abm.sender = MessageMember(user_id=user_id, nickname=nickname) 
        # --- 结束 ---
        
        # --- Bot ID (self_id) 逻辑 ---
        if self.spoof_platform and self.spoof_self_id:
            abm.self_id = self.spoof_self_id
        else:
            abm.self_id = self.instance_id
        # --- 修复结束 ---
        
        abm.message_id = f"chatcmpl-{uuid.uuid4()}"
        abm.message = chain
        abm.message_str = " ".join([p.text for p in chain if isinstance(p, Plain)])
        abm.raw_message = body

        model_name = body.get("model", "astrbot-default-model")
        
        logger.info(f"【Chatbox 适配器】: 转换消息成功 (类型: {abm.type}, BotID: {abm.self_id}, UserID: {abm.session_id}): {abm.message_str}")
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