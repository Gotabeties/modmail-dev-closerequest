import asyncio
import re
import discord
from discord.ext import commands
from discord.ext.commands.view import StringView
from copy import copy

from core import checks
from core.models import PermissionLevel, DummyMessage
from core.utils import normalize_alias

async def invoke_command(command_text, bot, thread, message):
    """Invoke a command like .close"""
    ctxs = []
    aliases = normalize_alias(command_text)
    for alias in aliases:
        view = StringView(bot.prefix + alias)
        ctx_ = commands.Context(prefix=bot.prefix, view=view, bot=bot, message=message)
        ctx_.thread = thread
        discord.utils.find(view.skip_string, await bot.get_prefix())
        ctx_.invoked_with = view.get_word().lower()
        ctx_.command = bot.all_commands.get(ctx_.invoked_with)
        ctxs.append(ctx_)

    for ctx in ctxs:
        if ctx.command:
            old_checks = copy(ctx.command.checks)
            ctx.command.checks = [checks.has_permissions(PermissionLevel.INVALID)]
            await bot.invoke(ctx)
            ctx.command.checks = old_checks

def parse_time(time_str):
    """Parse time string to seconds."""
    if not time_str:
        return None
    
    time_str = time_str.lower().strip()
    units = {
        's': 1, 'sec': 1, 'second': 1, 'seconds': 1,
        'm': 60, 'min': 60, 'minute': 60, 'minutes': 60,
        'h': 3600, 'hr': 3600, 'hour': 3600, 'hours': 3600,
        'd': 86400, 'day': 86400, 'days': 86400
    }
    pattern = r'(\d+)\s*([a-z]+)'
    matches = re.findall(pattern, time_str)
    if not matches:
        return None
    total_seconds = 0
    for amount, unit in matches:
        if unit in units:
            total_seconds += int(amount) * units[unit]
        else:
            return None
    return total_seconds if total_seconds > 0 else None

def format_time(seconds):
    """Format seconds into human-readable string."""
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
    def __init__(self, bot, thread, closer, close_message, all_messages):
        super().__init__(timeout=None)
        self.bot = bot
        self.thread = thread
        self.closer = closer
        self.close_message = close_message
        self.all_messages = all_messages  # All messages sent (thread channel + DM)
        self.result = None

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.thread.recipient.id:
            return await interaction.response.send_message("Only the ticket creator can use these buttons.", ephemeral=True)
        
        await interaction.response.defer()
        self.result = "closed"
        self.stop()
        
        # Update all messages (both thread and DM)
        embed = discord.Embed(
            title="Ticket Closed",
            description="This ticket has been closed by the user.",
            color=discord.Color.green()
        )
        
        for msg in self.all_messages:
            try:
                await msg.edit(embed=embed, view=None)
            except:
                pass
        
        # Run the close command
        close_msg = self.close_message if self.close_message else "Ticket closed by user request."
        dummy = DummyMessage(copy(self.all_messages[0]))
        dummy.author = self.thread.recipient
        dummy.content = f"{self.bot.prefix}close {close_msg}"
        await invoke_command(f"close {close_msg}", self.bot, self.thread, dummy)

    @discord.ui.button(label="Keep Open", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.thread.recipient.id:
            return await interaction.response.send_message("Only the ticket creator can use these buttons.", ephemeral=True)
        
        await interaction.response.defer()
        self.result = "cancelled"
        self.stop()
        
        # Update all messages (both thread and DM)
        embed = discord.Embed(
            title="Close Request Cancelled",
            description="This ticket will remain open.",
            color=discord.Color.red()
        )
        
        for msg in self.all_messages:
            try:
                await msg.edit(embed=embed, view=None)
            except:
                pass

class CloseRequest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.plugin_db.get_partition(self)
        self.default_config = {
            "default_message": "Your support ticket appears to be resolved. Reason: $reason. This ticket will auto-close in $time if no response is given.",
            "auto_close_message": "This ticket has been automatically closed due to inactivity.",
            "default_time": 21600  # 6 hours in seconds
        }
        self.config = None

    async def cog_load(self):
        self.config = await self.db.find_one({"_id": "closerequest-config"})
        if self.config is None:
            self.config = self.default_config
            await self.update_config()
        missing = [k for k in self.default_config if k not in self.config]
        if missing:
            for k in missing:
                self.config[k] = self.default_config[k]
            await self.update_config()

    async def update_config(self):
        await self.db.find_one_and_update(
            {"_id": "closerequest-config"},
            {"$set": self.config},
            upsert=True,
        )

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command(name="closerequest")
    async def closerequest(self, ctx, *, args: str = None):
        """Send a close request to the user with interactive buttons. Format: <reason> [optional time]"""
        thread = ctx.thread
        
        if not args:
            await ctx.send("❌ You must provide a reason. Usage: `closerequest <reason> [optional time]`")
            return
        
        auto_close_time = self.config["default_time"]  # Default to config time
        reason = args  # Default: entire args is the reason
        
        # Parse arguments to extract reason and optional time
        # Look for time patterns at the end of the args
        words = args.split()
        time_words = []
        reason_words = []
        
        # Iterate backwards to find time at the end
        found_time = False
        for i in range(len(words) - 1, -1, -1):
            word = words[i]
            # Check if this word looks like a time component
            if re.search(r'\d', word) or word.lower() in ['s', 'sec', 'second', 'seconds', 'm', 'min', 'minute', 'minutes', 'h', 'hr', 'hour', 'hours', 'd', 'day', 'days']:
                time_words.insert(0, word)
                found_time = True
            else:
                # Once we hit a non-time word, everything else is reason
                reason_words = words[:i+1]
                break
        
        # If we found potential time words, try to parse them
        if found_time and time_words:
            potential_time_str = ' '.join(time_words)
            parsed_time = parse_time(potential_time_str)
            if parsed_time:
                auto_close_time = parsed_time
                reason = ' '.join(reason_words) if reason_words else args
            # If parsing failed, treat everything as reason
        
        if not reason.strip():
            await ctx.send("❌ You must provide a reason. Usage: `closerequest <reason> [optional time]`")
            return

        # Get message template and replace placeholders
        message_template = self.config["default_message"]
        formatted_time = format_time(auto_close_time)
        message_text = message_template.replace("$time", formatted_time).replace("$reason", reason)
        close_message = self.config["auto_close_message"]

        # Create the embed
        embed = discord.Embed(
            title="Close Request",
            description=message_text,
            color=self.bot.main_color
        )

        # Send the close request message
        try:
            # Send to thread channel (staff can see it)
            thread_msg = await thread.channel.send(embed=embed)
            
            # Send to user's DM
            try:
                user_msg = await thread.recipient.send(embed=embed)
                messages = [thread_msg, user_msg]
            except discord.Forbidden:
                # User has DMs disabled, only thread message exists
                messages = [thread_msg]
                await ctx.send("⚠️ Close request sent to thread, but user has DMs disabled.")
            
            # Create view with all messages
            view = CloseRequestView(self.bot, thread, ctx.author, close_message, messages)
            
            # Edit all messages to add buttons
            for msg in messages:
                try:
                    await msg.edit(view=view)
                except Exception as e:
                    print(f"Error adding buttons to message: {e}")
                    
        except discord.Forbidden:
            await ctx.send("❌ Could not send message to the user. They might have DMs disabled.")
            return
        except Exception as e:
            await ctx.send(f"❌ An error occurred: {e}")
            import traceback
            print(traceback.format_exc())
            return
        
        await ctx.send(f"✅ Close request sent. Automatically closing the ticket in {formatted_time}.")

        # Handle auto-close
        try:
            done, pending = await asyncio.wait(
                [asyncio.create_task(view.wait()), asyncio.create_task(asyncio.sleep(auto_close_time))],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            
            # If view.result is None, it means timeout occurred (no button was clicked)
            if view.result is None:
                # Update all messages
                embed.title = "Ticket Auto-Closed"
                embed.description = self.config["auto_close_message"]
                embed.color = discord.Color.orange()
                
                for msg in messages:
                    try:
                        await msg.edit(embed=embed, view=None)
                    except:
                        pass
                
                # Close the thread using the close command
                dummy = DummyMessage(copy(messages[0]))
                dummy.author = thread.recipient
                dummy.content = f"{self.bot.prefix}close {self.config['auto_close_message']}"
                await invoke_command(f"close {self.config['auto_close_message']}", self.bot, thread, dummy)
        except Exception as e:
            print(f"Error in auto-close: {e}")
            import traceback
            print(traceback.format_exc())

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.group(invoke_without_command=True)
    async def closerequestconfig(self, ctx):
        """Configure the close request plugin."""
        await ctx.send_help(ctx.command)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @closerequestconfig.command(name="setmessage")
    async def closerequestconfig_setmessage(self, ctx, *, message: str):
        """Set the default close request message. Use $reason for the reason and $time for the auto-close time."""
        self.config["default_message"] = message
        await self.update_config()
        await ctx.send("✅ Default close request message updated. Use `$reason` and `$time` in your message as placeholders.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @closerequestconfig.command(name="setautoclosemessage")
    async def closerequestconfig_setautoclosemessage(self, ctx, *, message: str):
        """Set the auto-close message."""
        self.config["auto_close_message"] = message
        await self.update_config()
        await ctx.send("✅ Auto-close message updated.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @closerequestconfig.command(name="settime")
    async def closerequestconfig_settime(self, ctx, *, time: str):
        """Set the default auto-close time (e.g., 30m, 6h, 1d)."""
        parsed_time = parse_time(time)
        if parsed_time is None:
            await ctx.send("❌ Invalid time format. Use formats like: 30m, 6h, 1d")
            return
        
        self.config["default_time"] = parsed_time
        await self.update_config()
        await ctx.send(f"✅ Default auto-close time set to {format_time(parsed_time)}.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @closerequestconfig.command(name="view")
    async def closerequestconfig_view(self, ctx):
        """View current configuration."""
        embed = discord.Embed(title="Close Request Configuration", color=self.bot.main_color)
        embed.add_field(name="Default Message", value=self.config["default_message"], inline=False)
        embed.add_field(name="Auto-Close Message", value=self.config["auto_close_message"], inline=False)
        embed.add_field(name="Default Time", value=format_time(self.config["default_time"]), inline=False)
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(CloseRequest(bot))