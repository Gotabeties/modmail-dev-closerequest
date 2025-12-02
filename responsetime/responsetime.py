import discord
from discord.ext import commands
from datetime import datetime, timezone
from core import checks
from core.models import PermissionLevel


class ResponseTime(commands.Cog):
    """Track and log response times for modmail tickets."""
    
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        self.default_config = {
            "log_channel_id": None,
            "enabled": True,
            "include_stats": True  # Include average response time in logs
        }
        self.config = None
        # Store ticket creation times {thread_id: datetime}
        self.pending_tickets = {}
        # Store response times for statistics
        self.response_times = []

    async def cog_load(self):
        """Load configuration from database."""
        self.config = await self.db.find_one({"_id": "responsetime-config"})
        if self.config is None:
            self.config = self.default_config
            await self.update_config()
        
        # Ensure all default keys exist
        missing = [k for k in self.default_config if k not in self.config]
        if missing:
            for k in missing:
                self.config[k] = self.default_config[k]
            await self.update_config()

    async def update_config(self):
        """Save configuration to database."""
        await self.db.find_one_and_update(
            {"_id": "responsetime-config"},
            {"$set": self.config},
            upsert=True,
        )

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        """Track when a new ticket is created."""
        if not self.config["enabled"]:
            return
        
        # Store the creation time for this thread
        self.pending_tickets[thread.id] = datetime.now(timezone.utc)

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, from_mod, message, anonymous, plain):
        """Track when staff first responds to a ticket."""
        if not self.config["enabled"]:
            return
        
        # Only track responses from moderators (staff)
        if not from_mod:
            return
        
        # Ignore bot messages
        if message.author.bot:
            return
        
        # Check if this ticket is pending first response
        if thread.id not in self.pending_tickets:
            return
        
        # Calculate response time
        creation_time = self.pending_tickets[thread.id]
        response_time = datetime.now(timezone.utc)
        time_delta = response_time - creation_time
        
        # Remove from pending tickets
        del self.pending_tickets[thread.id]
        
        # Store for statistics
        self.response_times.append(time_delta.total_seconds())
        
        # Log the response time
        await self.log_response_time(thread, thread.recipient, time_delta)

    async def log_response_time(self, thread, creator, time_delta):
        """Send response time log to configured channel."""
        if self.config["log_channel_id"] is None:
            print("No log channel configured for response time logging")
            return
        
        log_channel = self.bot.get_channel(self.config["log_channel_id"])
        if log_channel is None:
            print(f"Log channel {self.config['log_channel_id']} not found")
            return
        
        # Format time delta
        total_seconds = int(time_delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        if hours > 0:
            time_str = f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            time_str = f"{minutes}m {seconds}s"
        else:
            time_str = f"{seconds}s"
        
        # Create embed
        embed = discord.Embed(
            title="📊 Response Time Logged",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(
            name="Ticket Creator",
            value=f"{creator.mention} ({creator})",
            inline=True
        )
        
        embed.add_field(
            name="Ticket ID",
            value=f"`{thread.id}`",
            inline=True
        )
        
        embed.add_field(
            name="Response Time",
            value=f"⏱️ **{time_str}**",
            inline=True
        )
        
        embed.add_field(
            name="Thread Channel",
            value=thread.channel.mention if thread.channel else "N/A",
            inline=False
        )
        
        # Add statistics if enabled
        if self.config["include_stats"] and self.response_times:
            avg_seconds = sum(self.response_times) / len(self.response_times)
            avg_hours, avg_remainder = divmod(int(avg_seconds), 3600)
            avg_minutes, avg_secs = divmod(avg_remainder, 60)
            
            if avg_hours > 0:
                avg_str = f"{avg_hours}h {avg_minutes}m"
            elif avg_minutes > 0:
                avg_str = f"{avg_minutes}m {avg_secs}s"
            else:
                avg_str = f"{avg_secs}s"
            
            embed.add_field(
                name="Average Response Time",
                value=f"📈 {avg_str} (based on {len(self.response_times)} tickets)",
                inline=False
            )
        
        try:
            await log_channel.send(embed=embed)
            print(f"Response time logged: {time_str} for thread {thread.id}")
        except discord.Forbidden:
            print(f"Missing permissions to send to log channel {log_channel.id}")
        except Exception as e:
            print(f"Error logging response time: {e}")
            import traceback
            traceback.print_exc()

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.group(invoke_without_command=True)
    async def responsetime(self, ctx):
        """Configure the response time logger plugin."""
        await ctx.send_help(ctx.command)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @responsetime.command(name="setchannel")
    async def responsetime_setchannel(self, ctx, channel: discord.TextChannel):
        """Set the channel where response times will be logged."""
        self.config["log_channel_id"] = channel.id
        await self.update_config()
        await ctx.send(f"✅ Response time logs will be sent to {channel.mention}")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @responsetime.command(name="toggle")
    async def responsetime_toggle(self, ctx):
        """Enable or disable response time logging."""
        self.config["enabled"] = not self.config["enabled"]
        await self.update_config()
        
        status = "enabled" if self.config["enabled"] else "disabled"
        await ctx.send(f"✅ Response time logging is now **{status}**.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @responsetime.command(name="togglestats")
    async def responsetime_togglestats(self, ctx):
        """Toggle whether to include average response time statistics in logs."""
        self.config["include_stats"] = not self.config["include_stats"]
        await self.update_config()
        
        status = "enabled" if self.config["include_stats"] else "disabled"
        await ctx.send(f"✅ Average response time statistics are now **{status}**.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @responsetime.command(name="stats")
    async def responsetime_stats(self, ctx):
        """View response time statistics."""
        if not self.response_times:
            await ctx.send("❌ No response time data available yet.")
            return
        
        total_tickets = len(self.response_times)
        avg_seconds = sum(self.response_times) / total_tickets
        min_seconds = min(self.response_times)
        max_seconds = max(self.response_times)
        
        def format_seconds(seconds):
            hours, remainder = divmod(int(seconds), 3600)
            minutes, secs = divmod(remainder, 60)
            if hours > 0:
                return f"{hours}h {minutes}m {secs}s"
            elif minutes > 0:
                return f"{minutes}m {secs}s"
            else:
                return f"{secs}s"
        
        embed = discord.Embed(
            title="📊 Response Time Statistics",
            color=self.bot.main_color,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="Total Tickets Tracked", value=str(total_tickets), inline=True)
        embed.add_field(name="Average Response Time", value=format_seconds(avg_seconds), inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="Fastest Response", value=format_seconds(min_seconds), inline=True)
        embed.add_field(name="Slowest Response", value=format_seconds(max_seconds), inline=True)
        
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @responsetime.command(name="resetstats")
    async def responsetime_resetstats(self, ctx):
        """Reset response time statistics."""
        self.response_times.clear()
        await ctx.send("✅ Response time statistics have been reset.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @responsetime.command(name="config")
    async def responsetime_config(self, ctx):
        """View current response time logger configuration."""
        log_channel = self.bot.get_channel(self.config["log_channel_id"]) if self.config["log_channel_id"] else None
        
        embed = discord.Embed(
            title="Response Time Logger Configuration",
            color=self.bot.main_color
        )
        
        embed.add_field(
            name="Log Channel",
            value=log_channel.mention if log_channel else "Not set",
            inline=False
        )
        
        embed.add_field(
            name="Logging Enabled",
            value="✅ Yes" if self.config["enabled"] else "❌ No",
            inline=True
        )
        
        embed.add_field(
            name="Include Statistics",
            value="✅ Yes" if self.config["include_stats"] else "❌ No",
            inline=True
        )
        
        embed.add_field(
            name="Tickets Tracked",
            value=str(len(self.response_times)),
            inline=True
        )
        
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(ResponseTime(bot))