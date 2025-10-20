from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.platform import AstrBotMessage, PlatformMetadata
from astrbot.api.message_components import Plain, Image
from astrbot.api import logger 

import typing
import os
import mimetypes
import uuid
import datetime
from urllib.parse import quote
import asyncio # 确保导入 asyncio

if typing.TYPE_CHECKING:
    from .chatbox_adapter import ChatboxAdapter 

class ChatboxEvent(AstrMessageEvent):
    def __init__(self, 
                 message_str: str, 
                 message_obj: AstrBotMessage, 
                 platform_meta: PlatformMetadata, 
                 session_id: str, 
                 client: "ChatboxAdapter", 
                 is_stream: bool, 
                 model_name: str):
        
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.client = client 
        self.is_stream = is_stream
        self.model_name = model_name
        # aggregated_content 现在用于非流式模式的聚合
        self.aggregated_content = "" 
        
    async def send(self, message: MessageChain):
        req_id = self.message_obj.message_id
        queue = self.client.pending_requests.get(req_id)
        
        if not queue:
            # 此时，队列不存在可能是因为适配器已超时并主动关闭
            # 这现在是 DEBUG 消息，因为它在正常超时后是预期行为
            logger.debug(f"【Chatbox 事件】: 'send' 找不到队列 (或队列已超时关闭)。 Message_ID: {req_id}")
            await super().send(message)
            return 

        # reply_content 是本次 send 调用的 *新* 内容
        reply_content = ""
        unhandled_components = [] 

        for i in message.chain:
            if isinstance(i, Plain):
                reply_content += i.text
            elif isinstance(i, Image):
                img_url = i.file
                if img_url:
                    # --- MinIO 逻辑 (来自上次修改) ---
                    if img_url.startswith("file:///"):
                        if self.client.minio_client:
                            try:
                                img_url = await self.upload_local_image_to_minio(img_url)
                            except Exception as e:
                                logger.error(f"【Chatbox 事件】: MinIO 上传失败: {e}")
                                logger.warning(f"Chatbox: 不支持本地图片路径 (MinIO上传失败): {i.file}")
                                reply_content += f"\n[图片发送失败: {os.path.basename(i.file)}]\n"
                        else:
                            logger.warning(f"Chatbox: 不支持本地图片路径: {img_url} (请在配置中启用 MinIO 以上传本地图片)")
                            reply_content += f"\n[图片发送失败: {os.path.basename(i.file)}]\n"
                    
                    if not img_url.startswith("file:///"):
                        reply_content += f"\n![Image]({img_url})\n"
                # --- MinIO 逻辑结束 ---
            else:
                unhandled_components.append(type(i).__name__)
        
        if not reply_content.strip() and unhandled_components:
            logger.warning(f"【Chatbox 事件】: 回复只包含不支持的组件 {unhandled_components}。正在发送兜底消息。")
            reply_content = f"[Astrbot 发送了不支持的内容: {', '.join(unhandled_components)}]"
        
        if not reply_content.strip():
            logger.warning("【Chatbox 事件】: 'send' 被调用，但消息链为空或无法处理。")
            await super().send(message)
            return # 不发送任何内容到队列

        if self.is_stream:
            # --- 流式：只发送新内容块 ---
            # **重要：不再发送 [DONE] 或 stop_chunk**
            chunk = self.client.format_as_openai_chunk(
                {"content": reply_content}, 
                req_id,
                self.model_name
            )
            try:
                await queue.put(chunk)
            except Exception as e:
                logger.warning(f"【Chatbox 事件】: (Stream) 写入队列失败 (可能已关闭): {e}")
        else:
            # --- 非流式：聚合内容并发送*完整的*新响应 ---
            self.aggregated_content += reply_content + "\n"
            
            response = self.client.format_as_openai_response(
                self.aggregated_content.strip(),
                req_id,
                self.model_name,
                finish_reason="stop" # 'stop' 是 OpenAI 非流式响应的标准
            )
            try:
                # 每次都发送一个*更新后的*完整响应
                await queue.put(response)
            except Exception as e:
                logger.warning(f"【Chatbox 事件】: (Non-Stream) 写入队列失败 (可能已关闭): {e}")

        await super().send(message)

    async def upload_local_image_to_minio(self, file_uri: str) -> str:
        """ 帮助函数：上传本地图片到 MinIO 并返回可访问的 URL """
        
        local_path = file_uri[7:] # 去掉 "file://"
        if not os.path.exists(local_path):
            logger.error(f"【Chatbox MinIO】: 本地文件不存在: {local_path}")
            raise FileNotFoundError(f"File not found: {local_path}")
        
        # 1. 准备对象名称和内容类型
        file_name = os.path.basename(local_path)
        # 创建一个唯一的对象名称，避免冲突
        object_name = f"chatbox_adapter/{uuid.uuid4()}/{file_name}"
        
        content_type, _ = mimetypes.guess_type(local_path)
        if not content_type:
            content_type = 'application/octet-stream' # 默认类型

        logger.debug(f"【Chatbox MinIO】: 正在上传: {local_path} -> {self.client.minio_bucket}/{object_name} ({content_type})")

        # 2. 上传 (fput_object)
        self.client.minio_client.fput_object(
            self.client.minio_bucket,
            object_name,
            local_path,
            content_type=content_type
        )
        
        # 3. 生成 URL
        if self.client.minio_use_presigned_url:
            # 生成预签名 URL
            expires_delta = datetime.timedelta(hours=self.client.minio_expires_hours)
            url = self.client.minio_client.presigned_get_object(
                self.client.minio_bucket,
                object_name,
                expires=expires_delta
            )
            logger.debug(f"【Chatbox MinIO】: 上传成功 (预签名 URL): {url}")
        else:
            # 生成公开 URL (基于配置)
            protocol = "https" if self.client.minio_secure else "http"
            # 需要对对象名称进行 URL 编码
            encoded_object_name = quote(object_name)
            url = f"{protocol}://{self.client.minio_endpoint}/{self.client.minio_bucket}/{encoded_object_name}"
            logger.debug(f"【Chatbox MinIO】: 上传成功 (公开 URL): {url}")
            
        return url