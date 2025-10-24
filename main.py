import asyncio
import json
import re
import time
from typing import Dict, Tuple, Optional
from dataclasses import dataclass
from asyncio import Lock
from collections import defaultdict

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star, register
from astrbot.api import logger


@dataclass
class UserSession:
    """用户沉浸式对话会话数据"""
    user_id: str
    group_id: str
    start_time: float
    last_activity: float
    message_count: int = 0
    context_messages: list = None
    timer: Optional[asyncio.TimerHandle] = None
    
    def __post_init__(self):
        if self.context_messages is None:
            self.context_messages = []


@register(
    "astrbot_plugin_ContinuousChat",
    "lociere",
    "智能连续对话插件，为用户提供沉浸式对话体验",
    "1.0.0",
)
class ContinuousDialoguePlugin(Star):
    """连续对话插件"""
    
    def __init__(self, context, config):
        super().__init__(context)
        self.config = config
        
        # 会话管理
        self.user_sessions: Dict[Tuple[str, str], UserSession] = {}  # (group_id, user_id) -> UserSession
        self.session_lock = Lock()
        
        # 配置参数
        self.session_timeout = self.config.get("session_timeout", 300)  # 会话超时时间（秒）
        self.max_session_messages = self.config.get("max_session_messages", 20)  # 最大对话轮数
        self.enable_commands = self.config.get("enable_commands", ["开始对话", "结束对话"])
        self.auto_start_on_mention = self.config.get("auto_start_on_mention", True)
        
        logger.info("连续对话插件初始化完成")

    def _should_start_session(self, event: AstrMessageEvent) -> bool:
        """判断是否应该开始沉浸式对话"""
        # 检查是否被@（如果启用）
        if self.auto_start_on_mention and event.is_at_or_wake_command:
            return True
            
        # 检查是否包含开始命令
        message_content = event.message_str.strip()
        start_commands = [cmd for cmd in self.enable_commands if cmd in message_content]
        
        if start_commands:
            return True
            
        return False

    async def _start_user_session(self, event: AstrMessageEvent) -> bool:
        """为用户开启沉浸式对话会话"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        
        if not group_id or not user_id:
            return False
            
        session_key = (group_id, user_id)
        
        async with self.session_lock:
            # 如果已存在会话，先关闭
            if session_key in self.user_sessions:
                await self._close_user_session(session_key)
            
            # 创建新会话
            current_time = time.time()
            session = UserSession(
                user_id=user_id,
                group_id=group_id,
                start_time=current_time,
                last_activity=current_time
            )
            
            # 设置超时定时器
            session.timer = asyncio.get_event_loop().call_later(
                self.session_timeout,
                lambda: asyncio.create_task(self._handle_session_timeout(session_key))
            )
            
            self.user_sessions[session_key] = session
            
            # 获取对话历史作为上下文
            await self._load_conversation_context(event, session)
            
            logger.info(f"为用户 {user_id} 开启沉浸式对话会话，超时时间: {self.session_timeout}秒")
            return True

    async def _close_user_session(self, session_key: Tuple[str, str]):
        """关闭用户会话"""
        if session_key in self.user_sessions:
            session = self.user_sessions[session_key]
            if session.timer:
                session.timer.cancel()
            del self.user_sessions[session_key]
            logger.info(f"关闭用户 {session_key[1]} 的沉浸式对话会话")

    async def _handle_session_timeout(self, session_key: Tuple[str, str]):
        """处理会话超时"""
        async with self.session_lock:
            if session_key in self.user_sessions:
                session = self.user_sessions[session_key]
                logger.info(f"用户 {session_key[1]} 的会话已超时，自动关闭")
                await self._close_user_session(session_key)

    async def _load_conversation_context(self, event: AstrMessageEvent, session: UserSession):
        """加载对话历史上下文"""
        try:
            uid = event.unified_msg_origin
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
            if not curr_cid:
                return
                
            conversation = await self.context.conversation_manager.get_conversation(uid, curr_cid)
            if conversation and conversation.history:
                history_data = json.loads(conversation.history)
                # 只保留最近的几条消息作为上下文
                session.context_messages = history_data[-5:]  # 最近5条消息
                
        except Exception as e:
            logger.error(f"加载对话上下文失败: {e}")

    async def _judge_should_reply(self, event: AstrMessageEvent, session: UserSession) -> dict:
        """使用大模型判断是否应该回复"""
        try:
            user_message = event.message_str
            current_context = session.context_messages.copy()
            
            # 构建判断提示词
            judgment_prompt = f"""
你正在与用户进行连续对话。请判断是否应该回复用户的最新消息。

## 对话历史（最近{len(current_context)}条）：
{json.dumps(current_context, ensure_ascii=False, indent=2) if current_context else "无历史记录"}

## 用户最新消息：
{user_message}

## 判断要求：
请分析用户的意图和对话的连贯性，判断是否需要回复。

**回复条件（满足以下任一条件即可回复）：**
1. 用户的问题需要回答
2. 对话需要继续推进
3. 用户的发言有明显的互动意图
4. 对话内容与当前话题相关

**不回复条件（满足以下任一条件则不回复）：**
1. 用户的发言是结束对话的信号（如"再见"、"结束"等）
2. 发言与当前话题完全无关且无互动价值
3. 用户明显是在自言自语
4. 发言内容无意义或无法理解

请以JSON格式回复：
{{
    "should_reply": true/false,
    "reason": "判断理由的详细说明",
    "confidence": 0.0-1.0的置信度
}}

**重要：必须返回纯JSON格式，不要包含其他内容！**
"""
            
            provider = self.context.get_using_provider()
            if not provider:
                return {"should_reply": False, "reason": "无可用大模型", "confidence": 0.0}
            
            # 调用大模型进行判断
            llm_response = await provider.text_chat(
                prompt=judgment_prompt,
                contexts=[]  # 不使用额外上下文
            )
            
            response_text = llm_response.completion_text.strip()
            
            # 提取JSON
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                judgment_data = json.loads(json_match.group())
                return judgment_data
            else:
                logger.warning(f"大模型返回格式异常: {response_text}")
                return {"should_reply": False, "reason": "响应格式错误", "confidence": 0.0}
                
        except Exception as e:
            logger.error(f"判断是否回复时发生错误: {e}")
            return {"should_reply": False, "reason": f"判断错误: {str(e)}", "confidence": 0.0}

    async def _generate_reply(self, event: AstrMessageEvent, session: UserSession) -> str:
        """生成回复内容"""
        try:
            user_message = event.message_str
            current_context = session.context_messages.copy()
            
            # 添加当前用户消息到上下文
            current_context.append({"role": "user", "content": user_message})
            
            # 使用大模型生成回复
            provider = self.context.get_using_provider()
            if not provider:
                return "抱歉，我现在无法回复您。"
            
            # 构建生成提示词
            generation_prompt = f"""
请基于对话历史，自然地回复用户的最新消息。保持对话的连贯性和友好性。

当前对话上下文：
{json.dumps(current_context, ensure_ascii=False, indent=2) if current_context else "这是对话的开始"}

请生成一个自然、连贯的回复：
"""
            
            llm_response = await provider.text_chat(
                prompt=generation_prompt,
                contexts=current_context
            )
            
            return llm_response.completion_text.strip()
            
        except Exception as e:
            logger.error(f"生成回复时发生错误: {e}")
            return "抱歉，我暂时无法处理您的消息。"

    async def _update_conversation_history(self, event: AstrMessageEvent, session: UserSession, 
                                         user_message: str, bot_reply: str):
        """更新对话历史"""
        try:
            # 更新会话上下文
            session.context_messages.extend([
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": bot_reply}
            ])
            
            # 限制上下文长度
            if len(session.context_messages) > self.max_session_messages * 2:
                session.context_messages = session.context_messages[-self.max_session_messages * 2:]
                
            # 更新到对话管理器
            uid = event.unified_msg_origin
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
            if curr_cid:
                conversation = await self.context.conversation_manager.get_conversation(uid, curr_cid)
                if conversation:
                    # 合并历史记录
                    try:
                        existing_history = json.loads(conversation.history) if conversation.history else []
                        updated_history = existing_history + [
                            {"role": "user", "content": user_message},
                            {"role": "assistant", "content": bot_reply}
                        ]
                        conversation.history = json.dumps(updated_history, ensure_ascii=False)
                        await self.context.conversation_manager.update_conversation(uid, curr_cid, updated_history)
                    except Exception as e:
                        logger.error(f"更新对话历史失败: {e}")
                        
        except Exception as e:
            logger.error(f"更新对话历史时发生错误: {e}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """处理群消息"""
        if not self.config.get("enable_plugin", True):
            return
            
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        
        # 跳过机器人自己的消息
        if user_id == event.get_self_id():
            return
            
        session_key = (group_id, user_id)
        
        # 检查是否在沉浸式对话中
        async with self.session_lock:
            in_session = session_key in self.user_sessions
        
        if in_session:
            # 处理沉浸式对话中的消息
            await self._handle_in_session_message(event, session_key)
        else:
            # 检查是否应该开始新会话
            if self._should_start_session(event):
                success = await self._start_user_session(event)
                if success:
                    # 发送开始提示
                    start_msg = "🎯 已开启连续对话模式！您可以继续与我对话，我会智能判断是否回复。输入'结束对话'可随时退出。"
                    yield event.plain_result(start_msg)
                    
                    # 处理当前触发消息
                    await self._handle_in_session_message(event, session_key)

    async def _handle_in_session_message(self, event: AstrMessageEvent, session_key: Tuple[str, str]):
        """处理会话中的消息"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        user_message = event.message_str.strip()
        
        # 检查结束命令
        if any(end_cmd in user_message for end_cmd in ["结束对话", "退出对话", "结束"]):
            async with self.session_lock:
                await self._close_user_session(session_key)
            yield event.plain_result("👋 已结束连续对话，期待下次与您交流！")
            return
        
        async with self.session_lock:
            if session_key not in self.user_sessions:
                return
                
            session = self.user_sessions[session_key]
            session.last_activity = time.time()
            session.message_count += 1
            
            # 重置超时定时器
            if session.timer:
                session.timer.cancel()
            session.timer = asyncio.get_event_loop().call_later(
                self.session_timeout,
                lambda: asyncio.create_task(self._handle_session_timeout(session_key))
            )
        
        # 使用大模型判断是否回复
        judgment_result = await self._judge_should_reply(event, session)
        
        logger.info(f"连续对话判断结果 - 用户: {user_id}, 回复: {judgment_result['should_reply']}, "
                   f"置信度: {judgment_result.get('confidence', 0):.2f}, 理由: {judgment_result.get('reason', '')}")
        
        if judgment_result["should_reply"]:
            # 生成并发送回复
            bot_reply = await self._generate_reply(event, session)
            yield event.plain_result(bot_reply)
            
            # 更新对话历史
            await self._update_conversation_history(event, session, user_message, bot_reply)
        else:
            # 不回复，检查是否需要结束会话
            confidence = judgment_result.get("confidence", 0)
            if confidence > 0.7:  # 高置信度判断不需要回复时，结束会话
                async with self.session_lock:
                    await self._close_user_session(session_key)
                
                end_reason = judgment_result.get("reason", "对话自然结束")
                yield event.plain_result(f"💤 检测到对话结束信号: {end_reason}\n连续对话已自动结束。")

    @filter.command("对话状态")
    async def show_session_status(self, event: AstrMessageEvent):
        """显示当前对话状态"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        session_key = (group_id, user_id)
        
        async with self.session_lock:
            in_session = session_key in self.user_sessions
            
            if in_session:
                session = self.user_sessions[session_key]
                duration = int(time.time() - session.start_time)
                status_info = f"""
🔮 连续对话状态
├── 状态: 🟢 进行中
├── 持续时间: {duration}秒
├── 消息数量: {session.message_count}条
├── 最后活动: {int(time.time() - session.last_activity)}秒前
└── 超时时间: {self.session_timeout}秒后自动结束
                """
            else:
                status_info = "🔮 连续对话状态: 🔴 未开启\n使用'开始对话'或@我来开启连续对话模式"
        
        yield event.plain_result(status_info)

    @filter.command("结束对话")
    async def end_session_command(self, event: AstrMessageEvent):
        """手动结束对话会话"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        session_key = (group_id, user_id)
        
        async with self.session_lock:
            if session_key in self.user_sessions:
                await self._close_user_session(session_key)
                yield event.plain_result("👋 已结束连续对话")
            else:
                yield event.plain_result("💤 您当前没有进行中的连续对话")

    async def terminate(self):
        """插件卸载时清理资源"""
        logger.info("正在清理连续对话插件...")
        
        async with self.session_lock:
            for session_key in list(self.user_sessions.keys()):
                await self._close_user_session(session_key)
                
        logger.info("连续对话插件清理完成")
