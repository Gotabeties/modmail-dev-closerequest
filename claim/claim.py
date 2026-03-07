import asyncio
import re

import discord
from discord.ext import commands

from core.models import PermissionLevel


class Claim(commands.Cog):
    """Allow supporters to claim and unclaim modmail tickets."""

    CLAIM_SUFFIX_PATTERN = re.compile(r"^(?P<base>.+)-(?P<suffix>[a-z0-9]{1,5})$")

    def __init__(self, bot):
        self.bot = bot
        self.claim_cache = {}

    async def _get_ticket_channel(self, ctx):
        channel = getattr(ctx, "channel", None)
        if channel is None:
            thread = getattr(ctx, "thread", None)
            channel = getattr(thread, "channel", None)
        if channel is None:
            return None

        guild = getattr(ctx, "guild", None) or getattr(channel, "guild", None)

        try:
            if guild is not None:
                fresh_channel = await guild.fetch_channel(channel.id)
            else:
                fresh_channel = await self.bot.fetch_channel(channel.id)

            if hasattr(fresh_channel, "name") and hasattr(fresh_channel, "edit"):
                return fresh_channel
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass

        return channel

    async def _refresh_channel(self, channel):
        guild = getattr(channel, "guild", None)

        try:
            if guild is not None:
                refreshed = await guild.fetch_channel(channel.id)
            else:
                refreshed = await self.bot.fetch_channel(channel.id)

            if hasattr(refreshed, "name") and hasattr(refreshed, "edit"):
                return refreshed
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass

        return channel

    def _remember_claim(self, channel_id: int, base_name: str, suffix: str):
        self.claim_cache[int(channel_id)] = {
            "base_name": base_name,
            "suffix": suffix,
        }

    def _clear_claim(self, channel_id: int):
        self.claim_cache.pop(int(channel_id), None)

    def _get_cached_claim(self, channel_id: int):
        return self.claim_cache.get(int(channel_id))

    async def _get_claim_state(self, channel, *, retries: int = 3, delay: float = 0.5):
        current_channel = channel

        for attempt in range(retries + 1):
            base_name, suffix = self._split_claimed_name(current_channel.name)
            if suffix is not None:
                self._remember_claim(current_channel.id, base_name, suffix)
                return current_channel, base_name, suffix

            cached = self._get_cached_claim(current_channel.id)
            if cached is not None:
                return current_channel, cached["base_name"], cached["suffix"]

            if attempt >= retries:
                break

            await asyncio.sleep(delay)
            current_channel = await self._refresh_channel(current_channel)

        return current_channel, current_channel.name, None

    @staticmethod
    def _supporter_suffix(name: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]", "", str(name).lower())
        return cleaned[:5] or "staff"

    @classmethod
    def _split_claimed_name(cls, channel_name: str):
        match = cls.CLAIM_SUFFIX_PATTERN.fullmatch(channel_name)
        if not match:
            return channel_name, None
        return match.group("base"), match.group("suffix")

    @classmethod
    def _build_claimed_name(cls, channel_name: str, supporter_name: str) -> str:
        suffix = cls._supporter_suffix(supporter_name)
        max_base_length = 100 - len(suffix) - 1
        base_name = channel_name[:max_base_length]
        return f"{base_name}-{suffix}"

    async def _check_permissions(self, ctx) -> bool:
        """Returns True if author has at least SUPPORTER level. Sends an error and returns False otherwise."""
        try:
            level = await self.bot.get_permission_level(ctx.author)
            if level >= PermissionLevel.SUPPORTER:
                return True
        except Exception:
            # Fall back to guild admin check if permission lookup fails
            if getattr(ctx.author, "guild_permissions", None) and ctx.author.guild_permissions.administrator:
                return True

        await ctx.send("❌ You need to be a supporter to use this command.")
        return False

    @commands.command(name="claim")
    async def claim(self, ctx):
        """Claim the current ticket."""
        try:
            if not await self._check_permissions(ctx):
                return

            thread = getattr(ctx, "thread", None)
            if thread is None:
                await ctx.send("❌ This command can only be used inside a ticket.")
                return

            channel = await self._get_ticket_channel(ctx)
            if channel is None:
                await ctx.send("❌ Could not find the ticket channel.")
                return

            channel, current_base_name, current_suffix = await self._get_claim_state(channel, retries=1, delay=0.35)
            supporter_suffix = self._supporter_suffix(ctx.author.name)

            if current_suffix is not None:
                if current_suffix == supporter_suffix:
                    await ctx.send("ℹ️ This ticket is already claimed by you.")
                else:
                    await ctx.send(f"❌ This ticket is already claimed by someone else.")
                return

            new_name = self._build_claimed_name(current_base_name, ctx.author.name)

            try:
                await channel.edit(name=new_name, reason=f"Ticket claimed by {ctx.author}")
            except discord.Forbidden:
                await ctx.send("❌ I do not have permission to rename this ticket.")
                return
            except discord.HTTPException as e:
                await ctx.send(f"❌ Could not rename this ticket: {e}")
                return

            self._remember_claim(channel.id, current_base_name, supporter_suffix)
            await ctx.send(f"✅ {ctx.author.mention} claimed this ticket.")
        except Exception as error:
            await ctx.send(f"❌ Claim failed: {type(error).__name__}: {error}")

    @commands.command(name="unclaim")
    async def unclaim(self, ctx):
        """Unclaim the current ticket."""
        try:
            if not await self._check_permissions(ctx):
                return

            thread = getattr(ctx, "thread", None)
            if thread is None:
                await ctx.send("❌ This command can only be used inside a ticket.")
                return

            channel = await self._get_ticket_channel(ctx)
            if channel is None:
                await ctx.send("❌ Could not find the ticket channel.")
                return

            channel, base_name, current_suffix = await self._get_claim_state(channel, retries=2, delay=0.3)
            if current_suffix is None:
                await ctx.send("ℹ️ This ticket is not claimed.")
                return

            supporter_suffix = self._supporter_suffix(ctx.author.name)
            is_admin = getattr(ctx.author.guild_permissions, "administrator", False)

            if current_suffix != supporter_suffix and not is_admin:
                await ctx.send("❌ Only the supporter who claimed this ticket or an admin can unclaim it.")
                return

            try:
                await channel.edit(name=base_name, reason=f"Ticket unclaimed by {ctx.author}")
            except discord.Forbidden:
                await ctx.send("❌ I do not have permission to rename this ticket.")
                return
            except discord.HTTPException as e:
                await ctx.send(f"❌ Could not rename this ticket: {e}")
                return

            self._clear_claim(channel.id)
            await ctx.send("✅ This ticket has been unclaimed.")
        except Exception as error:
            await ctx.send(f"❌ Unclaim failed: {type(error).__name__}: {error}")


async def setup(bot):
    await bot.add_cog(Claim(bot))
