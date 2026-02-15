import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel


def is_discord_server_link(url: str) -> bool:
    if not url:
        return False

    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return False

    host = parsed.netloc.lower()
    path = parsed.path.strip("/")

    if host in {"discord.gg", "www.discord.gg"} and len(path) > 0:
        return True

    if host in {"discord.com", "www.discord.com", "ptb.discord.com", "canary.discord.com"}:
        return bool(re.fullmatch(r"invite/[A-Za-z0-9\-_.~]+", path))

    return False


class HiringSubmissionModal(discord.ui.Modal, title="Hiring Submission"):
    def __init__(self, cog: "Hiring"):
        super().__init__()
        self.cog = cog

        self.position = discord.ui.TextInput(
            label="Position",
            placeholder="Example: Moderator",
            max_length=100,
            required=True,
        )
        self.description = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            placeholder="Describe the role and what you're looking for.",
            max_length=1000,
            required=True,
        )
        self.server_link = discord.ui.TextInput(
            label="Discord Server Link",
            placeholder="https://discord.gg/yourinvite",
            max_length=200,
            required=True,
        )

        self.add_item(self.position)
        self.add_item(self.description)
        self.add_item(self.server_link)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True,
            )

        link_value = str(self.server_link.value).strip()
        if not is_discord_server_link(link_value):
            return await interaction.response.send_message(
                "❌ Please provide a valid Discord server invite link (discord.gg or discord.com/invite).",
                ephemeral=True,
            )

        output_channel_id = self.cog.config.get("output_channel_id")
        output_channel = guild.get_channel(output_channel_id) if output_channel_id else None
        if output_channel is None:
            return await interaction.response.send_message(
                "❌ Hiring output channel is not configured or not found.",
                ephemeral=True,
            )

        if not self.cog.supabase_ready():
            return await interaction.response.send_message(
                "❌ Supabase is not configured. Ask an administrator to set it up.",
                ephemeral=True,
            )

        payload = {
            "guild_id": str(guild.id),
            "guild_name": guild.name,
            "user_id": str(interaction.user.id),
            "username": str(interaction.user),
            "position": str(self.position.value).strip(),
            "description": str(self.description.value).strip(),
            "discord_server_link": link_value,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }

        ok, err = await self.cog.save_to_supabase(payload)
        if not ok:
            return await interaction.response.send_message(
                f"❌ Could not save to Supabase: {err}",
                ephemeral=True,
            )

        embed = discord.Embed(
            title="📋 New Hiring Submission",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Submitted By", value=f"{interaction.user.mention} ({interaction.user})", inline=False)
        embed.add_field(name="Position", value=payload["position"], inline=False)
        embed.add_field(name="Description", value=payload["description"], inline=False)
        embed.add_field(name="Discord Server Link", value=payload["discord_server_link"], inline=False)

        try:
            await output_channel.send(embed=embed)
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I don't have permission to send messages in the configured output channel.",
                ephemeral=True,
            )
        except Exception as exc:
            return await interaction.response.send_message(
                f"❌ Saved to Supabase, but failed to post embed: {exc}",
                ephemeral=True,
            )

        await interaction.response.send_message("✅ Hiring submission sent successfully.", ephemeral=True)


class HiringPanelView(discord.ui.View):
    def __init__(self, cog: "Hiring"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Apply for Hiring",
        style=discord.ButtonStyle.primary,
        custom_id="hiring:open_form",
    )
    async def open_hiring_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ This button can only be used in a server.",
                ephemeral=True,
            )

        await interaction.response.send_modal(HiringSubmissionModal(self.cog))


class Hiring(commands.Cog):
    """Collect hiring submissions via a button panel and save to Supabase."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        self.default_config = {
            "panel_channel_id": None,
            "panel_message": "Click the button below to submit a hiring post.",
            "output_channel_id": None,
            "supabase_url": None,
            "supabase_key": None,
            "supabase_table": "hiring_submissions",
        }
        self.config = None

    async def cog_load(self):
        self.config = await self.db.find_one({"_id": "hiring-config"})
        if self.config is None:
            self.config = self.default_config.copy()
            await self.update_config()

        missing = [k for k in self.default_config if k not in self.config]
        if missing:
            for key in missing:
                self.config[key] = self.default_config[key]
            await self.update_config()

        self.bot.add_view(HiringPanelView(self))

    async def update_config(self):
        await self.db.find_one_and_update(
            {"_id": "hiring-config"},
            {"$set": self.config},
            upsert=True,
        )

    def supabase_ready(self) -> bool:
        return bool(
            self.config.get("supabase_url")
            and self.config.get("supabase_key")
            and self.config.get("supabase_table")
        )

    async def save_to_supabase(self, payload: dict):
        url = self.config["supabase_url"].rstrip("/")
        table = self.config["supabase_table"]
        key = self.config["supabase_key"]

        endpoint = f"{url}/rest/v1/{table}"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, json=payload, headers=headers) as response:
                    if response.status in (200, 201):
                        return True, None
                    body = await response.text()
                    return False, f"HTTP {response.status}: {body[:300]}"
        except Exception as exc:
            return False, str(exc)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.group(invoke_without_command=True)
    async def hiringconfig(self, ctx):
        """Configure the hiring plugin."""
        await ctx.send_help(ctx.command)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @hiringconfig.command(name="setpanelchannel")
    async def hiringconfig_setpanelchannel(self, ctx, channel: discord.TextChannel):
        """Set the channel where the hiring button panel should be posted."""
        self.config["panel_channel_id"] = channel.id
        await self.update_config()
        await ctx.send(f"✅ Hiring panel channel set to {channel.mention}")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @hiringconfig.command(name="setpanelmessage")
    async def hiringconfig_setpanelmessage(self, ctx, *, message: str):
        """Set the message content used for the hiring panel."""
        self.config["panel_message"] = message
        await self.update_config()
        await ctx.send("✅ Hiring panel message updated.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @hiringconfig.command(name="sendpanel")
    async def hiringconfig_sendpanel(self, ctx):
        """Send the hiring panel message with button to the configured channel."""
        if ctx.guild is None:
            return await ctx.send("❌ This command can only be used in a server.")

        panel_channel_id = self.config.get("panel_channel_id")
        if not panel_channel_id:
            return await ctx.send("❌ Panel channel not set. Use `hiringconfig setpanelchannel <#channel>`." )

        panel_channel = ctx.guild.get_channel(panel_channel_id)
        if panel_channel is None:
            return await ctx.send("❌ The configured panel channel no longer exists.")

        panel_message = self.config.get("panel_message") or "Click the button below to submit a hiring post."
        view = HiringPanelView(self)

        try:
            await panel_channel.send(panel_message, view=view)
        except discord.Forbidden:
            return await ctx.send("❌ I don't have permission to send messages in the configured panel channel.")
        except Exception as exc:
            return await ctx.send(f"❌ Failed to send hiring panel: {exc}")

        await ctx.send(f"✅ Hiring panel sent to {panel_channel.mention}")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @hiringconfig.command(name="setoutputchannel")
    async def hiringconfig_setoutputchannel(self, ctx, channel: discord.TextChannel):
        """Set the channel where hiring embeds are posted."""
        self.config["output_channel_id"] = channel.id
        await self.update_config()
        await ctx.send(f"✅ Hiring submissions will be posted in {channel.mention}")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @hiringconfig.command(name="setsupabase")
    async def hiringconfig_setsupabase(self, ctx, url: str, key: str, table: str = "hiring_submissions"):
        """Set Supabase URL, API key, and optional table name."""
        if not url.startswith(("https://", "http://")):
            return await ctx.send("❌ Supabase URL must start with http:// or https://")

        self.config["supabase_url"] = url.rstrip("/")
        self.config["supabase_key"] = key
        self.config["supabase_table"] = table.strip()
        await self.update_config()

        await ctx.send("✅ Supabase configuration updated.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @hiringconfig.command(name="settable")
    async def hiringconfig_settable(self, ctx, table: str):
        """Set Supabase table name used for submissions."""
        self.config["supabase_table"] = table.strip()
        await self.update_config()
        await ctx.send(f"✅ Supabase table set to `{table.strip()}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @hiringconfig.command(name="view")
    async def hiringconfig_view(self, ctx):
        """View current hiring configuration."""
        panel_channel = self.bot.get_channel(self.config["panel_channel_id"]) if self.config.get("panel_channel_id") else None
        output_channel = self.bot.get_channel(self.config["output_channel_id"]) if self.config.get("output_channel_id") else None
        key = self.config.get("supabase_key")
        masked_key = "Configured" if key else "Not set"

        embed = discord.Embed(
            title="Hiring Plugin Configuration",
            color=self.bot.main_color,
        )
        embed.add_field(
            name="Panel Channel",
            value=panel_channel.mention if panel_channel else "Not set",
            inline=False,
        )
        embed.add_field(
            name="Panel Message",
            value=(self.config.get("panel_message") or "Not set")[:1024],
            inline=False,
        )
        embed.add_field(
            name="Output Channel",
            value=output_channel.mention if output_channel else "Not set",
            inline=False,
        )
        embed.add_field(
            name="Supabase URL",
            value=self.config.get("supabase_url") or "Not set",
            inline=False,
        )
        embed.add_field(
            name="Supabase Key",
            value=masked_key,
            inline=True,
        )
        embed.add_field(
            name="Supabase Table",
            value=self.config.get("supabase_table") or "Not set",
            inline=True,
        )

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Hiring(bot))
