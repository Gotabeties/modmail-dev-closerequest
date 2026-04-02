import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp
import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel


class AITicket(commands.Cog):
    """Auto-reply to selected Modmail tickets with a Hermes-compatible AI API."""

    def __init__(self, bot):
        self.bot = bot
        if hasattr(bot, "plugin_db"):
            self.db = bot.plugin_db.get_partition(self)
        else:
            self.db = bot.api.get_plugin_partition(self)
        self.default_config = {
            "enabled": False,
            "base_url": "https://hermes.ai.unturf.com/v1",
            "api_key": "choose-any-value",
            "model": "adamo1139/Hermes-3-Llama-3.1-8B-FP8-Dynamic",
            "temperature": 0.5,
            "max_tokens": 220,
            "history_messages": 8,
            "request_timeout": 45,
            "cooldown_seconds": 8,
            "allowed_category_ids": [],
            "allowed_channel_ids": [],
            "escalation_category_id": None,
            "escalation_keywords": [
                "real person",
                "human",
                "agent",
                "staff",
                "someone real",
                "talk to a person",
                "speak to a person",
                "representative",
            ],
            "system_prompt": (
                "You are a helpful Modmail support assistant. Keep responses concise, accurate, and polite. "
                "If unsure, ask one clarifying question. Never claim actions are done unless they are confirmed."
            ),
            "error_notice_enabled": False,
            "escalate_on_error": True,
            "thinking_message": "Thinking...",
        }
        self.config = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._thread_locks: Dict[int, asyncio.Lock] = {}
        self._last_reply_at: Dict[int, datetime] = {}

    async def cog_load(self):
        self.config = await self.db.find_one({"_id": "aiticket-config"})
        if self.config is None:
            self.config = self.default_config.copy()
            await self.update_config()

        missing = [k for k in self.default_config if k not in self.config]
        if missing:
            for key in missing:
                self.config[key] = self.default_config[key]
            await self.update_config()

        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    async def update_config(self):
        await self.db.find_one_and_update(
            {"_id": "aiticket-config"},
            {"$set": self.config},
            upsert=True,
        )

    @staticmethod
    def _clean_list_ids(values: List[int]) -> List[int]:
        deduped = []
        seen = set()
        for value in values:
            ivalue = int(value)
            if ivalue in seen:
                continue
            seen.add(ivalue)
            deduped.append(ivalue)
        return deduped

    def _thread_is_allowed(self, thread) -> bool:
        channel = getattr(thread, "channel", None)
        if channel is None:
            return False

        allowed_channels = {int(x) for x in self.config.get("allowed_channel_ids", [])}
        allowed_categories = {int(x) for x in self.config.get("allowed_category_ids", [])}

        if not allowed_channels and not allowed_categories:
            return False

        if allowed_channels and channel.id in allowed_channels:
            return True

        channel_category_id = getattr(channel, "category_id", None)
        if allowed_categories and channel_category_id in allowed_categories:
            return True

        return False

    def _is_escalation_request(self, content: str) -> bool:
        text = (content or "").strip().lower()
        if not text:
            return False

        for phrase in self.config.get("escalation_keywords", []):
            phrase = str(phrase).strip().lower()
            if phrase and phrase in text:
                return True
        return False

    async def _move_thread_to_escalation(self, thread, requested_by: discord.abc.User):
        channel = getattr(thread, "channel", None)
        if channel is None:
            return False, "Ticket channel not found."

        escalation_category_id = self.config.get("escalation_category_id")
        if escalation_category_id is None:
            return False, "Escalation category is not configured."

        category = channel.guild.get_channel(int(escalation_category_id))
        if category is None or not isinstance(category, discord.CategoryChannel):
            return False, "Configured escalation category no longer exists."

        if channel.category_id == category.id:
            return True, "Ticket is already in the escalation category."

        try:
            await channel.edit(
                category=category,
                reason=f"AI escalation requested by {requested_by} ({requested_by.id})",
            )
            return True, f"Moved this ticket to **{category.name}** for human support."
        except discord.Forbidden:
            return False, "I do not have permission to move this channel."
        except discord.HTTPException as exc:
            return False, f"Failed to move channel: {exc}"

    async def _build_ai_messages(self, thread, user_message: discord.Message) -> List[dict]:
        channel = getattr(thread, "channel", None)
        messages = [{"role": "system", "content": self.config.get("system_prompt", "You are helpful.")}]

        if channel is None:
            messages.append({"role": "user", "content": user_message.content or ""})
            return messages

        history_count = max(1, int(self.config.get("history_messages", 8)))
        history = []
        try:
            async for msg in channel.history(limit=history_count, oldest_first=False):
                if not msg.content:
                    continue
                history.append(msg)
        except Exception:
            history = []

        for msg in reversed(history):
            if msg.id == user_message.id:
                role = "user"
            elif msg.author.id == self.bot.user.id:
                role = "assistant"
            else:
                role = "user"

            content = (msg.content or "").strip()
            if not content:
                continue

            messages.append({"role": role, "content": content[:2000]})

        if not any(item.get("content") == (user_message.content or "").strip() for item in messages if item["role"] == "user"):
            messages.append({"role": "user", "content": (user_message.content or "")[:2000]})

        return messages

    async def _request_ai_reply(self, messages: List[dict]) -> str:
        if self.session is None:
            raise RuntimeError("HTTP session is not ready")

        base_url = str(self.config.get("base_url", "https://hermes.ai.unturf.com/v1")).rstrip("/")
        endpoint = f"{base_url}/chat/completions"

        headers = {
            "Authorization": f"Bearer {self.config.get('api_key', 'choose-any-value')}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.config.get("model", "adamo1139/Hermes-3-Llama-3.1-8B-FP8-Dynamic"),
            "messages": messages,
            "temperature": float(self.config.get("temperature", 0.5)),
            "max_tokens": int(self.config.get("max_tokens", 220)),
        }

        timeout = aiohttp.ClientTimeout(total=max(5, int(self.config.get("request_timeout", 45))))
        async with self.session.post(endpoint, json=payload, headers=headers, timeout=timeout) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                message = data.get("error", data) if isinstance(data, dict) else data
                raise RuntimeError(f"AI API HTTP {response.status}: {message}")

            choices = data.get("choices", []) if isinstance(data, dict) else []
            if not choices:
                raise RuntimeError("AI API returned no choices.")

            content = choices[0].get("message", {}).get("content", "")
            content = str(content).strip()
            if not content:
                raise RuntimeError("AI API returned empty content.")

            return content[:1900]

    def _is_in_cooldown(self, thread_id: int) -> bool:
        cooldown = max(0, int(self.config.get("cooldown_seconds", 8)))
        if cooldown == 0:
            return False

        now = datetime.now(timezone.utc)
        last = self._last_reply_at.get(thread_id)
        if last is None:
            return False

        return (now - last).total_seconds() < cooldown

    async def _edit_or_send(self, thread, target_message: Optional[discord.Message], text: str):
        if target_message is not None:
            try:
                await target_message.edit(content=text)
                return
            except Exception:
                pass

        try:
            await thread.reply(text)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, from_mod, message, anonymous, plain):
        if not self.config.get("enabled", False):
            return

        if from_mod:
            return

        if getattr(message.author, "bot", False):
            return

        if not self._thread_is_allowed(thread):
            return

        thread_id = int(thread.id)
        if self._is_in_cooldown(thread_id):
            return

        lock = self._thread_locks.setdefault(thread_id, asyncio.Lock())
        if lock.locked():
            return

        async with lock:
            if self._is_escalation_request(message.content):
                ok, status_message = await self._move_thread_to_escalation(thread, message.author)
                try:
                    await thread.channel.send(
                        f"🧭 Escalation check: {status_message}",
                    )
                except Exception:
                    pass

                if ok:
                    try:
                        await thread.reply("I moved this ticket for a human team member to continue with you.")
                    except Exception:
                        pass
                return

            thinking_msg: Optional[discord.Message] = None
            try:
                thinking_text = str(self.config.get("thinking_message", "Thinking...")).strip() or "Thinking..."
                thinking_msg = await thread.reply(f"🤖 {thinking_text}")
            except Exception:
                thinking_msg = None

            try:
                prompt_messages = await self._build_ai_messages(thread, message)
                ai_reply = await self._request_ai_reply(prompt_messages)
                await self._edit_or_send(thread, thinking_msg, ai_reply)
                self._last_reply_at[thread_id] = datetime.now(timezone.utc)
            except Exception as exc:
                escalated = False
                escalation_status = None

                if self.config.get("escalate_on_error", True):
                    escalated, escalation_status = await self._move_thread_to_escalation(thread, message.author)

                if escalated:
                    await self._edit_or_send(
                        thread,
                        thinking_msg,
                        "I hit an error while generating a reply. I moved this ticket so a human team member can help you next.",
                    )
                else:
                    await self._edit_or_send(
                        thread,
                        thinking_msg,
                        "I hit an error while generating a reply. A staff member will continue helping you.",
                    )

                if self.config.get("error_notice_enabled", False):
                    try:
                        details = f"⚠️ AI auto-reply failed: {exc}"
                        if escalation_status:
                            details += f" | Escalation: {escalation_status}"
                        await thread.channel.send(details)
                    except Exception:
                        pass

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.group(name="aiticket", invoke_without_command=True)
    async def aiticket_group(self, ctx):
        """Configure AI auto-reply behavior for selected Modmail tickets."""
        await ctx.send_help(ctx.command)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="status")
    async def aiticket_status(self, ctx):
        """Show current AI auto-reply settings."""
        allowed_channels = self.config.get("allowed_channel_ids", [])
        allowed_categories = self.config.get("allowed_category_ids", [])
        escalation_category = self.config.get("escalation_category_id")
        keywords = self.config.get("escalation_keywords", [])

        embed = discord.Embed(title="AI Ticket Auto-Reply", color=self.bot.main_color)
        embed.add_field(name="Enabled", value=str(self.config.get("enabled", False)), inline=True)
        embed.add_field(name="Model", value=str(self.config.get("model", "N/A"))[:1024], inline=False)
        embed.add_field(name="Base URL", value=str(self.config.get("base_url", "N/A"))[:1024], inline=False)
        embed.add_field(name="Temperature", value=str(self.config.get("temperature", 0.5)), inline=True)
        embed.add_field(name="Max Tokens", value=str(self.config.get("max_tokens", 220)), inline=True)
        embed.add_field(name="Cooldown (s)", value=str(self.config.get("cooldown_seconds", 8)), inline=True)
        embed.add_field(name="Escalate On Error", value=str(self.config.get("escalate_on_error", True)), inline=True)
        embed.add_field(name="Thinking Message", value=str(self.config.get("thinking_message", "Thinking..."))[:1024], inline=False)

        embed.add_field(
            name="Allowed Channels",
            value=", ".join(f"<#{cid}>" for cid in allowed_channels) if allowed_channels else "None",
            inline=False,
        )
        embed.add_field(
            name="Allowed Categories",
            value=", ".join(f"`{cid}`" for cid in allowed_categories) if allowed_categories else "None",
            inline=False,
        )

        if escalation_category:
            embed.add_field(name="Escalation Category", value=f"`{escalation_category}`", inline=False)
        else:
            embed.add_field(name="Escalation Category", value="Not set", inline=False)

        embed.add_field(name="Escalation Keywords", value=", ".join(keywords[:25]) if keywords else "None", inline=False)
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="toggle")
    async def aiticket_toggle(self, ctx):
        """Enable or disable AI auto-reply."""
        self.config["enabled"] = not self.config.get("enabled", False)
        await self.update_config()
        await ctx.send(f"✅ AI ticket auto-reply is now **{'enabled' if self.config['enabled'] else 'disabled'}**.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setbaseurl")
    async def aiticket_setbaseurl(self, ctx, url: str):
        """Set API base URL (example: https://hermes.ai.unturf.com/v1)."""
        self.config["base_url"] = url.strip().rstrip("/")
        await self.update_config()
        await ctx.send(f"✅ Base URL set to: `{self.config['base_url']}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setapikey")
    async def aiticket_setapikey(self, ctx, *, key: str):
        """Set API key. Use 'clear' to reset to choose-any-value."""
        if key.strip().lower() == "clear":
            self.config["api_key"] = "choose-any-value"
            await self.update_config()
            await ctx.send("✅ API key reset to default placeholder.")
            return

        self.config["api_key"] = key.strip()
        await self.update_config()
        await ctx.send("✅ API key updated.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setmodel")
    async def aiticket_setmodel(self, ctx, *, model: str):
        """Set model id used for completions."""
        self.config["model"] = model.strip()
        await self.update_config()
        await ctx.send(f"✅ Model set to: `{self.config['model']}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="settemperature")
    async def aiticket_settemperature(self, ctx, value: float):
        """Set temperature (0.0 to 2.0)."""
        if value < 0 or value > 2:
            await ctx.send("❌ Temperature must be between 0.0 and 2.0")
            return

        self.config["temperature"] = float(value)
        await self.update_config()
        await ctx.send(f"✅ Temperature set to: `{value}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setmaxtokens")
    async def aiticket_setmaxtokens(self, ctx, value: int):
        """Set max response tokens."""
        if value < 20 or value > 2000:
            await ctx.send("❌ Max tokens must be between 20 and 2000")
            return

        self.config["max_tokens"] = int(value)
        await self.update_config()
        await ctx.send(f"✅ Max tokens set to: `{value}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="sethistory")
    async def aiticket_sethistory(self, ctx, value: int):
        """Set number of recent messages to include (1-20)."""
        if value < 1 or value > 20:
            await ctx.send("❌ History must be between 1 and 20")
            return

        self.config["history_messages"] = int(value)
        await self.update_config()
        await ctx.send(f"✅ History messages set to: `{value}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="settimeout")
    async def aiticket_settimeout(self, ctx, value: int):
        """Set API request timeout in seconds (5-120)."""
        if value < 5 or value > 120:
            await ctx.send("❌ Timeout must be between 5 and 120 seconds")
            return

        self.config["request_timeout"] = int(value)
        await self.update_config()
        await ctx.send(f"✅ Request timeout set to: `{value}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setcooldown")
    async def aiticket_setcooldown(self, ctx, value: int):
        """Set minimum seconds between AI replies in a ticket (0-600)."""
        if value < 0 or value > 600:
            await ctx.send("❌ Cooldown must be between 0 and 600 seconds")
            return

        self.config["cooldown_seconds"] = int(value)
        await self.update_config()
        await ctx.send(f"✅ Cooldown set to: `{value}` seconds")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setprompt")
    async def aiticket_setprompt(self, ctx, *, prompt: str):
        """Set system prompt used for AI responses."""
        prompt = prompt.strip()
        if len(prompt) < 5:
            await ctx.send("❌ Prompt is too short.")
            return

        self.config["system_prompt"] = prompt
        await self.update_config()
        await ctx.send("✅ System prompt updated.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="seterrornotice")
    async def aiticket_seterrornotice(self, ctx, enabled: bool):
        """Enable or disable staff-channel error notices."""
        self.config["error_notice_enabled"] = bool(enabled)
        await self.update_config()
        await ctx.send(f"✅ Error notices set to `{enabled}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setescalateonerror")
    async def aiticket_setescalateonerror(self, ctx, enabled: bool):
        """Enable or disable automatic category move when AI fails."""
        self.config["escalate_on_error"] = bool(enabled)
        await self.update_config()
        await ctx.send(f"✅ Escalate on error set to `{enabled}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setthinking")
    async def aiticket_setthinking(self, ctx, *, text: str):
        """Set the temporary status message shown while AI is generating."""
        cleaned = text.strip()
        if not cleaned:
            await ctx.send("❌ Thinking message cannot be empty.")
            return

        self.config["thinking_message"] = cleaned[:150]
        await self.update_config()
        await ctx.send(f"✅ Thinking message set to `{self.config['thinking_message']}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="addchannel")
    async def aiticket_addchannel(self, ctx, channel: discord.TextChannel):
        """Allow AI replies in one specific ticket channel."""
        current = list(self.config.get("allowed_channel_ids", []))
        current.append(channel.id)
        self.config["allowed_channel_ids"] = self._clean_list_ids(current)
        await self.update_config()
        await ctx.send(f"✅ Added allowed channel: {channel.mention}")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="removechannel")
    async def aiticket_removechannel(self, ctx, channel: discord.TextChannel):
        """Remove an allowed ticket channel."""
        current = [int(x) for x in self.config.get("allowed_channel_ids", []) if int(x) != channel.id]
        self.config["allowed_channel_ids"] = current
        await self.update_config()
        await ctx.send(f"✅ Removed allowed channel: {channel.mention}")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="clearchannels")
    async def aiticket_clearchannels(self, ctx):
        """Clear allowed ticket channel list."""
        self.config["allowed_channel_ids"] = []
        await self.update_config()
        await ctx.send("✅ Cleared all allowed channels.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="addcategory")
    async def aiticket_addcategory(self, ctx, category: discord.CategoryChannel):
        """Allow AI replies for every ticket channel in a category."""
        current = list(self.config.get("allowed_category_ids", []))
        current.append(category.id)
        self.config["allowed_category_ids"] = self._clean_list_ids(current)
        await self.update_config()
        await ctx.send(f"✅ Added allowed category: **{category.name}** (`{category.id}`)")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="removecategory")
    async def aiticket_removecategory(self, ctx, category: discord.CategoryChannel):
        """Remove an allowed category."""
        current = [int(x) for x in self.config.get("allowed_category_ids", []) if int(x) != category.id]
        self.config["allowed_category_ids"] = current
        await self.update_config()
        await ctx.send(f"✅ Removed allowed category: **{category.name}**")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="clearcategories")
    async def aiticket_clearcategories(self, ctx):
        """Clear allowed category list."""
        self.config["allowed_category_ids"] = []
        await self.update_config()
        await ctx.send("✅ Cleared all allowed categories.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setescalationcategory")
    async def aiticket_setescalationcategory(self, ctx, *, category_input: str):
        """Set escalation target category. Use 'none' to disable."""
        if category_input.strip().lower() == "none":
            self.config["escalation_category_id"] = None
            await self.update_config()
            await ctx.send("✅ Escalation category cleared.")
            return

        converter = commands.CategoryChannelConverter()
        try:
            category = await converter.convert(ctx, category_input)
        except commands.BadArgument:
            await ctx.send("❌ Could not find that category.")
            return

        self.config["escalation_category_id"] = int(category.id)
        await self.update_config()
        await ctx.send(f"✅ Escalation category set to **{category.name}** (`{category.id}`)")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="addkeyword")
    async def aiticket_addkeyword(self, ctx, *, phrase: str):
        """Add a phrase that triggers human escalation."""
        phrase = phrase.strip().lower()
        if not phrase:
            await ctx.send("❌ Phrase cannot be empty.")
            return

        keywords = [str(x).lower() for x in self.config.get("escalation_keywords", [])]
        if phrase in keywords:
            await ctx.send("ℹ️ That escalation phrase already exists.")
            return

        keywords.append(phrase)
        self.config["escalation_keywords"] = keywords
        await self.update_config()
        await ctx.send(f"✅ Added escalation phrase: `{phrase}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="removekeyword")
    async def aiticket_removekeyword(self, ctx, *, phrase: str):
        """Remove an escalation trigger phrase."""
        phrase = phrase.strip().lower()
        keywords = [str(x).lower() for x in self.config.get("escalation_keywords", [])]

        if phrase not in keywords:
            await ctx.send("❌ That phrase is not in the escalation list.")
            return

        keywords = [k for k in keywords if k != phrase]
        self.config["escalation_keywords"] = keywords
        await self.update_config()
        await ctx.send(f"✅ Removed escalation phrase: `{phrase}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="resetkeywords")
    async def aiticket_resetkeywords(self, ctx):
        """Reset escalation keywords to defaults."""
        self.config["escalation_keywords"] = list(self.default_config["escalation_keywords"])
        await self.update_config()
        await ctx.send("✅ Escalation keywords reset to defaults.")


async def setup(bot):
    await bot.add_cog(AITicket(bot))
