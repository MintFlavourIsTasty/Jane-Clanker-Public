#  Credit to MintFlavour(@koni_mint)
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
from features.community.reminders import (
    ReminderSnoozeView, cancelReminder, createReminder, getReminder,
    listActiveRemindersForUser, listDueReminders, markReminderSent,
    parseRecurringInterval, parseReminderWhen, rescheduleReminder,
)
from runtime import interaction as interactionRuntime
from runtime import permissions as runtimePermissions

log = logging.getLogger(__name__)

def _parseRoleIdsText(raw: str) -> list[int]:
    roleIds: list[int] = []
    for token in str(raw or "").replace("\n", " ").replace(",", " ").split():
        digits = "".join(ch for ch in token if ch.isdigit())
        if digits:
            parsed = int(digits)
            if parsed > 0 and parsed not in roleIds:
                roleIds.append(parsed)
    return roleIds

def _formatIntervalText(totalSeconds: int) -> str:
    seconds = max(0, int(totalSeconds or 0))
    if seconds <= 0: return "none"
    if seconds % 604800 == 0: return f"every {seconds // 604800} week{'s' if seconds // 604800 != 1 else ''}"
    if seconds % 86400 == 0: return f"every {seconds // 86400} day{'s' if seconds // 86400 != 1 else ''}"
    if seconds % 3600 == 0: return f"every {seconds // 3600} hour{'s' if seconds // 3600 != 1 else ''}"
    if seconds % 60 == 0: return f"every {seconds // 60} minute{'s' if seconds // 60 != 1 else ''}"
    return f"every {seconds} second{'s' if seconds != 1 else ''}"

class ReminderCog(commands.Cog):
    reminderGroup = app_commands.Group(name="reminder", description="Reminder and timer tools.")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._reminderTask: asyncio.Task | None = None

    async def cog_load(self) -> None:
        if self._reminderTask is None or self._reminderTask.done():
            self._reminderTask = asyncio.create_task(self._runReminderLoop())

    def cog_unload(self) -> None:
        if self._reminderTask is not None and not self._reminderTask.done():
            self._reminderTask.cancel()
        self._reminderTask = None

    async def _safeEphemeral(self, interaction: discord.Interaction, content: str) -> None:
        await interactionRuntime.safeInteractionReply(interaction, content=content, ephemeral=True)

    def _canCreateTeamReminder(self, member: discord.Member) -> bool:
        if runtimePermissions.hasAdminOrManageGuild(member): return True
        allowedRoleIds = {int(getattr(config, "middleRankRoleId", 0) or 0), int(getattr(config, "highRankRoleId", 0) or 0)}
        allowedRoleIds = {r for r in allowedRoleIds if r > 0}
        if not allowedRoleIds: return False
        return any(int(role.id) in allowedRoleIds for role in member.roles)

    async def _getReminderChannel(self, channelId: int) -> discord.TextChannel | discord.Thread | None:
        if int(channelId) <= 0: return None
        channel = self.bot.get_channel(int(channelId))
        if channel is None:
            try: channel = await self.bot.fetch_channel(int(channelId))
            except (discord.Forbidden, discord.NotFound, discord.HTTPException): return None
        return channel if isinstance(channel, (discord.TextChannel, discord.Thread)) else None

    def _parseReminderTime(self, row: dict) -> datetime | None:
        raw = str(row.get("remindAtUtc") or "").strip()
        if not raw: return None
        try: remindAt = datetime.fromisoformat(raw)
        except ValueError: return None
        return remindAt.replace(tzinfo=timezone.utc) if remindAt.tzinfo is None else remindAt.astimezone(timezone.utc)

    def _parseTargetRoleIds(self, row: dict) -> list[int]:
        raw = str(row.get("targetRoleIdsJson") or "").strip()
        if not raw: return []
        try: data = json.loads(raw)
        except json.JSONDecodeError: return []
        if not isinstance(data, list): return []
        return [int(v) for v in data if str(v).isdigit() and int(v) > 0]

    def _buildReminderEmbed(self, row: dict) -> discord.Embed:
        reminderId = int(row.get("reminderId") or 0)
        reminderText = str(row.get("reminderText") or "").strip() or "Reminder"
        embed = discord.Embed(title=f"Reminder #{reminderId}", description=reminderText, color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
        remindAt = self._parseReminderTime(row)
        if remindAt is not None:
            embed.add_field(name="Scheduled For", value=f"{discord.utils.format_dt(remindAt, 'F')}\n{discord.utils.format_dt(remindAt, 'R')}", inline=False)
        embed.add_field(name="Target", value="Team reminder" if str(row.get("targetType") or "USER").strip().upper() == "ROLE" else "Personal reminder", inline=True)
        if int(row.get("recurringIntervalSec") or 0) > 0:
            embed.add_field(name="Repeats", value=_formatIntervalText(int(row.get("recurringIntervalSec"))), inline=True)
        return embed

    def _nextRecurringTime(self, row: dict, *, now: datetime) -> datetime | None:
        intervalSeconds = int(row.get("recurringIntervalSec") or 0)
        if intervalSeconds <= 0: return None
        remindAt, step = self._parseReminderTime(row) or now, timedelta(seconds=intervalSeconds)
        while remindAt <= now: remindAt += step
        return remindAt

    async def _deliverUserReminder(self, row: dict, embed: discord.Embed) -> bool:
        reminderId, userId, channelId = int(row.get("reminderId") or 0), int(row.get("userId") or 0), int(row.get("channelId") or 0)
        user = self.bot.get_user(userId)
        if user is None:
            try: user = await self.bot.fetch_user(userId)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException): user = None
        view = ReminderSnoozeView(cog=self, reminderId=reminderId, userId=userId)
        dmDelivered = False
        if user is not None:
            try:
                await user.send(embed=embed, view=view)
                dmDelivered = True
            except (discord.Forbidden, discord.HTTPException): pass
        if not dmDelivered:
            channel = await self._getReminderChannel(channelId)
            if channel is not None:
                try: await channel.send(content=f"<@{userId}> reminder:", embed=embed, view=view, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
                except (discord.Forbidden, discord.HTTPException): pass
        return dmDelivered

    async def _deliverRoleReminder(self, row: dict, embed: discord.Embed) -> None:
        channel = await self._getReminderChannel(int(row.get("channelId") or 0))
        if channel is None: return
        roleMentions = " ".join(f"<@&{roleId}>" for roleId in self._parseTargetRoleIds(row))
        prefix = f"{roleMentions}\n" if roleMentions else ""
        try: await channel.send(content=f"{prefix}Team reminder from <@{int(row.get('userId') or 0)}>:", embed=embed, allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False))
        except (discord.Forbidden, discord.HTTPException): pass

    async def _processReminder(self, row: dict, now: datetime) -> None:
        reminderId = int(row.get("reminderId") or 0)
        embed = self._buildReminderEmbed(row)
        guildId = int(row.get("guildId") or 0)
        if guildId > 0:
            guild = self.bot.get_guild(guildId)
            if guild is not None: embed.add_field(name="Server", value=guild.name, inline=False)
        dmDelivered = False
        if str(row.get("targetType") or "USER").strip().upper() == "ROLE":
            await self._deliverRoleReminder(row, embed)
        else:
            dmDelivered = await self._deliverUserReminder(row, embed)
        nextTime = self._nextRecurringTime(row, now=now)
        if nextTime is not None:
            await rescheduleReminder(reminderId, remindAtUtcIso=nextTime.isoformat())
        else:
            await markReminderSent(reminderId, dmDelivered=dmDelivered)

    async def _runReminderLoop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                now = datetime.now(timezone.utc)
                due = await listDueReminders(now.isoformat(), limit=50)
                if due:
                    await asyncio.gather(*(self._processReminder(row, now) for row in due), return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Reminder loop failed.")
            await asyncio.sleep(2)

    @reminderGroup.command(name="add", description="Create a reminder or timer.")
    @app_commands.rename(reminder_text="reminder-text")
    async def addReminder(self, interaction: discord.Interaction, when: str, reminder_text: str, repeat: str | None = None, attachment: discord.Attachment | None = None) -> None:
        if not interaction.guild or not interaction.channel:
            return await self._safeEphemeral(interaction, "This command can only be used in a server.")
        if attachment:
            reminder_text = f"{reminder_text}\n{attachment.url}"
        try:
            remindAtUtc, label = parseReminderWhen(when)
            repeatSeconds = parseRecurringInterval(str(repeat or ""))
        except ValueError as exc:
            return await self._safeEphemeral(interaction, str(exc))
        if remindAtUtc <= datetime.now(timezone.utc):
            return await self._safeEphemeral(interaction, "That reminder time has already passed.")
        reminderId = await createReminder(
            guildId=int(interaction.guild.id), channelId=int(interaction.channel.id), userId=int(interaction.user.id),
            reminderText=str(reminder_text or "").strip(), remindAtUtcIso=remindAtUtc.isoformat(), recurringIntervalSec=repeatSeconds
        )
        await self._safeEphemeral(interaction, f"Reminder #{reminderId} set for {discord.utils.format_dt(remindAtUtc, 'F')} ({label}).{' Repeats ' + _formatIntervalText(repeatSeconds) + '.' if repeatSeconds > 0 else ''}")

    @reminderGroup.command(name="team", description="Create a channel-wide reminder for one or more staff roles.")
    @app_commands.rename(role_ids="role-ids", reminder_text="reminder-text")
    async def addTeamReminder(self, interaction: discord.Interaction, when: str, role_ids: str, reminder_text: str, repeat: str | None = None, attachment: discord.Attachment | None = None) -> None:
        if not interaction.guild or not interaction.channel or not isinstance(interaction.user, discord.Member):
            return await self._safeEphemeral(interaction, "This command can only be used in a server.")
        if not self._canCreateTeamReminder(interaction.user):
            return await self._safeEphemeral(interaction, "MR/HR roles or administrator/manage-server required.")
        roleIds = [r for r in _parseRoleIdsText(role_ids) if interaction.guild.get_role(r) is not None]
        if not roleIds:
            return await self._safeEphemeral(interaction, "Please provide at least one valid role ID.")
        if attachment:
            reminder_text = f"{reminder_text}\n{attachment.url}"
        try:
            remindAtUtc, label = parseReminderWhen(when)
            repeatSeconds = parseRecurringInterval(str(repeat or ""))
        except ValueError as exc:
            return await self._safeEphemeral(interaction, str(exc))
        reminderId = await createReminder(
            guildId=int(interaction.guild.id), channelId=int(interaction.channel.id), userId=int(interaction.user.id),
            reminderText=str(reminder_text or "").strip(), remindAtUtcIso=remindAtUtc.isoformat(), targetType="ROLE",
            targetRoleIds=roleIds, recurringIntervalSec=repeatSeconds
        )
        await self._safeEphemeral(interaction, f"Team reminder #{reminderId} set for {discord.utils.format_dt(remindAtUtc, 'F')} ({label}) for {' '.join(f'<@&{r}>' for r in roleIds)}.{' Repeats ' + _formatIntervalText(repeatSeconds) + '.' if repeatSeconds > 0 else ''}")

    @reminderGroup.command(name="list", description="List your active reminders.")
    async def listReminders(self, interaction: discord.Interaction) -> None:
        if not interaction.guild: return await self._safeEphemeral(interaction, "This command can only be used in a server.")
        rows = await listActiveRemindersForUser(int(interaction.guild.id), int(interaction.user.id))
        if not rows: return await self._safeEphemeral(interaction, "You do not have any active reminders.")
        lines: list[str] = []
        for row in rows[:15]:
            remindAt = self._parseReminderTime(row)
            suffixParts = []
            if str(row.get("targetType") or "USER").strip().upper() == "ROLE":
                roleMentions = " ".join(f"<@&{r}>" for r in self._parseTargetRoleIds(row))
                if roleMentions: suffixParts.append(roleMentions)
            if int(row.get("recurringIntervalSec") or 0) > 0:
                suffixParts.append(_formatIntervalText(int(row.get("recurringIntervalSec"))))
            lines.append(f"`#{int(row.get('reminderId') or 0)}` {discord.utils.format_dt(remindAt, 'R') if remindAt else str(row.get('remindAtUtc')).strip()} - {str(row.get('reminderText') or '').strip()}{f' ({chr(124).join(suffixParts)})' if suffixParts else ''}")
        await interactionRuntime.safeInteractionReply(interaction, embed=discord.Embed(title="Your Active Reminders", description="\n".join(lines), color=discord.Color.orange()), ephemeral=True, allowedMentions=discord.AllowedMentions(users=False, roles=False, everyone=False))

    @reminderGroup.command(name="cancel", description="Cancel one of your reminders.")
    @app_commands.rename(reminder_id="reminder-id")
    async def cancelReminderCommand(self, interaction: discord.Interaction, reminder_id: int) -> None:
        reminder = await getReminder(int(reminder_id))
        if reminder is None: return await self._safeEphemeral(interaction, "Reminder not found.")
        if str(reminder.get("status") or "").strip().upper() != "PENDING": return await self._safeEphemeral(interaction, "That reminder is no longer active.")
        if int(reminder.get("userId") or 0) != int(interaction.user.id) and not (isinstance(interaction.user, discord.Member) and (interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild)):
            return await self._safeEphemeral(interaction, "You can only cancel your own reminders.")
        await cancelReminder(int(reminder_id))
        await self._safeEphemeral(interaction, f"Reminder #{int(reminder_id)} canceled.")

    async def handleReminderSnooze(self, interaction: discord.Interaction, *, reminderId: int, delaySeconds: int) -> None:
        row = await getReminder(int(reminderId))
        if row is None: return await self._safeEphemeral(interaction, "Reminder not found.")
        if int(row.get("userId") or 0) != int(interaction.user.id): return await self._safeEphemeral(interaction, "Only the reminder owner can snooze this reminder.")
        newTime = datetime.now(timezone.utc) + timedelta(seconds=max(60, int(delaySeconds or 0)))
        await rescheduleReminder(int(reminderId), remindAtUtcIso=newTime.isoformat())
        await interactionRuntime.safeInteractionReply(interaction, content=f"Reminder #{int(reminderId)} snoozed until {discord.utils.format_dt(newTime, 'R')}.", ephemeral=True)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReminderCog(bot))
