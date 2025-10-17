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
        
        # 移除了 is_finalized
        
    async def send(self, message: MessageChain):
        logger.info(f"【Chatbox 事件】: 'send' 方法被调用。Message_ID: {self.message_obj.message_id}")
        logger.info(f"【Chatbox 事件】: 原始消息链: {message.chain}")

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
        
        # --- 关键修复：[DONE] 逻辑在这里 ---
        
        if not reply_content:
            logger.warning("【Chatbox 事件】: 'send' 被调用，但消息链为空或无法处理。")
            # 即使为空，也要关闭流
            if self.is_stream:
                try:
                    logger.info(f"【Chatbox 事件】: (Stream) 发送空内容的 'stop' 和 '[DONE]'。")
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
            logger.info(f"【Chatbox 事件】: (Stream) 正在向队列 {req_id} 放入数据块: {reply_content[:20]}...")
            chunk = self.client.format_as_openai_chunk(
                {"content": reply_content}, 
                req_id,
                self.model_name
            )
            try:
                await queue.put(chunk)
                
                # 假设这是最后一块数据，立即发送 stop 和 [DONE]
                logger.info(f"【Chatbox 事件】: (Stream) 正在发送 'stop' 和 '[DONE]'。")
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
            logger.info(f"【Chatbox 事件】: (Non-Stream) 正在聚合内容: {reply_content[:20]}...")
            self.aggregated_content += reply_content + "\n"
            
            # 非流式，我们假设这是最终回复，直接 put
            logger.info("【Chatbox 事件】: (Non-Stream) 正在发送聚合响应。")
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
        # --- 修复结束 ---

        await super().send(message)