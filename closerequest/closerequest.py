from copy import copy
import asyncio
import re
import discord
from discord.ext import commands

from core import checks
from core.models import DummyMessage, PermissionLevel

def parse_time(time_str):
    """
    Parse time string to seconds.
    Supports: 1h, 1hr, 1hour, 1hours, 1d, 1day, 1m, 1min, 1minute, 1s, 1sec, 1second
    Also supports combinations like: 1h 30m, 2d 3h 15m
    """
    if not time_str:
        return None
    
    # Convert to lowercase and remove extra spaces
    time_str = time_str.lower().strip()
    
    # Time units in seconds
    units = {
        's': 1, 'sec': 1, 'second': 1, 'seconds': 1,
        'm': 60, 'min': 60, 'minute': 60, 'minutes': 60,
        'h': 3600, 'hr': 3600, 'hour': 3600, 'hours': 3600,
        'd': 86400, 'day': 86400, 'days': 86400
    }
    
    # Find all number + unit combinations
    pattern = r'(\d+)\s*([a-z]+)'
    matches = re.findall(pattern, time_str)
    
    if not matches:
        return None
    
    total_seconds = 0
    for amount, unit in matches:
        if unit in units:
            total_seconds += int(amount) * units[unit]
        else:
            return None  # Invalid unit
    
    return total_seconds if total_seconds > 0 else None

def format_time(seconds):
    """Format seconds into a human-readable string."""
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    elif seconds < 3600:
        minutes = seconds // 60
        remaining_seconds = seconds % 60
        if remaining_seconds == 0:
            return f"{minutes} minute{'s' if minutes != 1 else ''}"
        return f"{minutes} minute{'s' if minutes != 1 else ''} {remaining_seconds} second{'s' if remaining_seconds != 1 else ''}"
    elif seconds < 86400:
        hours = seconds // 3600
        remaining_minutes = (seconds % 3600) // 60
        if remaining_minutes == 0:
            return f"{hours} hour{'s' if hours != 1 else ''}"
        return f"{hours} hour{'s' if hours != 1 else ''} {remaining_minutes} minute{'s' if remaining_minutes != 1 else ''}"
    else:
        days = seconds // 86400
        remaining_hours = (seconds % 86400) // 3600
        if remaining_hours == 0:
            return f"{days} day{'s' if days != 1 else ''}"
        return f"{days} day{'s' if days != 1 else ''} {remaining_hours} hour{'s' if remaining_hours != 1 else ''}"

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
    async def closerequest(self, ctx, *, args: str = ""):
        """Send a close request to the user with interactive buttons."""
        
        # Parse arguments
        auto_close_time = None
        custom_message = None
        
        if args:
            # Try to parse the first part as a time
            parts = args.split(None, 1)
            first_word = parts[0]
            
            # Check if it looks like a time string (contains digits)
            if re.search(r'\d', first_word):
                # Try to get more words that might be part of the time (e.g., "1 hour 30 minutes")
                potential_time = first_word
                remaining_parts = parts[1].split() if len(parts) > 1 else []
                
                # Keep adding words while they look like time components
                temp_remaining = []
                for word in remaining_parts:
                    if re.search(r'\d', word) or word.lower() in ['s', 'sec', 'second', 'seconds', 'm', 'min', 'minute', 'minutes', 'h', 'hr', 'hour', 'hours', 'd', 'day', 'days']:
                        potential_time += ' ' + word
                    else:
                        temp_remaining.append(word)
                
                parsed_time = parse_time(potential_time)
                
                if parsed_time:
                    auto_close_time = parsed_time
                    custom_message = ' '.join(temp_remaining) if temp_remaining else None
                else:
                    # Not a valid time, treat everything as message
                    custom_message = args
            else:
                # No digits in first word, everything is custom message
                custom_message = args
        
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
            # Format time nicely for display
            time_display = format_time(auto_close_time)
            embed.set_footer(text=f"This ticket will auto-close in {time_display} if no response is given.")
        
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