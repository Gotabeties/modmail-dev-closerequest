import discord
from discord.ext import commands
from typing import Optional

from core import checks
from core.checks import PermissionLevel


class ClaimThread(commands.Cog):
    """Claim system that renames the thread"""

    def __init__(self, bot):
        self.bot = bot
        self.db = self.bot.plugin_db.get_partition(self)

    def _get_thread_channel(self, ctx):
        if not hasattr(ctx, "thread") or ctx.thread is None:
            return None
        return ctx.thread.channel

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.command()
    async def claim(self, ctx, name: Optional[str] = None):
        try:
            channel = self._get_thread_channel(ctx)
            if not channel:
                await ctx.send("This command can only be used inside a Modmail thread.")
                return

            # Get the claimer name
            if name:
                claimer_name = str(name).strip().replace(" ", "-")
            else:
                claimer_name = ctx.author.display_name.replace(" ", "-")

            data = await self.db.find_one({"thread_id": str(channel.id)})
            if data:
                await ctx.send("This thread is already claimed.")
                return

            original_name = channel.name
            
            # Remove any existing claim suffix
            if "-" in original_name:
                parts = original_name.rsplit("-", 1)
                if len(parts) == 2 and parts[1] and not parts[1][0].isdigit():
                    original_name = parts[0]
            
            new_name = f"{original_name}-{claimer_name}"[:100]

            try:
                await channel.edit(name=new_name)
            except discord.Forbidden:
                await ctx.send("I don't have permission to rename this thread.")
                return
            except discord.HTTPException as e:
                if hasattr(e, 'status') and e.status == 429:
                    await ctx.send("Please wait before claiming - Discord rate limit (max 2 name changes per 10 minutes).")
                else:
                    await ctx.send(f"Failed to rename the thread: {e}")
                return

            await self.db.insert_one({
                "thread_id": str(channel.id),
                "original_name": original_name,
                "claimer": claimer_name
            })

            await ctx.send(f"Thread claimed as **{claimer_name}**")
            
        except Exception as e:
            await ctx.send(f"Error in claim command: {type(e).__name__}: {e}")
            raise  # This will show the full traceback in console

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.command()
    async def unclaim(self, ctx):
        try:
            channel = self._get_thread_channel(ctx)
            if not channel:
                await ctx.send("This command can only be used inside a Modmail thread.")
                return

            data = await self.db.find_one({"thread_id": str(channel.id)})
            if not data:
                await ctx.send("This thread is not claimed.")
                return

            original_name = data.get("original_name")
            if not original_name:
                await ctx.send("Database error: original name not found. Removing claim data.")
                await self.db.delete_one({"thread_id": str(channel.id)})
                return

            try:
                await channel.edit(name=original_name)
            except discord.Forbidden:
                await ctx.send("I don't have permission to rename this thread.")
                return
            except discord.HTTPException as e:
                if hasattr(e, 'status') and e.status == 429:
                    await ctx.send("Please wait before unclaiming - Discord rate limit (max 2 name changes per 10 minutes).")
                else:
                    await ctx.send(f"Failed to rename the thread: {e}")
                return

            await self.db.delete_one({"thread_id": str(channel.id)})
            await ctx.send("Thread unclaimed.")
            
        except Exception as e:
            await ctx.send(f"Error in unclaim command: {type(e).__name__}: {e}")
            raise  # This will show the full traceback in console


async def setup(bot):
    await bot.add_cog(ClaimThread(bot))