from copy import copy
import asyncio
import discord
from discord.ext import commands

from core import checks
from core.models import DummyMessage, PermissionLevel

class CloseRequestView(discord.ui.View):
    def __init__(self, bot, thread, closer, message):
        super().__init__(timeout=None)
        self.bot = bot
        self.thread = thread
        self.closer = closer
        self.message = message
        self.result = None

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.thread.recipient.id:
            return await interaction.response.send_message("Only the ticket creator can use these buttons.", ephemeral=True)
        
        await interaction.response.defer()
        self.result = "closed"
        self.stop()
        
        # Close the thread
        await self.thread.close(closer=self.closer, silent=False, delete_channel=False, message=self.message)

    @discord.ui.button(label="Keep Open", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.thread.recipient.id:
            return await interaction.response.send_message("Only the ticket creator can use these buttons.", ephemeral=True)
        
        await interaction.response.defer()
        self.result = "cancelled"
        self.stop()
        
        # Edit message to show it was cancelled
        embed = discord.Embed(
            title="Close Request Cancelled",
            description="This ticket will remain open.",
            color=discord.Color.red()
        )
        await self.message.edit(embed=embed, view=None)

class CloseRequest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.plugin_db.get_partition(self)
        self.default_config = {
            "default_message": "Your support ticket appears to be resolved. Please click the checkmark below to close this ticket, or click the X if you need more support.",
            "auto_close_message": "This ticket has been automatically closed due to inactivity."
        }
        self.config = None

    async def cog_load(self):
        self.config = await self.db.find_one({"_id": "closerequest-config"})
        if self.config is None:
            self.config = self.default_config
            await self.update_config()
        
        # Add any missing keys from default config
        missing = []
        for key in self.default_config.keys():
            if key not in self.config:
                missing.append(key)
        
        if missing:
            for key in missing:
                self.config[key] = self.default_config[key]
            await self.update_config()

    async def update_config(self):
        await self.db.find_one_and_update(
            {"_id": "closerequest-config"},
            {"$set": self.config},
            upsert=True,
        )

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    async def closerequest(self, ctx, auto_close_time: int = None, *, custom_message: str = None):
        """
        Send a close request to the user with interactive buttons.
        
        Usage:
        - closerequest
        - closerequest 60
        - closerequest Custom message here
        - closerequest 60 Custom message here
        
        If auto_close_time is provided (in seconds), the ticket will auto-close after that time if no response.
        """
        thread = ctx.thread
        
        # Determine the message to use
        if custom_message:
            message_text = custom_message
        else:
            message_text = self.config["default_message"]
        
        # Create embed
        embed = discord.Embed(
            title="Close Request",
            description=message_text,
            color=discord.Color.blurple()
        )
        
        if auto_close_time:
            embed.set_footer(text=f"This ticket will auto-close in {auto_close_time} seconds if no response is given.")
        
        # Create the view with buttons
        view = CloseRequestView(self.bot, thread, ctx.author, self.config["auto_close_message"] if auto_close_time else None)
        
        # Send the message to the user
        dummy_message = DummyMessage(copy(thread._genesis_message))
        dummy_message.author = self.bot.modmail_guild.me
        dummy_message.content = None
        dummy_message.embeds = [embed]
        
        msgs, _ = await thread.reply(dummy_message, anonymous=False)
        
        # Find the message sent to the recipient
        recipient_msg = None
        for m in msgs:
            if m.channel.recipient == thread.recipient:
                recipient_msg = m
                break
        
        if recipient_msg:
            await recipient_msg.edit(view=view)
        
        # Confirm to staff
        await ctx.send(f"Close request sent to {thread.recipient.mention}.")
        
        # Handle auto-close if time is specified
        if auto_close_time:
            # Wait for either the timeout or user interaction
            done, pending = await asyncio.wait(
                [asyncio.create_task(view.wait()), asyncio.create_task(asyncio.sleep(auto_close_time))],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Cancel pending tasks
            for task in pending:
                task.cancel()
            
            # If view didn't stop (no button pressed), auto-close
            if view.result is None:
                await thread.close(closer=ctx.author, silent=False, delete_channel=False, message=self.config["auto_close_message"])
                
                # Update the message to show it was auto-closed
                if recipient_msg:
                    embed.title = "Ticket Auto-Closed"
                    embed.description = self.config["auto_close_message"]
                    embed.color = discord.Color.orange()
                    embed.set_footer(text="This ticket was automatically closed due to no response.")
                    try:
                        await recipient_msg.edit(embed=embed, view=None)
                    except:
                        pass

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.group(invoke_without_command=True)
    async def closerequestconfig(self, ctx):
        """Configure the close request plugin."""
        await ctx.send_help(ctx.command)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @closerequestconfig.command(name="setmessage")
    async def closerequestconfig_setmessage(self, ctx, *, message: str):
        """Set the default close request message."""
        self.config["default_message"] = message
        await self.update_config()
        await ctx.send("Default close request message updated.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @closerequestconfig.command(name="setautoclosemessage")
    async def closerequestconfig_setautoclosemessage(self, ctx, *, message: str):
        """Set the auto-close message."""
        self.config["auto_close_message"] = message
        await self.update_config()
        await ctx.send("Auto-close message updated.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @closerequestconfig.command(name="view")
    async def closerequestconfig_view(self, ctx):
        """View current configuration."""
        embed = discord.Embed(title="Close Request Configuration", color=discord.Color.blurple())
        embed.add_field(name="Default Message", value=self.config["default_message"], inline=False)
        embed.add_field(name="Auto-Close Message", value=self.config["auto_close_message"], inline=False)
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(CloseRequest(bot))