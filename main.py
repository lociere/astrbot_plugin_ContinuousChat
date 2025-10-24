import asyncio
import json
import re
import time
from typing import Dict, Tuple, Optional, List
from dataclasses import dataclass
from asyncio import Lock

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, filter, MessageEventResult
from astrbot.api.star import Star, register, Context
from astrbot.api import logger
from astrbot.core.conversation_mgr import Conversation


@dataclass
class UserSession:
    """用户沉浸式对话会话数据"""
    user_id: str
    group_id: str
    start_time: float
    last_activity: float
    message_count: int = 0
    context_messages: List[Dict] = None
    timer: Optional[asyncio.TimerHandle] = None
    persona_prompt: str = ""  # 新增：存储人格提示词
    
    def __post_init__(self):
        if self.context_messages is None:
            self.context_messages = []


# 注册插件
register(
    ContinuousDialoguePlugin,
    "continuous_dialogue_plugin",
    "assistant",
    "智能连续对话插件，为用户提供沉浸式对话体验",
    "1.0.0"
)


class ContinuousDialoguePlugin(Star):
    """连续对话插件 - 基于AstrBot插件开发规范优化"""
    
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # 从配置中读取参数
        self.enable_plugin = self.config.get("enable_plugin", True)
        self.session_timeout = self.config.get("session_timeout", 300)
        self.max_session_messages = self.config.get("max_session_messages", 20)
        self.auto_start_on_mention = self.config.get("auto_start_on_mention", True)
        self.judgment_threshold = self.config.get("judgment_threshold", 0.7)
        self.enable_commands = self.config.get("enable_commands", ["开始对话", "连续对话", "开启对话"])
        
        # 会话管理
        self.user_sessions: Dict[Tuple[str, str], UserSession] = {}
        self.session_lock = Lock()
        
        logger.info("连续对话插件初始化完成")

    def _should_start_session(self, event: AstrMessageEvent) -> bool:
        """判断是否应该开始沉浸式对话"""
        if not self.enable_plugin:
            return False
            
        # 检查是否被@（如果启用）
        if self.auto_start_on_mention and event.is_at_or_wake_command:
            return True
            
        # 检查是否包含开始命令
        message_content = event.message_str.strip()
        start_commands = [cmd for cmd in self.enable_commands if cmd in message_content]
        
        return bool(start_commands)

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
            
            # 获取当前人格提示词
            session.persona_prompt = await self._get_persona_prompt(event)
            
            self.user_sessions[session_key] = session
            
            # 获取对话历史作为上下文
            await self._load_conversation_context(event, session)
            
            logger.info(f"为用户 {user_id} 开启沉浸式对话会话，超时时间: {self.session_timeout}秒")
            return True

    async def _get_persona_prompt(self, event: AstrMessageEvent) -> str:
        """获取当前对话的人格提示词"""
        try:
            # 获取当前对话
            uid = event.unified_msg_origin
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
            if not curr_cid:
                return ""
                
            conversation = await self.context.conversation_manager.get_conversation(uid, curr_cid)
            if not conversation:
                return ""
                
            # 获取人格ID
            persona_id = conversation.persona_id
            
            # 处理特殊值
            if persona_id == "[%None]":  # 用户显式取消人格
                return ""
                
            if not persona_id:  # 如果为None，使用默认人格
                default_persona = self.context.provider_manager.selected_default_persona
                persona_id = default_persona.get("name") if default_persona else ""
                
            if not persona_id:
                return ""
                
            # 从人格列表中查找对应的人格
            for persona in self.context.provider_manager.personas:
                if persona.get("name") == persona_id:
                    return persona.get("prompt", "")
                    
            return ""
            
        except Exception as e:
            logger.error(f"获取人格提示词失败: {e}")
            return ""

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
            
            # 构建判断提示词（包含人格信息）
            judgment_prompt = f"""
你正在与用户进行连续对话。请判断是否应该回复用户的最新消息。

## 机器人角色设定：
{session.persona_prompt if session.persona_prompt else "默认角色：智能助手"}

## 对话历史（最近{len(current_context)}条）：
{json.dumps(current_context, ensure_ascii=False, indent=2) if current_context else "无历史记录"}

## 用户最新消息：
{user_message}

## 判断要求：
请基于上述机器人角色设定，分析用户的意图和对话的连贯性，判断是否需要回复。

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
        """生成回复内容（包含人格设定）"""
        try:
            user_message = event.message_str
            current_context = session.context_messages.copy()
            
            # 添加当前用户消息到上下文
            current_context.append({"role": "user", "content": user_message})
            
            # 使用大模型生成回复（包含人格设定）
            provider = self.context.get_using_provider()
            if not provider:
                return "抱歉，我现在无法回复您。"
            
            # 构建系统提示词（包含人格设定）
            system_prompt = ""
            if session.persona_prompt:
                system_prompt = f"你是一个AI助手，请按照以下角色设定进行回复：\n\n{session.persona_prompt}"
            
            llm_response = await provider.text_chat(
                prompt=user_message,
                contexts=current_context,
                system_prompt=system_prompt
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
        """处理群消息 - 使用事件监听器"""
        if not self.enable_plugin:
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
            async for result in self._handle_in_session_message(event, session_key):
                yield result
        else:
            # 检查是否应该开始新会话
            if self._should_start_session(event):
                success = await self._start_user_session(event)
                if success:
                    # 发送开始提示
                    start_msg = "🎯 已开启连续对话模式！您可以继续与我对话，我会智能判断是否回复。输入'结束对话'可随时退出。"
                    yield event.plain_result(start_msg)
                    
                    # 处理当前触发消息
                    async for result in self._handle_in_session_message(event, session_key):
                        yield result

    async def _handle_in_session_message(self, event: AstrMessageEvent, session_key: Tuple[str, str]):
        """处理会话中的消息 - 修复：使用异步生成器"""
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
                   f"置信度: {judgment_result.get('confidence', 0):.2f}")
        
        if judgment_result["should_reply"]:
            # 生成并发送回复
            bot_reply = await self._generate_reply(event, session)
            
            # 使用消息链构建更丰富的回复
            reply_chain = [
                Comp.Plain(text=bot_reply)
            ]
            
            yield event.chain_result(reply_chain)
            
            # 更新对话历史
            await self._update_conversation_history(event, session, user_message, bot_reply)
        else:
            # 不回复，检查是否需要结束会话
            confidence = judgment_result.get("confidence", 0)
            if confidence > self.judgment_threshold:
                async with self.session_lock:
                    await self._close_user_session(session_key)
                
                end_reason = judgment_result.get("reason", "对话自然结束")
                yield event.plain_result(f"💤 检测到对话结束信号: {end_reason}\n连续对话已自动结束。")

    @filter.command("对话状态")
    async def show_session_status(self, event: AstrMessageEvent):
        """显示当前对话状态"""
        if not self.enable_plugin:
            yield event.plain_result("❌ 插件未启用")
            return
            
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        session_key = (group_id, user_id)
        
        async with self.session_lock:
            in_session = session_key in self.user_sessions
            
            if in_session:
                session = self.user_sessions[session_key]
                duration = int(time.time() - session.start_time)
                
                # 获取人格名称
                persona_name = "默认人格"
                if session.persona_prompt:
                    # 尝试从人格提示词中提取人格名称
                    match = re.search(r"(?:名称|名字|角色)[:：]\s*([^\n]+)", session.persona_prompt)
                    if match:
                        persona_name = match.group(1).strip()
                
                status_info = f"""
🔮 连续对话状态
├── 状态: 🟢 进行中
├── 人格: {persona_name}
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
        if not self.enable_plugin:
            yield event.plain_result("❌ 插件未启用")
            return
            
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        session_key = (group_id, user_id)
        
        async with self.session_lock:
            if session_key in self.user_sessions:
                await self._close_user_session(session_key)
                yield event.plain_result("👋 已结束连续对话")
            else:
                yield event.plain_result("💤 您当前没有进行中的连续对话")

    @filter.command("开始对话")
    async def start_session_command(self, event: AstrMessageEvent):
        """通过命令开启对话会话"""
        if not self.enable_plugin:
            yield event.plain_result("❌ 插件未启用")
            return
            
        success = await self._start_user_session(event)
        if success:
            yield event.plain_result("🎯 已开启连续对话模式！")
        else:
            yield event.plain_result("❌ 开启连续对话失败")

    async def terminate(self):
        """插件卸载时清理资源"""
        logger.info("正在清理连续对话插件...")
        
        async with self.session_lock:
            for session_key in list(self.user_sessions.keys()):
                await self._close_user_session(session_key)
                
        logger.info("连续对话插件清理完成")

