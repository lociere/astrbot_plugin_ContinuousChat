import asyncio
import json
import re
import time
import os
import pickle
from typing import Dict, Tuple, Optional, List, Any
from dataclasses import dataclass, asdict
from asyncio import Lock
from datetime import datetime

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, filter, MessageEventResult
from astrbot.api.star import Star, register, Context
from astrbot.api import logger
from astrbot.core.conversation_mgr import Conversation


@dataclass
class ChatHistoryRecord:
    """单条聊天记录"""
    timestamp: float
    role: str  # 'user' 或 'assistant'
    content: str
    message_type: str = 'text'
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ChatHistoryRecord':
        return cls(**data)


@dataclass
class UserChatHistory:
    """用户聊天历史记录"""
    user_id: str
    group_id: str
    records: List[ChatHistoryRecord]
    last_updated: float
    total_messages: int = 0
    
    def __post_init__(self):
        if self.records is None:
            self.records = []
    
    def add_record(self, record: ChatHistoryRecord):
        self.records.append(record)
        self.last_updated = time.time()
        self.total_messages += 1
    
    def get_recent_records(self, count: int) -> List[ChatHistoryRecord]:
        return self.records[-count:] if self.records else []
    
    def clear_old_records(self, max_records: int):
        if len(self.records) > max_records:
            self.records = self.records[-max_records:]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'user_id': self.user_id,
            'group_id': self.group_id,
            'records': [r.to_dict() for r in self.records],
            'last_updated': self.last_updated,
            'total_messages': self.total_messages
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UserChatHistory':
        records = [ChatHistoryRecord.from_dict(r) for r in data.get('records', [])]
        return cls(
            user_id=data['user_id'],
            group_id=data['group_id'],
            records=records,
            last_updated=data['last_updated'],
            total_messages=data.get('total_messages', 0)
        )


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
    persona_prompt: str = ""
    is_new_session: bool = True
    history_records_used: int = 0
    
    def __post_init__(self):
        if self.context_messages is None:
            self.context_messages = []


@register(
    "continuous_dialogue_plugin",
    "assistant",
    "连续对话插件，支持历史记录存储和智能角色设定调用",
    "1.0.0"
)
class ContinuousDialoguePlugin(Star):
    """增强版连续对话插件"""
    
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # 基础配置
        self.enable_plugin = self.config.get("enable_plugin", True)
        self.session_timeout = self.config.get("session_timeout", 300)
        self.max_session_messages = self.config.get("max_session_messages", 20)
        self.auto_start_on_mention = self.config.get("auto_start_on_mention", True)
        self.judgment_threshold = self.config.get("judgment_threshold", 0.7)
        self.enable_commands = self.config.get("enable_commands", ["开始对话", "连续对话", "开启对话"])
        
        # 历史记录配置
        self.enable_history_storage = self.config.get("enable_history_storage", True)
        self.max_history_records = self.config.get("max_history_records", 100)
        self.history_records_to_use = self.config.get("history_records_to_use", 10)
        self.history_storage_path = self.config.get("history_storage_path", "data/continuous_dialogue_history")
        
        # 角色设定配置
        self.enable_persona = self.config.get("enable_persona", True)
        self.use_system_prompt_directly = self.config.get("use_system_prompt_directly", True)
        self.persona_override = self.config.get("persona_override", "")
        
        # 会话管理
        self.user_sessions: Dict[Tuple[str, str], UserSession] = {}
        self.session_lock = Lock()
        
        # 历史记录管理
        self.chat_histories: Dict[Tuple[str, str], UserChatHistory] = {}
        self.history_lock = Lock()
        
        # 创建存储目录
        if self.enable_history_storage:
            os.makedirs(self.history_storage_path, exist_ok=True)
            asyncio.create_task(self._load_all_histories())
        
        logger.info("增强版连续对话插件初始化完成")

    async def _load_all_histories(self):
        """加载所有历史记录"""
        try:
            if not os.path.exists(self.history_storage_path):
                return
                
            history_files = [f for f in os.listdir(self.history_storage_path) if f.endswith('.pkl')]
            loaded_count = 0
            
            for filename in history_files:
                try:
                    filepath = os.path.join(self.history_storage_path, filename)
                    with open(filepath, 'rb') as f:
                        history_data = pickle.load(f)
                    
                    user_history = UserChatHistory.from_dict(history_data)
                    session_key = (user_history.group_id, user_history.user_id)
                    self.chat_histories[session_key] = user_history
                    loaded_count += 1
                    
                except Exception as e:
                    logger.error(f"加载历史记录文件 {filename} 失败: {e}")
            
            logger.info(f"成功加载 {loaded_count} 个用户的历史记录")
            
        except Exception as e:
            logger.error(f"加载历史记录失败: {e}")

    async def _save_user_history(self, user_history: UserChatHistory):
        """保存用户历史记录"""
        if not self.enable_history_storage:
            return
            
        try:
            filename = f"{user_history.group_id}_{user_history.user_id}.pkl"
            filepath = os.path.join(self.history_storage_path, filename)
            
            user_history.clear_old_records(self.max_history_records)
            
            with open(filepath, 'wb') as f:
                pickle.dump(user_history.to_dict(), f)
                
        except Exception as e:
            logger.error(f"保存用户历史记录失败: {e}")

    def _get_user_history(self, group_id: str, user_id: str) -> UserChatHistory:
        """获取用户历史记录"""
        session_key = (group_id, user_id)
        
        if session_key not in self.chat_histories:
            self.chat_histories[session_key] = UserChatHistory(
                user_id=user_id,
                group_id=group_id,
                records=[],
                last_updated=time.time()
            )
        
        return self.chat_histories[session_key]

    async def _add_message_to_history(self, group_id: str, user_id: str, role: str, content: str):
        """添加消息到历史记录"""
        if not self.enable_history_storage:
            return
            
        async with self.history_lock:
            user_history = self._get_user_history(group_id, user_id)
            
            record = ChatHistoryRecord(
                timestamp=time.time(),
                role=role,
                content=content
            )
            
            user_history.add_record(record)
            await self._save_user_history(user_history)

    async def _get_recent_history_for_session(self, group_id: str, user_id: str) -> List[Dict[str, Any]]:
        """获取用于会话的最近历史记录"""
        if not self.enable_history_storage:
            return []
            
        async with self.history_lock:
            user_history = self._get_user_history(group_id, user_id)
            recent_records = user_history.get_recent_records(self.history_records_to_use)
            
            context_messages = []
            for record in recent_records:
                context_messages.append({
                    "role": record.role,
                    "content": record.content
                })
            
            return context_messages

    async def _get_enhanced_persona_prompt(self, event: AstrMessageEvent) -> str:
        """获取增强版角色设定"""
        if not self.enable_persona:
            return ""
        
        if self.persona_override:
            return self.persona_override
        
        try:
            uid = event.unified_msg_origin
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
            if not curr_cid:
                return ""
                
            conversation = await self.context.conversation_manager.get_conversation(uid, curr_cid)
            if not conversation:
                return ""
            
            if self.use_system_prompt_directly:
                return await self._get_system_prompt_directly(conversation)
            else:
                persona_id = conversation.persona_id
                
                if persona_id == "[%None]":
                    return ""
                    
                if not persona_id:
                    default_persona = self.context.provider_manager.selected_default_persona
                    persona_id = default_persona.get("name") if default_persona else ""
                
                return await self._get_persona_prompt_by_id(persona_id)
                
        except Exception as e:
            logger.error(f"获取角色设定失败: {e}")
            return ""

    async def _get_system_prompt_directly(self, conversation: Conversation) -> str:
        """直接获取系统提示词"""
        try:
            if conversation.history:
                history_data = json.loads(conversation.history)
                for msg in history_data:
                    if msg.get("role") == "system":
                        return msg.get("content", "")
            
            if conversation.persona_id and conversation.persona_id != "[%None]":
                return await self._get_persona_prompt_by_id(conversation.persona_id)
            
            return ""
            
        except Exception as e:
            logger.error(f"直接获取系统提示词失败: {e}")
            return ""

    async def _get_persona_prompt_by_id(self, persona_id: str) -> str:
        """根据角色ID获取提示词"""
        try:
            for persona in self.context.provider_manager.personas:
                if persona.get("name") == persona_id:
                    return persona.get("prompt", "")
            return ""
        except Exception as e:
            logger.error(f"根据ID获取角色设定失败: {e}")
            return ""

    async def _start_user_session(self, event: AstrMessageEvent) -> bool:
        """为用户开启沉浸式对话会话"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        
        if not group_id or not user_id:
            return False
            
        session_key = (group_id, user_id)
        
        async with self.session_lock:
            if session_key in self.user_sessions:
                await self._close_user_session(session_key)
            
            current_time = time.time()
            session = UserSession(
                user_id=user_id,
                group_id=group_id,
                start_time=current_time,
                last_activity=current_time,
                is_new_session=True
            )
            
            session.persona_prompt = await self._get_enhanced_persona_prompt(event)
            
            session.timer = asyncio.get_event_loop().call_later(
                self.session_timeout,
                lambda: asyncio.create_task(self._handle_session_timeout(session_key))
            )
            
            if self.enable_history_storage:
                history_context = await self._get_recent_history_for_session(group_id, user_id)
                session.context_messages = history_context
                session.history_records_used = len(history_context)
            else:
                await self._load_conversation_context(event, session)
            
            self.user_sessions[session_key] = session
            
            logger.info(f"为用户 {user_id} 开启沉浸式对话会话")
            return True

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
                session.context_messages = history_data[-5:]
                session.history_records_used = len(session.context_messages)
                
        except Exception as e:
            logger.error(f"加载对话上下文失败: {e}")

    async def _close_user_session(self, session_key: Tuple[str, str]):
        """关闭用户会话"""
        if session_key in self.user_sessions:
            session = self.user_sessions[session_key]
            if session.timer:
                session.timer.cancel()
            del self.user_sessions[session_key]

    async def _handle_session_timeout(self, session_key: Tuple[str, str]):
        """处理会话超时"""
        async with self.session_lock:
            if session_key in self.user_sessions:
                logger.info(f"会话已超时，自动关闭")
                await self._close_user_session(session_key)

    async def _judge_should_reply(self, event: AstrMessageEvent, session: UserSession) -> dict:
        """使用大模型判断是否应该回复"""
        try:
            user_message = event.message_str
            current_context = session.context_messages.copy()
            
            judgment_prompt = f"""
请分析当前对话情况，判断是否需要回复用户的消息。

## 角色设定：
{session.persona_prompt if session.persona_prompt else "默认助手角色"}

## 最近对话记录（{len(current_context)}条）：
{json.dumps(current_context, ensure_ascii=False, indent=2) if current_context else "无历史记录"}

## 用户最新消息：
{user_message}

请基于角色设定和对话历史，分析用户意图和对话连贯性，判断是否需要回应。

请以JSON格式回复：
{{
    "should_reply": true/false,
    "reason": "判断理由",
    "confidence": 0.0-1.0的置信度
}}
"""
            
            provider = self.context.get_using_provider()
            if not provider:
                return {"should_reply": False, "reason": "无可用大模型", "confidence": 0.0}
            
            llm_response = await provider.text_chat(prompt=judgment_prompt, contexts=[])
            response_text = llm_response.completion_text.strip()
            
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            else:
                return {"should_reply": False, "reason": "响应格式错误", "confidence": 0.0}
                
        except Exception as e:
            logger.error(f"判断是否回复时发生错误: {e}")
            return {"should_reply": False, "reason": f"判断错误: {str(e)}", "confidence": 0.0}

    async def _generate_reply(self, event: AstrMessageEvent, session: UserSession, user_message: str) -> str:
        """生成回复内容"""
        try:
            current_context = session.context_messages.copy()
            provider = self.context.get_using_provider()
            
            if not provider:
                return "抱歉，我现在无法回复您。"
            
            system_prompt = ""
            if session.persona_prompt:
                system_prompt = f"请严格按照以下角色设定进行回复：\n\n{session.persona_prompt}"
            
            llm_response = await provider.text_chat(
                prompt=user_message,
                contexts=current_context,
                system_prompt=system_prompt
            )
            
            return llm_response.completion_text.strip()
            
        except Exception as e:
            logger.error(f"生成回复时发生错误: {e}")
            return "抱歉，我暂时无法处理您的消息。"

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """处理群消息"""
        if not self.enable_plugin:
            return
            
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        
        if user_id == event.get_self_id():
            return
            
        session_key = (group_id, user_id)
        
        async with self.session_lock:
            in_session = session_key in self.user_sessions
        
        if in_session:
            async for result in self._handle_in_session_message(event, session_key):
                yield result
        else:
            if self._should_start_session(event):
                success = await self._start_user_session(event)
                if success:
                    if not self._is_pure_command(event):
                        async for result in self._handle_in_session_message(event, session_key, is_first_message=True):
                            yield result

        if self.enable_history_storage and not self._is_pure_command(event):
            await self._add_message_to_history(group_id, user_id, 'user', event.message_str)

    async def _handle_in_session_message(self, event: AstrMessageEvent, session_key: Tuple[str, str], is_first_message: bool = False):
        """处理会话中的消息"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        user_message = event.message_str.strip()
        
        if any(end_cmd in user_message for end_cmd in ["结束对话", "退出对话", "结束"]):
            async with self.session_lock:
                await self._close_user_session(session_key)
            logger.info(f"用户 {user_id} 结束连续对话")
            return
        
        async with self.session_lock:
            if session_key not in self.user_sessions:
                return
                
            session = self.user_sessions[session_key]
            session.last_activity = time.time()
            session.message_count += 1
            
            if session.timer:
                session.timer.cancel()
            session.timer = asyncio.get_event_loop().call_later(
                self.session_timeout,
                lambda: asyncio.create_task(self._handle_session_timeout(session_key))
            )
        
        extracted_message = self._extract_user_message(event)
        
        if is_first_message and session.is_new_session and self._is_pure_command(event):
            session.is_new_session = False
            return
        
        judgment_result = await self._judge_should_reply(event, session)
        
        if judgment_result["should_reply"]:
            bot_reply = await self._generate_reply(event, session, extracted_message)
            
            reply_chain = [Comp.Plain(text=bot_reply)]
            yield event.chain_result(reply_chain)
            
            if extracted_message and self.enable_history_storage:
                await self._add_message_to_history(group_id, user_id, 'assistant', bot_reply)
            
            session.is_new_session = False
        else:
            confidence = judgment_result.get("confidence", 0)
            if confidence > self.judgment_threshold:
                async with self.session_lock:
                    await self._close_user_session(session_key)

    def _should_start_session(self, event: AstrMessageEvent) -> bool:
        """判断是否应该开始沉浸式对话"""
        if not self.enable_plugin:
            return False
            
        if self.auto_start_on_mention and event.is_at_or_wake_command:
            return True
            
        message_content = event.message_str.strip()
        start_commands = [cmd for cmd in self.enable_commands if cmd in message_content]
        return bool(start_commands)

    def _is_pure_command(self, event: AstrMessageEvent) -> bool:
        """判断是否为纯指令"""
        message_content = event.message_str.strip()
        
        if self.auto_start_on_mention and event.is_at_or_wake_command:
            at_removed = re.sub(r'@\S+\s*', '', message_content).strip()
            return len(at_removed) == 0
            
        for cmd in self.enable_commands:
            if message_content == cmd:
                return True
                
        return False

    def _extract_user_message(self, event: AstrMessageEvent) -> str:
        """从消息中提取用户消息部分"""
        message_content = event.message_str.strip()
        
        if self.auto_start_on_mention and event.is_at_or_wake_command:
            message_content = re.sub(r'@\S+\s*', '', message_content).strip()
        
        for cmd in self.enable_commands:
            if message_content.startswith(cmd):
                message_content = message_content[len(cmd):].strip()
                break
                
        return message_content

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
                
                status_info = f"""
🔮 连续对话状态
├── 状态: 🟢 进行中
├── 持续时间: {duration}秒
├── 消息数量: {session.message_count}条
├── 历史记录: {session.history_records_used}条
└── 超时时间: {self.session_timeout}秒后自动结束
                """
            else:
                status_info = "🔮 连续对话状态: 🔴 未开启\n使用'开始对话'或@我来开启连续对话模式"
        
        yield event.plain_result(status_info)

    @filter.command("历史记录")
    async def show_chat_history(self, event: AstrMessageEvent, count: int = 10):
        """显示对话历史记录"""
        if not self.enable_history_storage:
            yield event.plain_result("❌ 历史记录功能未启用")
            return
            
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        
        async with self.history_lock:
            user_history = self._get_user_history(group_id, user_id)
            recent_records = user_history.get_recent_records(min(count, 20))
            
            if not recent_records:
                yield event.plain_result("📭 暂无历史记录")
                return
            
            history_text = f"📜 最近{len(recent_records)}条对话历史：\n\n"
            for i, record in enumerate(reversed(recent_records), 1):
                time_str = datetime.fromtimestamp(record.timestamp).strftime("%H:%M")
                role_icon = "👤" if record.role == "user" else "🤖"
                history_text += f"{i}. {time_str} {role_icon} {record.content[:50]}...\n"
            
            history_text += f"\n💾 总记录数: {user_history.total_messages}"
            
        yield event.plain_result(history_text)

    async def terminate(self):
        """插件卸载时清理资源"""
        logger.info("正在清理连续对话插件...")
        
        async with self.session_lock:
            for session_key in list(self.user_sessions.keys()):
                await self._close_user_session(session_key)
                
        logger.info("连续对话插件清理完成")
