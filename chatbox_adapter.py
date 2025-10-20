import asyncio
import json
import time
import uuid
import os
import mimetypes
import datetime
from urllib.parse import quote

try:
    from aiohttp import web
except ImportError:
    print("缺少 aiohttp 依赖，请在插件的 requirements.txt 中添加 aiohttp")
    raise

try:
    from minio import Minio
    from minio.error import S3Error
    MINIO_INSTALLED = True
except ImportError:
    MINIO_INSTALLED = False
    print("缺少 minio 依赖，如果启用 MinIO 功能，请在插件的 requirements.txt 中添加 minio")


from astrbot.api.platform import Platform, AstrBotMessage, MessageMember, PlatformMetadata, MessageType, register_platform_adapter
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Image
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.api import logger

# 导入我们的自定义事件
from .chatbox_event import ChatboxEvent

DEFAULT_CONFIG = {
    "api_key": "your_secret_key",
    "port": 8080,
    "host": "127.0.0.1",
    "timeout": 300, # 这是“LLM总超时”，应该设置得较长
    "aggregation_timeout_seconds": 2, # 这是“消息聚合超时”，应该设置得较短
    "default_user_id": "chatbox_api_user",
    "default_nickname": "Chatbox User",
    "spoof_platform": "",
    "spoof_user_id": "",
    "spoof_nickname": "",
    "spoof_self_id": "",
    
    # --- MinIO S3 兼容的对象存储配置 (可选) ---
    "minio_enable": False, # 默认关闭。设为 True 以启用本地图片上传
    "minio_endpoint": "127.0.0.1:9000", # MinIO 服务器地址
    "minio_access_key": "minioadmin",   # Access Key
    "minio_secret_key": "minio123456",   # Secret Key
    "minio_bucket": "images",         # 存储桶
    "minio_secure": False,            # 是否使用 HTTPS (True/False)
    "minio_use_presigned_url": False, # 是否使用预签名 URL (带过期时间)
                                    # False: 使用公开 URL (http://endpoint/bucket/object)
                                    # True: 使用预签名 URL (http://endpoint/bucket/object?...)
    "minio_expires_duration_hours": 24, # 预签名 URL 有效期 (小时)
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
        
        try:
            self.timeout = float(self.config.get('timeout', 300)) # LLM总超时
            self.aggregation_timeout = float(self.config.get('aggregation_timeout_seconds', 2)) # 消息聚合超时
        except (ValueError, TypeError):
            logger.error("【Chatbox 适配器】: 'timeout' 或 'aggregation_timeout_seconds' 配置值无效，必须是数字。")
            self.timeout = 300.0
            self.aggregation_timeout = 2.0
        except Exception as e:
            logger.error(f"【Chatbox 适配器】: 加载超时配置时出错: {e}")
            self.timeout = 300.0
            self.aggregation_timeout = 2.0
        # --- [修复结束] ---

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

        # --- MinIO Client ---
        self.minio_client: "Minio | None" = None
        self.minio_enable = self.config.get('minio_enable', False)
        self.minio_endpoint = self.config.get('minio_endpoint', '127.0.0.1:9000')
        self.minio_access_key = self.config.get('minio_access_key', 'minioadmin')
        self.minio_secret_key = self.config.get('minio_secret_key', 'minio123456')
        self.minio_bucket = self.config.get('minio_bucket', 'images')
        self.minio_secure = self.config.get('minio_secure', False)
        self.minio_use_presigned_url = self.config.get('minio_use_presigned_url', False)
        self.minio_expires_hours = self.config.get('minio_expires_duration_hours', 24)

        if self.minio_enable:
            if not MINIO_INSTALLED:
                logger.error("【Chatbox 适配器】: MinIO 功能已启用，但 'minio' 库未安装。")
                logger.error("【Chatbox 适配器】: 请在 AstrBot 环境中运行: pip install minio")
            else:
                try:
                    self.minio_client = Minio(
                        self.minio_endpoint,
                        access_key=self.minio_access_key,
                        secret_key=self.minio_secret_key,
                        secure=self.minio_secure
                    )
                    # 检查存储桶是否存在
                    found = self.minio_client.bucket_exists(self.minio_bucket)
                    if not found:
                        logger.warning(f"【Chatbox 适配器】: MinIO 存储桶 '{self.minio_bucket}' 不存在。将尝试创建它...")
                        try:
                            self.minio_client.make_bucket(self.minio_bucket)
                            logger.info(f"【Chatbox 适配器】: MinIO 存储桶 '{self.minio_bucket}' 创建成功。")
                        except S3Error as e:
                            logger.error(f"【Chatbox 适配器】: MinIO 存储桶 '{self.minio_bucket}' 自动创建失败: {e}")
                            self.minio_client = None
                    else:
                        logger.info(f"【Chatbox 适配器】: 成功连接到 MinIO，存储桶 '{self.minio_bucket}' 已找到。")
                
                except S3Error as exc:
                    logger.error(f"【Chatbox 适配器】: 连接 MinIO 时出错: {exc}")
                    self.minio_client = None
                except Exception as e:
                    logger.error(f"【Chatbox 适配器】: 初始化 MinIO 客户端时发生未知错误: {e}")
                    self.minio_client = None
    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            "chatbox", 
            "Chatbox (OpenAI API) 适配器",
        )

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
                    "id": "Astrbot",
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
                # 发送一个初始空块，让客户端知道连接已建立
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
        final_response = None
        
        # 嵌套函数，用于被 wait_for 包裹
        async def _responder():
            nonlocal final_response
            # --- 1. 等待第一条消息 ---
            # 这里的 queue.get() 受外层的 self.timeout 限制
            try:
                item = await queue.get()
                if isinstance(item, dict):
                    final_response = item # 存储第一条消息
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"【Chatbox 适配器】: (Non-Stream) 队列 {message_id} 等待第一条消息时出错: {e}")
                raise

            # --- 2. 循环等待后续消息 (聚合) ---
            try:
                while True:
                    # 内层: 聚合超时 (例如 2s)
                    item = await asyncio.wait_for(queue.get(), timeout=self.aggregation_timeout)
                    if isinstance(item, dict):
                        final_response = item # 持续覆盖，只保留最后一个聚合响应
                            
            except asyncio.TimeoutError:
                # --- 正常退出 (聚合超时) ---
                # 内层的 aggregation_timeout 触发，意味着Bot停止发送消息。
                logger.debug(f"【Chatbox 适配器】: (Non-Stream) 队列 {message_id} 聚合超时，准备发送回复。")
                pass # 正常退出
            
            except asyncio.CancelledError:
                logger.debug(f"【Chatbox 适配器】: (Non-Stream) 队列 {message_id} 内部循环被取消。")
                raise

        try:
            # --- 外层: LLM总超时 ---
            # 使用兼容的 asyncio.wait_for 替代 asyncio.timeout
            await asyncio.wait_for(_responder(), timeout=self.timeout)

        except asyncio.TimeoutError:
            # --- 异常退出 (LLM总超时) ---
            logger.warning(f"【Chatbox 适配器】: (Non-Stream) 队列 {message_id} *总超时* (LLM超时)。")
            if not final_response:
                self.pending_requests.pop(message_id, None)
                return web.json_response({"error": f"Request timed out after {self.timeout}s (no first reply)"}, status=504)
            logger.warning(f"【Chatbox 适配器】: (Non-Stream) 队列 {message_id} 返回*部分*聚合回复。")

        except Exception as e:
            logger.error(f"【Chatbox 适配器】: (Non-Stream) 队列 {message_id} 发生未知错误: {e}")
            if not final_response:
                self.pending_requests.pop(message_id, None)
                return web.json_response({"error": "Internal server error"}, status=500)

        finally:
            self.pending_requests.pop(message_id, None)

        # --- 统一出口 ---
        if final_response:
            return web.json_response(final_response)
        else:
            logger.warning(f"【Chatbox 适配器】: (Non-Stream) 队列 {message_id} 未收到任何有效回复。")
            return web.json_response({"error": "No response from bot"}, status=500)

    async def handle_stream_response(self, request: web.Request, message_id: str, queue: asyncio.Queue):
        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache'}
        )
        await response.prepare(request)
        
        model_name = "astrbot-stream" # 默认模型名
        
        # 嵌套函数，用于被 wait_for 包裹
        async def _responder():
            nonlocal model_name
            # --- 1. 等待第一条 *有效* 消息 ---
            try:
                while True:
                    # 这里的 queue.get() 受外层的 self.timeout 限制
                    chunk = await queue.get()
                    if chunk.get("model"):
                        model_name = chunk.get("model")
                    
                    # [修复] 只有在收到 *非空* 块时才算 "第一条消息"
                    # 如果是空的心跳块, (delta == {})，则继续循环
                    if not (chunk.get("choices") and chunk["choices"][0].get("delta") == {}):
                        # 这是一个有效块 (文本, tool_call, 或 stop)
                        chunk_json = json.dumps(chunk)
                        await response.write(f'data: {chunk_json}\n\n'.encode('utf-8'))
                        # 收到有效块，跳出Step 1的循环, 进入Step 2
                        break
                    else:
                        # 是空块 (delta == {})，忽略并继续等待第一条 *有效* 消息
                        logger.debug("【Chatbox 适配器】: (Stream) 收到并忽略了心跳空块")
                        
            except asyncio.CancelledError:
                raise # 如果被取消，直接抛出
            except Exception as e:
                logger.error(f"【Chatbox 适配器】: (Stream) 队列 {message_id} 等待第一条消息时出错: {e}")
                raise # 抛出错误到外层 catch
            
            # --- 2. 循环等待后续消息 (聚合) ---
            try:
                while True:
                    # 内层: 聚合超时 (例如 3s)
                    chunk = await asyncio.wait_for(queue.get(), timeout=self.aggregation_timeout)
                    
                    # (K线图的第二条消息会在这里被捕获)
                    if chunk.get("choices") and chunk["choices"][0].get("delta") == {}:
                        continue # 跳过后续可能的心跳块
                    
                    chunk_json = json.dumps(chunk)
                    await response.write(f'data: {chunk_json}\n\n'.encode('utf-8'))

            except asyncio.TimeoutError:
                # --- 正常退出 (聚合超时) ---
                logger.debug(f"【Chatbox 适配器】: (Stream) 队列 {message_id} 聚合超时，正常关闭流。")
                pass # 正常退出
            
            except asyncio.CancelledError:
                logger.debug(f"【Chatbox 适配器】: (Stream) 队列 {message_id} 内部循环被取消。")
                raise

        try:
            # --- 外层: LLM总超时 ---
            await asyncio.wait_for(_responder(), timeout=self.timeout)

        except asyncio.TimeoutError:
            # --- 异常退出 (LLM总超时) ---
            logger.warning(f"【Chatbox 适配器】: (Stream) 队列 {message_id} *总超时* (LLM超时)。")
            # 同样进入 finally 块发送 [DONE]
            
        except Exception as e:
            logger.error(f"【Chatbox 适配器】: (Stream) 队列 {message_id} 发生未知错误: {e} (in _responder: {type(e)})")
            # 同样进入 finally 块发送 [DONE]

        finally:
            # --- 统一出口：必须关闭客户端流 ---
            try:
                logger.debug(f"【Chatbox 适配器】: (Stream) 队列 {message_id} 正在发送 'stop' 和 [DONE]...")
                # 1. 发送 'stop' 信号块
                stop_chunk = self.format_as_openai_chunk(
                    {"finish_reason": "stop"},
                    message_id,
                    model_name 
                )
                chunk_json = json.dumps(stop_chunk)
                await response.write(f'data: {chunk_json}\n\n'.encode('utf-8'))
                
                # 2. 发送 [DONE] 终止信号
                await response.write(b'data: [DONE]\n\n')
                
            except Exception as e:
                logger.warning(f"【Chatbox 适配器】: (Stream) 写入最终 [DONE] 失败 (客户端可能已提前断开): {e}")
            
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
                    "delta": choice_delta if choice_delta else {},
                    "logprobs": None,
                    "finish_reason": finish_reason
                }
            ]
        }
