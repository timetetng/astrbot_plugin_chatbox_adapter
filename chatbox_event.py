from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.platform import AstrBotMessage, PlatformMetadata
from astrbot.api.message_components import Plain, Image
from astrbot.api import logger 

import typing
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
        self.aggregated_content = "" 
        
    async def send(self, message: MessageChain):
        req_id = self.message_obj.message_id
        queue = self.client.pending_requests.get(req_id)
        
        if not queue:
            logger.error(f"【Chatbox 事件】: 'send' 找不到队列！ Message_ID: {req_id}")
            await super().send(message)
            return 

        reply_content = ""
        unhandled_components = [] 

        for i in message.chain:
            if isinstance(i, Plain):
                reply_content += i.text
            elif isinstance(i, Image):
                img_url = i.file
                if img_url:
                    if img_url.startswith("file:///"):
                        logger.warning(f"Chatbox: 不支持本地图片路径: {img_url}")
                    else:
                        reply_content += f"\n![Image]({img_url})\n"
            else:
                unhandled_components.append(type(i).__name__)
        
        if not reply_content and unhandled_components:
            logger.warning(f"【Chatbox 事件】: 回复只包含不支持的组件 {unhandled_components}。正在发送兜底消息。")
            reply_content = f"[Astrbot 发送了不支持的内容: {', '.join(unhandled_components)}]"
        
        if not reply_content:
            logger.warning("【Chatbox 事件】: 'send' 被调用，但消息链为空或无法处理。")
            if self.is_stream:
                try:
                    stop_chunk = self.client.format_as_openai_chunk(
                        {"finish_reason": "stop"},
                        req_id,
                        self.model_name
                    )
                    await queue.put(stop_chunk)
                    await queue.put("[DONE]")
                except Exception as e:
                    logger.warning(f"【Chatbox 事件】: (Stream) 写入空内容 [DONE] 失败: {e}")
            await super().send(message)
            return

        if self.is_stream:
            chunk = self.client.format_as_openai_chunk(
                {"content": reply_content}, 
                req_id,
                self.model_name
            )
            try:
                await queue.put(chunk)
                
                stop_chunk = self.client.format_as_openai_chunk(
                    {"finish_reason": "stop"},
                    req_id,
                    self.model_name
                )
                await queue.put(stop_chunk)
                await queue.put("[DONE]")

            except Exception as e:
                logger.warning(f"【Chatbox 事件】: (Stream) 写入队列失败 (可能已关闭): {e}")
        else:
            self.aggregated_content += reply_content + "\n"
            
            response = self.client.format_as_openai_response(
                self.aggregated_content.strip(),
                req_id,
                self.model_name,
                finish_reason="stop"
            )
            try:
                await queue.put(response)
            except Exception as e:
                logger.warning(f"【Chatbox 事件】: (Non-Stream) 写入队列失败: {e}")

        await super().send(message)