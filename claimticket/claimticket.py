import discord
from discord.ext import commands

from core import checks
from core.checks import PermissionLevel


class ClaimThread(commands.Cog):
    """Claim system that renames the thread"""

    def __init__(self, bot):
        self.bot = bot
        self.db = self.bot.plugin_db.get_partition(self)

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    async def claim(self, ctx, *, name: str = None):
        channel = ctx.thread.channel
        claimer_name = name or ctx.author.display_name
        claimer_name = claimer_name.replace(" ", "-")

        data = await self.db.find_one({"thread_id": str(channel.id)})
        if data:
            await ctx.send("This thread is already claimed.")
            return

        original_name = channel.name
        new_name = f"{original_name}-{claimer_name}"[:100]

        await channel.edit(name=new_name)

        await self.db.insert_one({
            "thread_id": str(channel.id),
            "original_name": original_name,
            "claimer": claimer_name
        })

        await ctx.send(f"Thread claimed as **{claimer_name}**")

    # IMPORTANT: primary name is NOT "unclaim"
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command(name="clearclaim", aliases=["unclaim"])
    async def clearclaim(self, ctx):
        channel = ctx.thread.channel

        data = await self.db.find_one({"thread_id": str(channel.id)})
        if not data:
            await ctx.send("This thread is not claimed.")
            return

        await channel.edit(name=data["original_name"])
        await self.db.delete_one({"thread_id": str(channel.id)})

        await ctx.send("Thread unclaimed.")

async def setup(bot):
    await bot.add_cog(ClaimThread(bot))
