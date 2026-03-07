import re

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel


class Claim(commands.Cog):
    """Allow supporters to claim and unclaim modmail tickets."""

    CLAIM_SUFFIX_PATTERN = re.compile(r"^(?P<base>.+)-(?P<suffix>[a-z0-9]{1,5})$")

    def __init__(self, bot):
        self.bot = bot

    async def _get_ticket_channel(self, ctx):
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

            if isinstance(fresh_channel, discord.TextChannel):
                return fresh_channel
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass

        return channel

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

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command(name="claim")
    async def claim(self, ctx):
        """Claim the current ticket."""
        channel = await self._get_ticket_channel(ctx)

        if channel is None:
            await ctx.send("❌ Could not find the ticket channel.")
            return

        _, current_suffix = self._split_claimed_name(channel.name)
        supporter_suffix = self._supporter_suffix(ctx.author.name)

        if current_suffix is not None:
            if current_suffix == supporter_suffix:
                await ctx.send("ℹ️ This ticket is already claimed by you.")
            else:
                await ctx.send("❌ This ticket is already claimed.")
            return

        new_name = self._build_claimed_name(channel.name, ctx.author.name)

        try:
            await channel.edit(name=new_name, reason=f"Ticket claimed by {ctx.author}")
        except discord.Forbidden:
            await ctx.send("❌ I do not have permission to rename this ticket.")
            return
        except discord.HTTPException:
            await ctx.send("❌ I could not rename this ticket right now.")
            return

        await ctx.send(f"✅ {ctx.author.mention} claimed this ticket.")

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command(name="unclaim")
    async def unclaim(self, ctx):
        """Unclaim the current ticket."""
        channel = await self._get_ticket_channel(ctx)

        if channel is None:
            await ctx.send("❌ Could not find the ticket channel.")
            return

        base_name, current_suffix = self._split_claimed_name(channel.name)
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
        except discord.HTTPException:
            await ctx.send("❌ I could not rename this ticket right now.")
            return

        await ctx.send("✅ This ticket has been unclaimed.")


async def setup(bot):
    await bot.add_cog(Claim(bot))
