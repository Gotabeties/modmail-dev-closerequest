import re

import discord
from discord.ext import commands

from core.models import PermissionLevel


class Claim(commands.Cog):
    """Allow supporters to claim and unclaim modmail tickets."""

    # Claim suffix is always prefixed with "c-" to distinguish from ticket numbers
    CLAIM_TAG = "c-"
    CLAIM_SUFFIX_PATTERN = re.compile(r"^(?P<base>.+)-c-(?P<suffix>[a-z0-9]{1,5})$")

    def __init__(self, bot):
        self.bot = bot
        self._processed_messages = set()

    def _dedup(self, message_id: int) -> bool:
        """Return True if this message was already processed (duplicate). False if new."""
        if message_id in self._processed_messages:
            return True
        self._processed_messages.add(message_id)
        # Keep set small
        if len(self._processed_messages) > 200:
            keep = list(self._processed_messages)[-100:]
            self._processed_messages = set(keep)
        return False

    @staticmethod
    def _supporter_suffix(name: str) -> str:
        """First 5 alphanumeric chars of the supporter's name, lowercased."""
        cleaned = re.sub(r"[^a-z0-9]", "", str(name).lower())
        return cleaned[:5] or "staff"

    @classmethod
    def _split_claimed_name(cls, channel_name: str):
        """Split a claimed channel name into (base, suffix) or (full_name, None)."""
        match = cls.CLAIM_SUFFIX_PATTERN.fullmatch(channel_name)
        if not match:
            return channel_name, None
        return match.group("base"), match.group("suffix")

    @classmethod
    def _build_claimed_name(cls, channel_name: str, supporter_name: str) -> str:
        suffix = cls._supporter_suffix(supporter_name)
        tag = f"-{cls.CLAIM_TAG}{suffix}"
        max_base = 100 - len(tag)
        return f"{channel_name[:max_base]}{tag}"

    async def _check_permissions(self, ctx) -> bool:
        try:
            level = await self.bot.get_permission_level(ctx.author)
            if level >= PermissionLevel.SUPPORTER:
                return True
        except Exception:
            if getattr(ctx.author, "guild_permissions", None) and ctx.author.guild_permissions.administrator:
                return True

        await ctx.send("❌ You need to be a supporter to use this command.")
        return False

    @commands.command(name="claim")
    async def claim(self, ctx):
        """Claim the current ticket. Appends your name tag to the channel."""
        if self._dedup(ctx.message.id):
            return

        try:
            if not await self._check_permissions(ctx):
                return

            thread = getattr(ctx, "thread", None)
            if thread is None:
                await ctx.send("❌ This command can only be used inside a ticket.")
                return

            channel = ctx.channel
            _, current_suffix = self._split_claimed_name(channel.name)
            supporter_suffix = self._supporter_suffix(ctx.author.name)

            if current_suffix is not None:
                if current_suffix == supporter_suffix:
                    await ctx.send("ℹ️ This ticket is already claimed by you.")
                else:
                    await ctx.send("❌ This ticket is already claimed by someone else.")
                return

            new_name = self._build_claimed_name(channel.name, ctx.author.name)

            try:
                await channel.edit(name=new_name, reason=f"Ticket claimed by {ctx.author}")
            except discord.Forbidden:
                await ctx.send("❌ I do not have permission to rename this ticket.")
                return
            except discord.HTTPException as e:
                await ctx.send(f"❌ Could not rename this ticket: {e}")
                return

            await ctx.send(f"✅ {ctx.author.mention} claimed this ticket.")
        except Exception as error:
            await ctx.send(f"❌ Claim failed: {type(error).__name__}: {error}")

    @commands.command(name="unclaim")
    async def unclaim(self, ctx):
        """Unclaim the current ticket. Removes the claim tag from the channel name."""
        if self._dedup(ctx.message.id):
            return

        try:
            if not await self._check_permissions(ctx):
                return

            thread = getattr(ctx, "thread", None)
            if thread is None:
                await ctx.send("❌ This command can only be used inside a ticket.")
                return

            channel = ctx.channel
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
            except discord.HTTPException as e:
                await ctx.send(f"❌ Could not rename this ticket: {e}")
                return

            await ctx.send("✅ This ticket has been unclaimed.")
        except Exception as error:
            await ctx.send(f"❌ Unclaim failed: {type(error).__name__}: {error}")


async def setup(bot):
    await bot.add_cog(Claim(bot))
