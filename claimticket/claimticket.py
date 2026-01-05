import discord
from discord.ext import commands

from core import checks
from core.checks import PermissionLevel


class ClaimThread(commands.Cog):
    """Simple claim system that appends a name to the thread title"""

    def __init__(self, bot):
        self.bot = bot
        self.db = self.bot.plugin_db.get_partition(self)

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    async def claim(self, ctx, *, name: str = None):
        channel = ctx.thread.channel
        claimer_name = name or ctx.author.display_name

        data = await self.db.find_one({"thread_id": str(channel.id)})
        if data:
            await ctx.send("This thread is already claimed.")
            return

        original_name = channel.name
        claimer_name = claimer_name.replace(" ", "-")

        new_name = f"{original_name}-{claimer_name}"[:100]

        try:
            await channel.edit(name=new_name)
        except discord.Forbidden:
            await ctx.send("I don't have permission to rename this thread.")
            return
        except discord.HTTPException:
            await ctx.send("Failed to rename the thread.")
            return

        await self.db.insert_one({
            "thread_id": str(channel.id),
            "original_name": original_name,
            "claimer": claimer_name
        })

        await ctx.send(f"Thread claimed as **{claimer_name}**")

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command(name="unclaim")
    async def unclaim(self, ctx):
        channel = ctx.thread.channel

        data = await self.db.find_one({"thread_id": str(channel.id)})
        if not data:
            await ctx.send("This thread is not claimed.")
            return

        try:
            await channel.edit(name=data["original_name"])
        except discord.Forbidden:
            await ctx.send("I don't have permission to rename this thread.")
            return
        except discord.HTTPException:
            await ctx.send("Failed to rename the thread.")
            return

        await self.db.delete_one({"thread_id": str(channel.id)})
        await ctx.send("Thread unclaimed.")

async def setup(bot):
    await bot.add_cog(ClaimThread(bot))
