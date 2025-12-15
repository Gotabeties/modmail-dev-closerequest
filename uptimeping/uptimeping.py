import discord
from discord.ext import commands, tasks
from datetime import datetime, timezone
from core import checks
from core.models import PermissionLevel
import aiohttp
import asyncio


class HTTPPing(commands.Cog):
    """Periodically send HTTP requests to a configured URL."""
    
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        self.default_config = {
            "url": None,
            "method": "GET",  # HEAD, GET, or POST
            "enabled": False,
            "interval": 60,  # seconds (default 1 minute)
            "log_channel_id": None,
            "log_failures": True,
            "log_successes": False,
            "timeout": 10,  # request timeout in seconds
            "headers": {},  # custom headers
            "body": None  # for POST requests
        }
        self.config = None
        self.session = None
        # Statistics
        self.stats = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "last_success": None,
            "last_failure": None,
            "last_status_code": None
        }

    async def cog_load(self):
        """Load configuration from database."""
        self.config = await self.db.find_one({"_id": "httpping-config"})
        if self.config is None:
            self.config = self.default_config.copy()
            await self.update_config()
        
        # Ensure all default keys exist
        for k, v in self.default_config.items():
            if k not in self.config:
                self.config[k] = v
        await self.update_config()
        
        # Load statistics
        stats_data = await self.db.find_one({"_id": "httpping-stats"})
        if stats_data:
            for key in self.stats:
                if key in stats_data:
                    self.stats[key] = stats_data[key]
        
        # Create aiohttp session
        self.session = aiohttp.ClientSession()
        
        # Start the ping task if enabled
        if self.config["enabled"] and self.config["url"]:
            self.ping_task.change_interval(seconds=self.config["interval"])
            self.ping_task.start()

    async def cog_unload(self):
        """Clean up when the cog is unloaded."""
        if self.ping_task.is_running():
            self.ping_task.cancel()
        if self.session:
            await self.session.close()

    async def update_config(self):
        """Save configuration to database."""
        await self.db.find_one_and_update(
            {"_id": "httpping-config"},
            {"$set": self.config},
            upsert=True,
        )
    
    async def save_stats(self):
        """Save statistics to database."""
        await self.db.find_one_and_update(
            {"_id": "httpping-stats"},
            {"$set": self.stats},
            upsert=True,
        )

    @tasks.loop(seconds=60)
    async def ping_task(self):
        """Periodically ping the configured URL."""
        if not self.config["enabled"] or not self.config["url"]:
            return
        
        url = self.config["url"]
        method = self.config["method"].upper()
        
        try:
            # Prepare request parameters
            kwargs = {
                "timeout": aiohttp.ClientTimeout(total=self.config["timeout"]),
                "headers": self.config.get("headers", {})
            }
            
            # Add body for POST requests
            if method == "POST" and self.config.get("body"):
                kwargs["json"] = self.config["body"]
            
            # Make the request
            async with getattr(self.session, method.lower())(url, **kwargs) as response:
                self.stats["total_requests"] += 1
                self.stats["last_status_code"] = response.status
                
                if response.status < 400:
                    # Success
                    self.stats["successful_requests"] += 1
                    self.stats["last_success"] = datetime.now(timezone.utc).isoformat()
                    
                    if self.config["log_successes"]:
                        await self.log_request(True, response.status)
                else:
                    # HTTP error
                    self.stats["failed_requests"] += 1
                    self.stats["last_failure"] = datetime.now(timezone.utc).isoformat()
                    
                    if self.config["log_failures"]:
                        await self.log_request(False, response.status, f"HTTP {response.status}")
                
                await self.save_stats()
                
        except asyncio.TimeoutError:
            self.stats["total_requests"] += 1
            self.stats["failed_requests"] += 1
            self.stats["last_failure"] = datetime.now(timezone.utc).isoformat()
            
            if self.config["log_failures"]:
                await self.log_request(False, None, "Request timeout")
            
            await self.save_stats()
            
        except Exception as e:
            self.stats["total_requests"] += 1
            self.stats["failed_requests"] += 1
            self.stats["last_failure"] = datetime.now(timezone.utc).isoformat()
            
            if self.config["log_failures"]:
                await self.log_request(False, None, str(e))
            
            await self.save_stats()

    async def log_request(self, success, status_code, error_msg=None):
        """Log request result to configured channel."""
        if self.config["log_channel_id"] is None:
            return
        
        log_channel = self.bot.get_channel(self.config["log_channel_id"])
        if log_channel is None:
            return
        
        if success:
            embed = discord.Embed(
                title="✅ HTTP Ping Success",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="URL", value=self.config["url"], inline=False)
            embed.add_field(name="Method", value=self.config["method"], inline=True)
            embed.add_field(name="Status Code", value=str(status_code), inline=True)
        else:
            embed = discord.Embed(
                title="❌ HTTP Ping Failed",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="URL", value=self.config["url"], inline=False)
            embed.add_field(name="Method", value=self.config["method"], inline=True)
            if status_code:
                embed.add_field(name="Status Code", value=str(status_code), inline=True)
            if error_msg:
                embed.add_field(name="Error", value=error_msg, inline=False)
        
        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"Error logging HTTP ping: {e}")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.group(invoke_without_command=True)
    async def httpping(self, ctx):
        """Configure the HTTP ping plugin."""
        await ctx.send_help(ctx.command)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @httpping.command(name="seturl")
    async def httpping_seturl(self, ctx, url: str):
        """Set the URL to ping."""
        if not url.startswith(("http://", "https://")):
            await ctx.send("❌ URL must start with http:// or https://")
            return
        
        self.config["url"] = url
        await self.update_config()
        await ctx.send(f"✅ URL set to: `{url}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @httpping.command(name="setmethod")
    async def httpping_setmethod(self, ctx, method: str):
        """Set the HTTP method (HEAD, GET, or POST)."""
        method = method.upper()
        if method not in ["HEAD", "GET", "POST"]:
            await ctx.send("❌ Method must be HEAD, GET, or POST")
            return
        
        self.config["method"] = method
        await self.update_config()
        await ctx.send(f"✅ HTTP method set to: **{method}**")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @httpping.command(name="setinterval")
    async def httpping_setinterval(self, ctx, seconds: int):
        """Set the interval between pings (minimum 10 seconds)."""
        if seconds < 10:
            await ctx.send("❌ Interval must be at least 10 seconds")
            return
        
        self.config["interval"] = seconds
        await self.update_config()
        
        # Restart task with new interval if running
        if self.ping_task.is_running():
            self.ping_task.cancel()
            self.ping_task.change_interval(seconds=seconds)
            self.ping_task.start()
        
        await ctx.send(f"✅ Ping interval set to: **{seconds} seconds**")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @httpping.command(name="setchannel")
    async def httpping_setchannel(self, ctx, channel: discord.TextChannel):
        """Set the channel where ping logs will be sent."""
        self.config["log_channel_id"] = channel.id
        await self.update_config()
        await ctx.send(f"✅ Ping logs will be sent to {channel.mention}")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @httpping.command(name="toggle")
    async def httpping_toggle(self, ctx):
        """Enable or disable HTTP pinging."""
        if not self.config["url"]:
            await ctx.send("❌ Please set a URL first using `httpping seturl <url>`")
            return
        
        self.config["enabled"] = not self.config["enabled"]
        await self.update_config()
        
        if self.config["enabled"]:
            if not self.ping_task.is_running():
                self.ping_task.change_interval(seconds=self.config["interval"])
                self.ping_task.start()
            await ctx.send("✅ HTTP pinging is now **enabled**.")
        else:
            if self.ping_task.is_running():
                self.ping_task.cancel()
            await ctx.send("✅ HTTP pinging is now **disabled**.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @httpping.command(name="togglefailures")
    async def httpping_togglefailures(self, ctx):
        """Toggle logging of failed requests."""
        self.config["log_failures"] = not self.config["log_failures"]
        await self.update_config()
        
        status = "enabled" if self.config["log_failures"] else "disabled"
        await ctx.send(f"✅ Failure logging is now **{status}**.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @httpping.command(name="togglesuccesses")
    async def httpping_togglesuccesses(self, ctx):
        """Toggle logging of successful requests."""
        self.config["log_successes"] = not self.config["log_successes"]
        await self.update_config()
        
        status = "enabled" if self.config["log_successes"] else "disabled"
        await ctx.send(f"✅ Success logging is now **{status}**.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @httpping.command(name="stats")
    async def httpping_stats(self, ctx):
        """View HTTP ping statistics."""
        embed = discord.Embed(
            title="📊 HTTP Ping Statistics",
            color=self.bot.main_color,
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(
            name="Total Requests",
            value=str(self.stats["total_requests"]),
            inline=True
        )
        embed.add_field(
            name="Successful",
            value=f"✅ {self.stats['successful_requests']}",
            inline=True
        )
        embed.add_field(
            name="Failed",
            value=f"❌ {self.stats['failed_requests']}",
            inline=True
        )
        
        if self.stats["last_status_code"]:
            embed.add_field(
                name="Last Status Code",
                value=str(self.stats["last_status_code"]),
                inline=True
            )
        
        if self.stats["last_success"]:
            last_success = datetime.fromisoformat(self.stats["last_success"])
            embed.add_field(
                name="Last Success",
                value=f"<t:{int(last_success.timestamp())}:R>",
                inline=True
            )
        
        if self.stats["last_failure"]:
            last_failure = datetime.fromisoformat(self.stats["last_failure"])
            embed.add_field(
                name="Last Failure",
                value=f"<t:{int(last_failure.timestamp())}:R>",
                inline=True
            )
        
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @httpping.command(name="resetstats")
    async def httpping_resetstats(self, ctx):
        """Reset HTTP ping statistics."""
        self.stats = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "last_success": None,
            "last_failure": None,
            "last_status_code": None
        }
        await self.save_stats()
        await ctx.send("✅ HTTP ping statistics have been reset.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @httpping.command(name="config")
    async def httpping_config(self, ctx):
        """View current HTTP ping configuration."""
        log_channel = self.bot.get_channel(self.config["log_channel_id"]) if self.config["log_channel_id"] else None
        
        embed = discord.Embed(
            title="HTTP Ping Configuration",
            color=self.bot.main_color
        )
        
        embed.add_field(
            name="URL",
            value=self.config["url"] or "Not set",
            inline=False
        )
        
        embed.add_field(
            name="Method",
            value=self.config["method"],
            inline=True
        )
        
        embed.add_field(
            name="Interval",
            value=f"{self.config['interval']} seconds",
            inline=True
        )
        
        embed.add_field(
            name="Timeout",
            value=f"{self.config['timeout']} seconds",
            inline=True
        )
        
        embed.add_field(
            name="Enabled",
            value="✅ Yes" if self.config["enabled"] else "❌ No",
            inline=True
        )
        
        embed.add_field(
            name="Log Failures",
            value="✅ Yes" if self.config["log_failures"] else "❌ No",
            inline=True
        )
        
        embed.add_field(
            name="Log Successes",
            value="✅ Yes" if self.config["log_successes"] else "❌ No",
            inline=True
        )
        
        embed.add_field(
            name="Log Channel",
            value=log_channel.mention if log_channel else "Not set",
            inline=False
        )
        
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @httpping.command(name="test")
    async def httpping_test(self, ctx):
        """Test the HTTP ping immediately."""
        if not self.config["url"]:
            await ctx.send("❌ Please set a URL first using `httpping seturl <url>`")
            return
        
        await ctx.send("🔄 Testing HTTP ping...")
        
        # Manually trigger one ping
        await self.ping_task()
        
        if self.stats["last_status_code"]:
            if self.stats["last_status_code"] < 400:
                await ctx.send(f"✅ Test successful! Status code: {self.stats['last_status_code']}")
            else:
                await ctx.send(f"❌ Test failed with status code: {self.stats['last_status_code']}")
        else:
            await ctx.send("❌ Test failed - check logs for details")


async def setup(bot):
    await bot.add_cog(HTTPPing(bot))