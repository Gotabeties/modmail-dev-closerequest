from collections import OrderedDict
from datetime import datetime, timezone

from discord.ext import commands

from core.models import PermissionLevel


class Claim(commands.Cog):
    """Allow supporters to claim and unclaim modmail tickets."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.plugin_db.get_partition(self)
        self._processed_messages = OrderedDict()

    def _dedup(self, message_id: int) -> bool:
        """Return True if this message was already processed."""
        if message_id in self._processed_messages:
            return True

        self._processed_messages[message_id] = True
        if len(self._processed_messages) > 200:
            self._processed_messages.popitem(last=False)
        return False

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

    async def _get_claim_record(self, thread_id: int):
        record = await self.db.find_one({"_id": f"claim:{int(thread_id)}"})
        if record and record.get("active"):
            return record
        return None

    async def _set_claim_record(self, ctx, thread):
        await self.db.find_one_and_update(
            {"_id": f"claim:{int(thread.id)}"},
            {
                "$set": {
                    "thread_id": int(thread.id),
                    "channel_id": int(ctx.channel.id),
                    "guild_id": int(ctx.guild.id) if ctx.guild else None,
                    "claimer_id": int(ctx.author.id),
                    "claimer_name": str(ctx.author),
                    "claimer_mention": getattr(ctx.author, "mention", str(ctx.author)),
                    "claimed_at": datetime.now(timezone.utc).isoformat(),
                    "active": True,
                }
            },
            upsert=True,
        )

    async def _clear_claim_record(self, thread_id: int):
        await self.db.find_one_and_update(
            {"_id": f"claim:{int(thread_id)}"},
            {
                "$set": {
                    "active": False,
                    "unclaimed_at": datetime.now(timezone.utc).isoformat(),
                }
            },
            upsert=True,
        )

    @commands.command(name="claim")
    async def claim(self, ctx):
        """Claim the current ticket."""
        if self._dedup(ctx.message.id):
            return

        try:
            if not await self._check_permissions(ctx):
                return

            thread = getattr(ctx, "thread", None)
            if thread is None:
                await ctx.send("❌ This command can only be used inside a ticket.")
                return

            record = await self._get_claim_record(thread.id)
            if record is not None:
                current_claimer_id = int(record.get("claimer_id", 0))
                if current_claimer_id == ctx.author.id:
                    await ctx.send("ℹ️ This ticket is already claimed by you.")
                else:
                    current_name = record.get("claimer_mention") or record.get("claimer_name") or "someone else"
                    await ctx.send(f"❌ This ticket is already claimed by {current_name}.")
                return

            await self._set_claim_record(ctx, thread)
            await ctx.send(f"✅ {ctx.author.mention} claimed this ticket.")
        except Exception as error:
            await ctx.send(f"❌ Claim failed: {type(error).__name__}: {error}")

    @commands.command(name="unclaim")
    async def unclaim(self, ctx):
        """Unclaim the current ticket."""
        if self._dedup(ctx.message.id):
            return

        try:
            if not await self._check_permissions(ctx):
                return

            thread = getattr(ctx, "thread", None)
            if thread is None:
                await ctx.send("❌ This command can only be used inside a ticket.")
                return

            record = await self._get_claim_record(thread.id)
            if record is None:
                await ctx.send("ℹ️ This ticket is not claimed.")
                return

            current_claimer_id = int(record.get("claimer_id", 0))
            is_admin = getattr(getattr(ctx.author, "guild_permissions", None), "administrator", False)

            if current_claimer_id != ctx.author.id and not is_admin:
                await ctx.send("❌ Only the supporter who claimed this ticket or an admin can unclaim it.")
                return

            await self._clear_claim_record(thread.id)
            await ctx.send("✅ This ticket has been unclaimed.")
        except Exception as error:
            await ctx.send(f"❌ Unclaim failed: {type(error).__name__}: {error}")


async def setup(bot):
    await bot.add_cog(Claim(bot))
