import asyncio
import re
import discord
from discord.ext import commands
from copy import copy

from core import checks
from core.models import PermissionLevel, DummyMessage

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
        
        # Actually close the thread
        await self.thread.close(
            closer=self.closer,
            silent=False,
            delete_channel=False,
            message=self.close_message if self.close_message else "Ticket closed by user request."
        )

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
            "default_message": "Your support ticket appears to be resolved. Please click the checkmark below to close this ticket, or click the X if you need more support.",
            "auto_close_message": "This ticket has been automatically closed due to inactivity."
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
        """Send a close request to the user with interactive buttons."""
        thread = ctx.thread
        auto_close_time = None
        custom_message = None
        
        # Parse arguments for time and custom message
        if args:
            parts = args.split(None, 1)
            first_word = parts[0]
            if re.search(r'\d', first_word):
                potential_time = first_word
                remaining_parts = parts[1].split() if len(parts) > 1 else []
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
                    custom_message = args
            else:
                custom_message = args

        message_text = custom_message if custom_message else self.config["default_message"]
        close_message = self.config["auto_close_message"] if auto_close_time else None

        # Create the embed
        embed = discord.Embed(
            title="Close Request",
            description=message_text,
            color=self.bot.main_color
        )
        if auto_close_time:
            embed.set_footer(text=f"This ticket will auto-close in {format_time(auto_close_time)} if no response is given.")

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
        
        await ctx.send(f"✅ Close request sent to {thread.recipient.mention}.")

        # Handle auto-close if time was specified
        if auto_close_time:
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
                    embed.set_footer(text="This ticket was automatically closed due to no response.")
                    
                    for msg in messages:
                        try:
                            await msg.edit(embed=embed, view=None)
                        except:
                            pass
                    
                    # Close the thread
                    await thread.close(
                        closer=ctx.author,
                        silent=False,
                        delete_channel=False,
                        message=self.config["auto_close_message"]
                    )
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
        """Set the default close request message."""
        self.config["default_message"] = message
        await self.update_config()
        await ctx.send("✅ Default close request message updated.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @closerequestconfig.command(name="setautoclosemessage")
    async def closerequestconfig_setautoclosemessage(self, ctx, *, message: str):
        """Set the auto-close message."""
        self.config["auto_close_message"] = message
        await self.update_config()
        await ctx.send("✅ Auto-close message updated.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @closerequestconfig.command(name="view")
    async def closerequestconfig_view(self, ctx):
        """View current configuration."""
        embed = discord.Embed(title="Close Request Configuration", color=self.bot.main_color)
        embed.add_field(name="Default Message", value=self.config["default_message"], inline=False)
        embed.add_field(name="Auto-Close Message", value=self.config["auto_close_message"], inline=False)
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(CloseRequest(bot))