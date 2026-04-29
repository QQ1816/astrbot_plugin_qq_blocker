"""
AstrBot 插件：QQ 黑名单检测 + 自动退群 + 主人通知
版本 1.1.1
修复：发送群消息/私聊消息时使用纯文本字符串，避免 JSON 序列化错误
功能：
  - /block_qq <qq>  /unblock_qq <qq>  /block_list  管理黑名单
  - 每 60 秒扫描所有群，若包含黑名单用户则自动处理并通知主人
"""

import asyncio
import json
from pathlib import Path
from typing import List, Set, Optional, Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


@register("astrbot_plugin_qq_blocker", "your_name",
          "黑名单检测与自动退群，结果通知主人", "1.1.1",
          "https://github.com/your_name/astrbot_plugin_qq_blocker")
class QQBlocker(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._data_dir = Path("data")
        self._block_file = self._data_dir / "qq_block_list.json"
        self._blocked_qqs: Set[str] = set()
        self._check_task: Optional[asyncio.Task] = None
        self._admin_qqs: List[str] = []

    # ==================== 数据持久化 ====================
    async def _ensure_data_dir(self):
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"创建 data 目录失败: {e}")

    async def _load_block_list(self):
        await self._ensure_data_dir()
        try:
            if self._block_file.exists():
                data = json.loads(self._block_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._blocked_qqs = set(str(q) for q in data)
                    logger.info(f"已加载黑名单，共 {len(self._blocked_qqs)} 人")
            else:
                await self._save_block_list()
        except Exception as e:
            logger.error(f"加载黑名单失败: {e}")

    async def _save_block_list(self):
        await self._ensure_data_dir()
        try:
            self._block_file.write_text(
                json.dumps(list(self._blocked_qqs), ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except IOError as e:
            logger.error(f"保存黑名单失败: {e}")

    # ==================== 获取主人 QQ ====================
    def _load_admin_qqs(self):
        try:
            cfg = self.context.get_config()
            admin = cfg.get("admin_qq", [])
            if admin and isinstance(admin, list):
                self._admin_qqs = [str(q) for q in admin]
                logger.info(f"从配置读取管理员: {self._admin_qqs}")
                return
        except Exception as e:
            logger.warning(f"读取配置管理员失败: {e}")
        self._admin_qqs = ["3962118580"]
        logger.info("使用硬编码管理员: 3962118580")

    # ==================== 获取平台适配器 ====================
    def _get_platforms(self) -> List[Any]:
        pm = self.context.platform_manager
        if hasattr(pm, "get_insts"):
            try:
                insts = pm.get_insts()
                if insts:
                    return insts
            except Exception as e:
                logger.debug(f"get_insts() 失败: {e}")
        if hasattr(pm, "platform_insts"):
            try:
                insts = pm.platform_insts
                if insts:
                    return insts
            except Exception as e:
                logger.debug(f"platform_insts 失败: {e}")
        return []

    def _get_bot_client(self) -> Any:
        plats = self._get_platforms()
        for p in plats:
            if hasattr(p, "get_group_list"):
                return p
            if hasattr(p, "bot"):
                return p.bot
        return None

    # ==================== 通知主人 ====================
    async def _notify_admin(self, message: str):
        bot = self._get_bot_client()
        if not bot:
            logger.error("无法获取 bot 客户端，无法通知主人")
            return

        # 尝试私聊每位管理员
        for uid in self._admin_qqs:
            try:
                if hasattr(bot, "send_private_msg"):
                    await bot.send_private_msg(user_id=uid, message=message)
                    logger.info(f"已通知管理员 {uid}")
                    return
                elif hasattr(bot, "send_private_message"):
                    await bot.send_private_message(user_id=uid, message=message)
                    logger.info(f"已通知管理员 {uid}")
                    return
            except Exception as e:
                logger.warning(f"私聊通知管理员 {uid} 失败: {e}")

        # 兜底群通知
        try:
            for method in ("send_group_msg", "send_group_message"):
                if hasattr(bot, method):
                    await getattr(bot, method)(group_id="867441734", message=message)
                    logger.info("已通过群 867441734 发出通知")
                    return
        except Exception as e:
            logger.error(f"群兜底通知失败: {e}")

    # ==================== 核心检查 ====================
    async def _run_check(self):
        if not self._blocked_qqs:
            return

        bot = self._get_bot_client()
        if not bot:
            await self._notify_admin("❌ 黑名单检测失败：无法获取 bot 客户端")
            return

        # 获取群列表
        groups = []
        try:
            if hasattr(bot, "get_group_list"):
                groups = await bot.get_group_list()
        except Exception as e:
            await self._notify_admin(f"❌ 黑名单检测失败：获取群列表出错 - {e}")
            return

        if not groups:
            return

        for group in groups:
            group_id = str(group.get("group_id", ""))
            group_name = group.get("group_name", "") or group_id
            if not group_id:
                continue

            # 获取群成员
            try:
                members = []
                if hasattr(bot, "get_group_member_list"):
                    members = await bot.get_group_member_list(group_id=group_id)
            except Exception as e:
                await self._notify_admin(f"⚠️ 获取群 {group_id} 成员列表失败: {e}")
                continue

            hit_qqs = []
            for m in members:
                uid = str(m.get("user_id", ""))
                if uid in self._blocked_qqs:
                    hit_qqs.append((uid, m.get("card", "") or m.get("nickname", "")))

            if not hit_qqs:
                continue

            # 处理命中的第一个黑名单用户（发现一个就退群）
            uid, name = hit_qqs[0]
            logger.info(f"在群 {group_id} 中发现黑名单用户 {uid}({name})")

            # 向群内发送提醒（纯文本）
            alert_msg = f"检测到{uid}（{name}）在bot黑名单，3秒后尝试退群"
            send_ok = False
            for method_name in ("send_group_msg", "send_group_message"):
                if hasattr(bot, method_name):
                    try:
                        await getattr(bot, method_name)(group_id=group_id, message=alert_msg)
                        send_ok = True
                        break
                    except Exception as e:
                        logger.warning(f"群 {group_id} 发送提醒失败 ({method_name}): {e}")

            # 等待 3 秒
            await asyncio.sleep(3)

            # 退群
            leave_success = False
            error_msg = ""
            try:
                if hasattr(bot, "set_group_leave"):
                    await bot.set_group_leave(group_id=group_id)
                    leave_success = True
                else:
                    error_msg = "bot 缺少 set_group_leave 方法"
            except Exception as e:
                error_msg = str(e)

            # 通知管理员
            if leave_success:
                await self._notify_admin(f"✅ 已成功退出群 {group_id}（{group_name}）")
            else:
                await self._notify_admin(f"❌ 退出群 {group_id}（{group_name}）失败：{error_msg}")
                # 退群失败时也在群内通知
                fail_msg = f"退出群失败：{error_msg}"
                if not send_ok:
                    # 如果之前提醒都没成功，再试一次发失败通知
                    for method_name in ("send_group_msg", "send_group_message"):
                        if hasattr(bot, method_name):
                            try:
                                await getattr(bot, method_name)(group_id=group_id, message=fail_msg)
                                break
                            except Exception as e:
                                logger.warning(f"发送退群失败通知到群失败: {e}")
                else:
                    for method_name in ("send_group_msg", "send_group_message"):
                        if hasattr(bot, method_name):
                            try:
                                await getattr(bot, method_name)(group_id=group_id, message=fail_msg)
                                break
                            except Exception as e:
                                logger.warning(f"发送退群失败通知到群失败: {e}")

            # 一个群只处理一次（已退出或已尝试退出）
            break

    # ==================== 定时任务 ====================
    async def _periodic_check(self):
        while True:
            try:
                await self._run_check()
            except Exception as e:
                logger.error(f"定时检查异常: {e}", exc_info=True)
                await self._notify_admin(f"❌ 定时黑名单检测发生异常：{e}")
            await asyncio.sleep(60)

    # ==================== 指令 ====================
    @filter.command("block_qq")
    async def cmd_block_qq(self, event: AstrMessageEvent, qq: str):
        if not qq or not qq.strip().isdigit():
            yield event.plain_result("❌ 请输入有效 QQ 号。用法: /block_qq <qq号>")
            return
        qq = qq.strip()
        if qq in self._blocked_qqs:
            yield event.plain_result(f"⚠️ QQ {qq} 已在黑名单中。")
            return
        self._blocked_qqs.add(qq)
        await self._save_block_list()
        logger.info(f"已添加黑名单: {qq}")
        yield event.plain_result(f"✅ 已将 QQ {qq} 加入黑名单。")

    @filter.command("unblock_qq")
    async def cmd_unblock_qq(self, event: AstrMessageEvent, qq: str):
        if not qq or not qq.strip().isdigit():
            yield event.plain_result("❌ 请输入有效 QQ 号。用法: /unblock_qq <qq号>")
            return
        qq = qq.strip()
        if qq not in self._blocked_qqs:
            yield event.plain_result(f"⚠️ QQ {qq} 不在黑名单中。")
            return
        self._blocked_qqs.discard(qq)
        await self._save_block_list()
        logger.info(f"已移除黑名单: {qq}")
        yield event.plain_result(f"✅ 已将 QQ {qq} 移出黑名单。")

    @filter.command("block_list")
    async def cmd_block_list(self, event: AstrMessageEvent):
        if not self._blocked_qqs:
            yield event.plain_result("📋 当前黑名单为空。")
            return
        qq_list = "\n".join(f"• {qq}" for qq in sorted(self._blocked_qqs))
        yield event.plain_result(f"📋 当前黑名单 ({len(self._blocked_qqs)} 人):\n{qq_list}")

    # ==================== 生命周期 ====================
    async def initialize(self):
        self._load_admin_qqs()
        await self._load_block_list()
        self._check_task = asyncio.create_task(self._periodic_check())
        logger.info("QQ 黑名单检测插件已启动")

    async def terminate(self):
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        logger.info("QQ 黑名单检测插件已停止")