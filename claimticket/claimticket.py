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
    @commands.command()
    async def claim(self, ctx):
        """Claim a thread with your name"""
        # Check if in a thread
        if not hasattr(ctx, "thread") or ctx.thread is None:
            return await ctx.send("This command can only be used inside a Modmail thread.")
        
        channel = ctx.thread.channel
        
        # Check if already claimed
        data = await self.db.find_one({"thread_id": str(channel.id)})
        if data:
            return await ctx.send("This thread is already claimed.")
        
        # Get claimer name
        claimer_name = ctx.author.display_name.replace(" ", "-")
        
        # Store original name
        original_name = channel.name
        
        # Create new name
        new_name = f"{original_name}-{claimer_name}"
        if len(new_name) > 100:
            new_name = new_name[:100]
        
        # Rename channel
        try:
            await channel.edit(name=new_name)
        except discord.Forbidden:
            return await ctx.send("I don't have permission to rename this thread.")
        except discord.HTTPException as e:
            return await ctx.send(f"Failed to rename thread: {e}")
        
        # Save to database
        await self.db.insert_one({
            "thread_id": str(channel.id),
            "original_name": original_name,
            "claimer": claimer_name
        })
        
        await ctx.send(f"Thread claimed as **{claimer_name}**")

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.command()
    async def claimfor(self, ctx, *, name: str):
        """Claim a thread with a custom name"""
        # Check if in a thread
        if not hasattr(ctx, "thread") or ctx.thread is None:
            return await ctx.send("This command can only be used inside a Modmail thread.")
        
        channel = ctx.thread.channel
        
        # Check if already claimed
        data = await self.db.find_one({"thread_id": str(channel.id)})
        if data:
            return await ctx.send("This thread is already claimed.")
        
        # Get claimer name
        claimer_name = name.replace(" ", "-")
        
        # Store original name
        original_name = channel.name
        
        # Create new name
        new_name = f"{original_name}-{claimer_name}"
        if len(new_name) > 100:
            new_name = new_name[:100]
        
        # Rename channel
        try:
            await channel.edit(name=new_name)
        except discord.Forbidden:
            return await ctx.send("I don't have permission to rename this thread.")
        except discord.HTTPException as e:
            return await ctx.send(f"Failed to rename thread: {e}")
        
        # Save to database
        await self.db.insert_one({
            "thread_id": str(channel.id),
            "original_name": original_name,
            "claimer": claimer_name
        })
        
        await ctx.send(f"Thread claimed as **{claimer_name}**")

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.command()
    async def unclaim(self, ctx):
        """Unclaim a thread"""
        # Check if in a thread
        if not hasattr(ctx, "thread") or ctx.thread is None:
            return await ctx.send("This command can only be used inside a Modmail thread.")
        
        channel = ctx.thread.channel
        
        # Check if claimed
        data = await self.db.find_one({"thread_id": str(channel.id)})
        if not data:
            return await ctx.send("This thread is not claimed.")
        
        # Get original name
        original_name = data.get("original_name")
        if not original_name:
            await self.db.delete_one({"thread_id": str(channel.id)})
            return await ctx.send("Database error: original name not found. Claim data removed.")
        
        # Rename back
        try:
            await channel.edit(name=original_name)
        except discord.Forbidden:
            return await ctx.send("I don't have permission to rename this thread.")
        except discord.HTTPException as e:
            return await ctx.send(f"Failed to rename thread: {e}")
        
        # Remove from database
        await self.db.delete_one({"thread_id": str(channel.id)})
        
        await ctx.send("Thread unclaimed.")


async def setup(bot):
    await bot.add_cog(ClaimThread(bot))