import json
import uuid
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import LLMResponse
from astrbot.api import logger

# --- 顶层导入，处理重载错误 ---
try:
    from .chatbox_adapter import ChatboxAdapter # noqa
except ValueError as e:
    if "已经注册过了" in str(e):
        logger.info("Chatbox 适配器已注册 (重载)。")
    else:
        logger.error(f"Chatbox 适配器加载失败: {e}")
        logger.exception(e)
except ImportError:
    pass 
except Exception as e:
    logger.error(f"Chatbox 适配器加载失败: {e}")
    logger.exception(e)

try:
    from .chatbox_event import ChatboxEvent
except ImportError:
    ChatboxEvent = AstrMessageEvent
except Exception as e:
    logger.warning(f"导入 chatbox_event 失败: {e}")
    ChatboxEvent = AstrMessageEvent


@register("astrbot_plugin_chatbox_adapter", "timetetng", "提供 OpenAI API 兼容接口的 Chatbox 适配器", "1.0", "https://github.com/timetetng/astrbot_plugin_chatbox_adapter")
class ChatboxPlugin(Star):
    
    def __init__(self, context: Context):
        super().__init__(context)
        logger.info("Chatbox 插件 (钩子) 加载成功。")
            
    # (ping 指令用于测试)
    @filter.command("ping", priority=200) 
    async def handle_ping(self, event: AstrMessageEvent):
        if isinstance(event, ChatboxEvent):
            logger.info("【Chatbox 插件】: 内置 /ping 指令被触发。")
            yield event.plain_result("pong (from chatbox adapter)")

    # 钩子1: 拦截 LLM 的 tool_calls 响应 (这个仍然需要)
    @filter.on_llm_response(priority=100) # 高优先级
    async def intercept_tool_calls(self, event: AstrMessageEvent, resp: LLMResponse):
        if not isinstance(event, ChatboxEvent):
            return

        logger.info(f"【Chatbox 钩子】: 'on_llm_response' 触发。 Role: {resp.role}")

        if resp.role == "tool" and resp.tools_call_name:
            adapter = event.client
            queue = adapter.pending_requests.get(event.message_obj.message_id)
            if not queue:
                logger.warning("【Chatbox 钩子】: 'on_llm_response' 找不到队列")
                return

            logger.info(f"【Chatbox 钩子】: 拦截到 tool_calls: {resp.tools_call_name}")
            event.stop_event() 
            
            # --- 关键修复：设置标志位 ---
            event.is_finalized = True 
            # --- 修复结束 ---

            openai_tool_calls = self.convert_astrbot_tools_to_openai(resp)

            if event.is_stream:
                chunk = adapter.format_as_openai_chunk(
                    {"tool_calls": openai_tool_calls},
                    event.message_obj.message_id,
                    event.model_name
                )
                await queue.put(chunk)
                stop_chunk = adapter.format_as_openai_chunk(
                    {"finish_reason": "tool_calls"},
                    event.message_obj.message_id,
                    event.model_name
                )
                await queue.put(stop_chunk)
                await queue.put("[DONE]") 
            else:
                response = adapter.format_as_openai_response(
                    None,
                    event.message_obj.message_id,
                    event.model_name,
                    finish_reason="tool_calls",
                    tool_calls=openai_tool_calls
                )
                await queue.put(response)

    @filter.after_message_sent(priority=-100) # 低优先级，最后执行
    async def finalize_response(self, event: AstrMessageEvent):
        if not isinstance(event, ChatboxEvent):
            return
            
        logger.info(f"【Chatbox 钩子】: 'after_message_sent' 触发。")
            
        adapter = event.client
        queue = adapter.pending_requests.get(event.message_obj.message_id)
        if not queue:
            logger.warning("【Chatbox 钩子】: 'after_message_sent' 找不到队列。")
            return

        # 检查是否已被 tool_call 钩子终止
        if event.is_finalized:
            logger.info("【Chatbox 钩子】: 事件已被 tool_call 钩子终止，跳过。")
            return
        
        event.is_finalized = True # 标记为已终止

        if event.is_stream:
            logger.info("【Chatbox 钩子】: 正在发送流式 'stop' 块和 [DONE]。")
            stop_chunk = adapter.format_as_openai_chunk(
                {"finish_reason": "stop"},
                event.message_obj.message_id,
                event.model_name
            )
            await queue.put(stop_chunk)
            await queue.put("[DONE]") 
        else:
            logger.info("【Chatbox 钩子】: 正在发送非流式聚合响应。")
            response = adapter.format_as_openai_response(
                event.aggregated_content.strip(),
                event.message_obj.message_id,
                event.model_name,
                finish_reason="stop"
            )
            await queue.put(response)
 
    def convert_astrbot_tools_to_openai(self, resp: LLMResponse) -> list:
        tool_calls = []
        for i, name in enumerate(resp.tools_call_name):
            tool_calls.append({
                "id": resp.tools_call_ids[i] if (resp.tools_call_ids and len(resp.tools_call_ids) > i) else f"call_{uuid.uuid4()}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(resp.tools_call_args[i])
                }
            })
        return tool_calls