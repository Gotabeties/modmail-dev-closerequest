import re
from datetime import datetime, timezone
from typing import Dict, List, Optional
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
    def __init__(
        self,
        cog: "Hiring",
        mode: str = "create",
        request_id: Optional[int] = None,
        initial_data: Optional[Dict] = None,
    ):
        super().__init__()
        self.cog = cog
        self.mode = mode
        self.request_id = request_id
        initial_data = initial_data or {}

        self.company_name = discord.ui.TextInput(
            label="Company Name",
            placeholder="Example: Ameritian",
            max_length=100,
            required=True,
            default=str(initial_data.get("company_name", ""))[:100],
        )
        self.position = discord.ui.TextInput(
            label="Position",
            placeholder="Example: Moderator",
            max_length=100,
            required=True,
            default=str(initial_data.get("position", ""))[:100],
        )
        self.description = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            placeholder="Describe the role and what you're looking for.",
            max_length=1000,
            required=True,
            default=str(initial_data.get("description", ""))[:1000],
        )
        self.server_link = discord.ui.TextInput(
            label="Discord Server Link",
            placeholder="https://discord.gg/yourinvite",
            max_length=200,
            required=True,
            default=str(initial_data.get("discord_server_link", ""))[:200],
        )

        self.add_item(self.company_name)
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

        if not self.cog.supabase_ready():
            return await interaction.response.send_message(
                "❌ Supabase is not configured. Ask an administrator to set it up.",
                ephemeral=True,
            )

        output_channel = self.cog._get_output_channel(guild)
        if output_channel is None:
            return await interaction.response.send_message(
                "❌ Hiring output channel is not configured or not found.",
                ephemeral=True,
            )

        link_value = str(self.server_link.value).strip()
        if not is_discord_server_link(link_value):
            return await interaction.response.send_message(
                "❌ Please provide a valid Discord server invite link (discord.gg or discord.com/invite).",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        base_payload = {
            "company_name": str(self.company_name.value).strip(),
            "position": str(self.position.value).strip(),
            "description": str(self.description.value).strip(),
            "discord_server_link": link_value,
        }

        if self.mode == "create":
            request_count = await self.cog.get_open_request_count(str(guild.id), str(interaction.user.id))
            if request_count >= self.cog.max_open_requests:
                return await interaction.followup.send(
                    f"❌ You can only have {self.cog.max_open_requests} open hiring requests at a time. Delete one first.",
                    ephemeral=True,
                )

            payload = {
                "guild_id": str(guild.id),
                "guild_name": guild.name,
                "user_id": str(interaction.user.id),
                "username": str(interaction.user),
                **base_payload,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }

            ok, request_id = await self.cog.create_request(payload)
            if not ok:
                return await interaction.followup.send(
                    f"❌ Could not save hiring request: {request_id}",
                    ephemeral=True,
                )

            ok, result = await self.cog.post_or_repost_hiring_request(
                guild=guild,
                user=interaction.user,
                request_id=request_id,
                request_data=base_payload,
            )
            if not ok:
                return await interaction.followup.send(
                    f"❌ Saved request, but failed to post embed: {result}",
                    ephemeral=True,
                )

            success_message = "✅ Hiring request created."
        else:
            if self.request_id is None:
                return await interaction.followup.send(
                    "❌ Missing hiring request id for edit.",
                    ephemeral=True,
                )

            ok, result = await self.cog.update_request(
                request_id=self.request_id,
                guild_id=str(guild.id),
                user_id=str(interaction.user.id),
                payload=base_payload,
            )
            if not ok:
                return await interaction.followup.send(
                    f"❌ Could not update hiring request: {result}",
                    ephemeral=True,
                )

            ok, result = await self.cog.post_or_repost_hiring_request(
                guild=guild,
                user=interaction.user,
                request_id=self.request_id,
                request_data=base_payload,
            )
            if not ok:
                return await interaction.followup.send(
                    f"❌ Updated request, but failed to post embed: {result}",
                    ephemeral=True,
                )

            success_message = "✅ Hiring request updated."

        await interaction.followup.send(success_message, ephemeral=True)


class HiringEditRequestSelect(discord.ui.Select):
    def __init__(self, cog: "Hiring", requests: List[Dict]):
        self.cog = cog
        self.requests_map = {str(item["id"]): item for item in requests}

        options = []
        for item in requests[:25]:
            req_id = str(item["id"])
            company = str(item.get("company_name") or "Unknown Company")
            position = str(item.get("position") or "Unknown Position")
            label = f"{company} - {position}"[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    value=req_id,
                    description=f"Request ID: {req_id}",
                )
            )

        super().__init__(placeholder="Select a request to edit", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_id = self.values[0]
        request = self.requests_map.get(selected_id)
        if request is None:
            return await interaction.response.send_message("❌ Request not found.", ephemeral=True)

        await interaction.response.send_modal(
            HiringSubmissionModal(
                cog=self.cog,
                mode="edit",
                request_id=int(selected_id),
                initial_data=request,
            )
        )


class HiringDeleteRequestSelect(discord.ui.Select):
    def __init__(self, cog: "Hiring", requests: List[Dict]):
        self.cog = cog

        options = []
        for item in requests[:25]:
            req_id = str(item["id"])
            company = str(item.get("company_name") or "Unknown Company")
            position = str(item.get("position") or "Unknown Position")
            label = f"{company} - {position}"[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    value=req_id,
                    description=f"Request ID: {req_id}",
                )
            )

        super().__init__(placeholder="Select a request to delete", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        selected_id = self.values[0]

        guild = interaction.guild
        if guild is None:
            return await interaction.followup.send("❌ This can only be used in a server.", ephemeral=True)

        ok, result = await self.cog.delete_request(
            request_id=int(selected_id),
            guild_id=str(guild.id),
            user_id=str(interaction.user.id),
        )
        if not ok:
            return await interaction.followup.send(f"❌ Could not delete request: {result}", ephemeral=True)

        await self.cog.remove_request_message(request_id=int(selected_id), guild=guild)
        await interaction.followup.send("✅ Hiring request deleted.", ephemeral=True)


class HiringRequestSelectView(discord.ui.View):
    def __init__(self, cog: "Hiring", action: str, requests: List[Dict]):
        super().__init__(timeout=600)
        if action == "edit":
            self.add_item(HiringEditRequestSelect(cog=cog, requests=requests))
        else:
            self.add_item(HiringDeleteRequestSelect(cog=cog, requests=requests))


class HiringRequestMenuView(discord.ui.View):
    def __init__(self, cog: "Hiring"):
        super().__init__(timeout=600)
        self.cog = cog

    @discord.ui.button(label="Add New Request", style=discord.ButtonStyle.primary)
    async def add_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(HiringSubmissionModal(self.cog, mode="create"))

    @discord.ui.button(label="Edit Current Request", style=discord.ButtonStyle.secondary)
    async def edit_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return await interaction.followup.send("❌ This can only be used in a server.", ephemeral=True)

        ok, result = await self.cog.list_open_requests(str(guild.id), str(interaction.user.id))
        if not ok:
            return await interaction.followup.send(f"❌ Could not load requests: {result}", ephemeral=True)

        if not result:
            return await interaction.followup.send("ℹ️ You have no open hiring requests to edit.", ephemeral=True)

        embed = discord.Embed(
            title="Edit Request",
            description="Select one of your active requests to edit.",
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(
            embed=embed,
            view=HiringRequestSelectView(self.cog, action="edit", requests=result),
            ephemeral=True,
        )

    @discord.ui.button(label="Delete Old Request", style=discord.ButtonStyle.danger)
    async def delete_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return await interaction.followup.send("❌ This can only be used in a server.", ephemeral=True)

        ok, result = await self.cog.list_open_requests(str(guild.id), str(interaction.user.id))
        if not ok:
            return await interaction.followup.send(f"❌ Could not load requests: {result}", ephemeral=True)

        if not result:
            return await interaction.followup.send("ℹ️ You have no open hiring requests to delete.", ephemeral=True)

        embed = discord.Embed(
            title="Delete Request",
            description="Select one of your active requests to delete.",
            color=discord.Color.red(),
        )
        await interaction.followup.send(
            embed=embed,
            view=HiringRequestSelectView(self.cog, action="delete", requests=result),
            ephemeral=True,
        )


class HiringPanelView(discord.ui.View):
    def __init__(self, cog: "Hiring"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Hiring Request Menu",
        style=discord.ButtonStyle.primary,
        custom_id="hiring:open_form",
    )
    async def open_hiring_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ This button can only be used in a server.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        count = await self.cog.get_open_request_count(str(interaction.guild.id), str(interaction.user.id))
        menu_embed = discord.Embed(
            title=f"Hiring Request Menu ({count}/{self.cog.max_open_requests})",
            description="Use the buttons below to add, edit, or delete your hiring requests.",
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(
            embed=menu_embed,
            view=HiringRequestMenuView(self.cog),
            ephemeral=True,
        )


class Hiring(commands.Cog):
    """Collect hiring submissions via a button panel and save to Supabase."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        self.max_open_requests = 3
        self.default_config = {
            "panel_channel_id": None,
            "panel_message": "Click the button below to submit a hiring post.",
            "panel_message_id": None,
            "output_channel_id": None,
            "use_panel_channel_for_output": False,
            "supabase_url": None,
            "supabase_key": None,
            "supabase_table": "hiring_submissions",
        }
        self.config = None
        self.request_message_map = {}

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

        message_map_doc = await self.db.find_one({"_id": "hiring-request-message-map"})
        self.request_message_map = (message_map_doc or {}).get("map", {})

        self.bot.add_view(HiringPanelView(self))

    async def update_config(self):
        await self.db.find_one_and_update(
            {"_id": "hiring-config"},
            {"$set": self.config},
            upsert=True,
        )

    async def update_request_message_map(self):
        await self.db.find_one_and_update(
            {"_id": "hiring-request-message-map"},
            {"$set": {"map": self.request_message_map}},
            upsert=True,
        )

    async def _send_or_resend_panel_message(self, guild: discord.Guild):
        panel_channel_id = self.config.get("panel_channel_id")
        if not panel_channel_id:
            return False, "Panel channel not set. Use `hiringconfig setpanelchannel <#channel>`."

        panel_channel = guild.get_channel(panel_channel_id)
        if panel_channel is None:
            return False, "The configured panel channel no longer exists."

        old_message_id = self.config.get("panel_message_id")
        if old_message_id:
            try:
                old_message = await panel_channel.fetch_message(old_message_id)
                await old_message.delete()
            except Exception:
                pass

        panel_message = self.config.get("panel_message") or "Click the button below to submit a hiring post."
        embed = discord.Embed(
            title="Hiring Request Menu",
            description=panel_message,
            color=self.bot.main_color,
        )
        view = HiringPanelView(self)

        try:
            sent_message = await panel_channel.send(embed=embed, view=view)
        except discord.Forbidden:
            return False, "I don't have permission to send messages in the configured panel channel."
        except Exception as exc:
            return False, str(exc)

        self.config["panel_message_id"] = sent_message.id
        await self.update_config()
        return True, None

    def supabase_ready(self) -> bool:
        return bool(
            self.config.get("supabase_url")
            and self.config.get("supabase_key")
            and self.config.get("supabase_table")
        )

    def _get_output_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        if self.config.get("use_panel_channel_for_output"):
            panel_channel_id = self.config.get("panel_channel_id")
            panel_channel = guild.get_channel(panel_channel_id) if panel_channel_id else None
            if isinstance(panel_channel, discord.TextChannel):
                return panel_channel

        output_channel_id = self.config.get("output_channel_id")
        output_channel = guild.get_channel(output_channel_id) if output_channel_id else None
        if isinstance(output_channel, discord.TextChannel):
            return output_channel

        return None

    def _build_hiring_embed(self, user: discord.abc.User, request_data: Dict) -> discord.Embed:
        embed = discord.Embed(
            title="Now Hiring",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Submitted By", value=f"{user.mention} ({user})", inline=False)
        embed.add_field(name="Company Name", value=request_data["company_name"], inline=False)
        embed.add_field(name="Position", value=request_data["position"], inline=False)
        embed.add_field(name="Description", value=request_data["description"], inline=False)
        embed.add_field(name="Discord Server Link", value=request_data["discord_server_link"], inline=False)
        return embed

    async def remove_request_message(self, request_id: int, guild: discord.Guild):
        mapped = self.request_message_map.get(str(request_id))
        if not mapped:
            return

        channel = guild.get_channel(mapped.get("channel_id"))
        if channel is not None:
            try:
                message = await channel.fetch_message(mapped.get("message_id"))
                await message.delete()
            except Exception:
                pass

        self.request_message_map.pop(str(request_id), None)
        await self.update_request_message_map()

    async def post_or_repost_hiring_request(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        request_id: Optional[int],
        request_data: Dict,
    ):
        channel = self._get_output_channel(guild)
        if channel is None:
            return False, "Hiring output channel is not configured or not found."

        if request_id is not None:
            await self.remove_request_message(request_id=request_id, guild=guild)

        embed = self._build_hiring_embed(user=user, request_data=request_data)
        try:
            message = await channel.send(embed=embed)
        except discord.Forbidden:
            return False, "I don't have permission to send messages in the configured output channel."
        except Exception as exc:
            return False, str(exc)

        if request_id is not None:
            self.request_message_map[str(request_id)] = {
                "channel_id": channel.id,
                "message_id": message.id,
            }
            await self.update_request_message_map()

        if self.config.get("use_panel_channel_for_output"):
            await self._send_or_resend_panel_message(guild)

        return True, None

    def _supabase_endpoint(self) -> str:
        url = self.config["supabase_url"].rstrip("/")
        table = self.config["supabase_table"]
        return f"{url}/rest/v1/{table}"

    def _supabase_headers(self, prefer: Optional[str] = None) -> dict:
        key = self.config["supabase_key"]
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    async def create_request(self, payload: dict):
        endpoint = self._supabase_endpoint()
        headers = self._supabase_headers(prefer="return=representation")

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, json=payload, headers=headers) as response:
                    if response.status in (200, 201):
                        data = await response.json(content_type=None)
                        if isinstance(data, list) and data:
                            return True, data[0].get("id")
                        return True, None
                    body = await response.text()
                    if response.status == 409 and "23505" in body and "guild_id" in body:
                        return (
                            False,
                            "Table schema issue: `guild_id` is unique/primary. Create an `id` primary key and make `guild_id` a normal text column.",
                        )
                    return False, f"HTTP {response.status}: {body[:300]}"
        except Exception as exc:
            return False, str(exc)

    async def list_open_requests(self, guild_id: str, user_id: str):
        endpoint = self._supabase_endpoint()
        headers = self._supabase_headers()
        params = {
            "select": "id,company_name,position,description,discord_server_link,submitted_at",
            "guild_id": f"eq.{guild_id}",
            "user_id": f"eq.{user_id}",
            "order": "submitted_at.desc",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(endpoint, params=params, headers=headers) as response:
                    if response.status in (200, 206):
                        data = await response.json(content_type=None)
                        if isinstance(data, list):
                            return True, data
                        return True, []
                    body = await response.text()
                    return False, f"HTTP {response.status}: {body[:300]}"
        except Exception as exc:
            return False, str(exc)

    async def get_open_request_count(self, guild_id: str, user_id: str) -> int:
        ok, result = await self.list_open_requests(guild_id=guild_id, user_id=user_id)
        if not ok:
            return self.max_open_requests
        return len(result)

    async def update_request(self, request_id: int, guild_id: str, user_id: str, payload: dict):
        endpoint = self._supabase_endpoint()
        headers = self._supabase_headers(prefer="return=minimal")
        params = {
            "id": f"eq.{request_id}",
            "guild_id": f"eq.{guild_id}",
            "user_id": f"eq.{user_id}",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.patch(endpoint, params=params, json=payload, headers=headers) as response:
                    if response.status in (200, 204):
                        return True, None
                    body = await response.text()
                    return False, f"HTTP {response.status}: {body[:300]}"
        except Exception as exc:
            return False, str(exc)

    async def delete_request(self, request_id: int, guild_id: str, user_id: str):
        endpoint = self._supabase_endpoint()
        headers = self._supabase_headers(prefer="return=minimal")
        params = {
            "id": f"eq.{request_id}",
            "guild_id": f"eq.{guild_id}",
            "user_id": f"eq.{user_id}",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.delete(endpoint, params=params, headers=headers) as response:
                    if response.status in (200, 204):
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

        ok, result = await self._send_or_resend_panel_message(ctx.guild)
        if not ok:
            return await ctx.send(f"❌ Failed to send hiring panel: {result}")

        panel_channel = ctx.guild.get_channel(self.config.get("panel_channel_id"))
        if panel_channel is None:
            return await ctx.send("✅ Hiring panel sent.")

        await ctx.send(f"✅ Hiring panel sent to {panel_channel.mention}")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @hiringconfig.command(name="setoutputchannel")
    async def hiringconfig_setoutputchannel(self, ctx, channel: discord.TextChannel):
        """Set the channel where hiring embeds are posted."""
        self.config["output_channel_id"] = channel.id
        await self.update_config()
        await ctx.send(f"✅ Hiring submissions will be posted in {channel.mention}")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @hiringconfig.command(name="usepaneloutput")
    async def hiringconfig_usepaneloutput(self, ctx, enabled: bool):
        """Toggle posting hiring embeds to panel channel instead of output channel."""
        self.config["use_panel_channel_for_output"] = enabled
        await self.update_config()
        if enabled:
            await ctx.send("✅ Hiring embeds will post in the panel channel and repost to stay at the bottom.")
        else:
            await ctx.send("✅ Hiring embeds will use the configured output channel.")

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
            name="Use Panel as Output",
            value="Enabled" if self.config.get("use_panel_channel_for_output") else "Disabled",
            inline=True,
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
