import discord
from discord.ext import commands

from core import checks
from core.checks import PermissionLevel


class ClaimThread(commands.Cog):
    """Simple claim system that appends claimer name to the thread title"""

    def __init__(self, bot):
        self.bot = bot
        self.db = self.bot.plugin_db.get_partition(self)

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    async def claim(self, ctx):
        channel = ctx.thread.channel
        user = ctx.author

        existing = await self.db.find_one(
            {"thread_id": str(channel.id)}
        )

        if existing:
            await ctx.send("This thread is already claimed.")
            return

        original_name = channel.name
        new_name = f"{original_name}-{user.name}"

        # Discord has a 100 character limit
        new_name = new_name[:100]

        await channel.edit(name=new_name)

        await self.db.insert_one({
            "thread_id": str(channel.id),
            "original_name": original_name,
            "claimer_id": str(user.id)
        })

        await ctx.send(f"Thread claimed by **{user.display_name}**")

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    async def unclaim(self, ctx):
        channel = ctx.thread.channel
        user = ctx.author

        data = await self.db.find_one(
            {"thread_id": str(channel.id)}
        )

        if not data:
            await ctx.send("This thread is not claimed.")
            return

        if str(user.id) != data["claimer_id"]:
            await ctx.send("You did not claim this thread.")
            return

        await channel.edit(name=data["original_name"])
        await self.db.delete_one({"thread_id": str(channel.id)})

        await ctx.send("Thread unclaimed.")

async def setup(bot):
    await bot.add_cog(ClaimThread(bot))
