import re
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import commands, tasks

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
        request_id: Optional[str] = None,
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

        moderation_result = await self.cog.validate_hiring_content(base_payload)
        if moderation_result:
            if moderation_result.get("type") == "api_error":
                return await interaction.followup.send(
                    "❌ Content filter is temporarily unavailable. Please try again in a moment.",
                    ephemeral=True,
                )

            field_name = moderation_result["field"]
            return await interaction.followup.send(
                f"❌ Your submission was blocked by the content filter. Please remove inappropriate language from **{field_name}** and try again.",
                ephemeral=True,
            )

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

            request_id = str(request_id) if request_id is not None else None

            ok, result = await self.cog.post_or_repost_hiring_request(
                guild=guild,
                user=interaction.user,
                request_id=request_id,
                request_data=payload,
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

            ok_existing, existing = await self.cog.get_request_by_id(
                request_id=str(self.request_id),
                guild_id=str(guild.id),
            )
            request_data = {
                "guild_id": str(guild.id),
                "guild_name": guild.name,
                "user_id": str(interaction.user.id),
                "username": str(interaction.user),
                **base_payload,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }
            if ok_existing and isinstance(existing, dict):
                request_data["submitted_at"] = str(existing.get("submitted_at") or request_data["submitted_at"])
                request_data["username"] = str(existing.get("username") or request_data["username"])

            ok, result = await self.cog.post_or_repost_hiring_request(
                guild=guild,
                user=interaction.user,
                request_id=self.request_id,
                request_data=request_data,
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
        try:
            selected_id = self.values[0]
            request = self.requests_map.get(selected_id)
            if request is None:
                return await interaction.response.send_message("❌ Request not found.", ephemeral=True)

            await interaction.response.send_modal(
                HiringSubmissionModal(
                    cog=self.cog,
                    mode="edit",
                    request_id=selected_id,
                    initial_data=request,
                )
            )
        except Exception as exc:
            await self.cog.send_interaction_debug(
                interaction=interaction,
                context="Edit request selection failed.",
                exc=exc,
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
        try:
            await interaction.response.defer(ephemeral=True)
            selected_id = self.values[0]

            guild = interaction.guild
            if guild is None:
                return await interaction.followup.send("❌ This can only be used in a server.", ephemeral=True)

            ok, result = await self.cog.delete_request(
                request_id=selected_id,
                guild_id=str(guild.id),
                user_id=str(interaction.user.id),
            )
            if not ok:
                return await interaction.followup.send(f"❌ Could not delete request: {result}", ephemeral=True)

            await self.cog.remove_request_message(request_id=selected_id, guild=guild)
            await interaction.followup.send("✅ Hiring request deleted.", ephemeral=True)
        except Exception as exc:
            await self.cog.send_interaction_debug(
                interaction=interaction,
                context="Delete request selection failed.",
                exc=exc,
            )


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
        try:
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
        except Exception as exc:
            await self.cog.send_interaction_debug(
                interaction=interaction,
                context="Edit menu action failed.",
                exc=exc,
            )

    @discord.ui.button(label="Delete Old Request", style=discord.ButtonStyle.danger)
    async def delete_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
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
        except Exception as exc:
            await self.cog.send_interaction_debug(
                interaction=interaction,
                context="Delete menu action failed.",
                exc=exc,
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
        menu_title = (self.cog.config.get("panel_embed_title") or "Hiring Request Menu")[:220]
        menu_embed = discord.Embed(
            title=f"{menu_title} ({count}/{self.cog.max_open_requests})",
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
            "panel_embed_title": "Hiring Request Menu",
            "output_channel_id": None,
            "use_panel_channel_for_output": False,
            "content_filter_enabled": True,
            "profanity_api_url": "https://vector.profanity.dev",
            "auto_delete_enabled": True,
            "auto_delete_days": 14,
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
        if not self.auto_delete_loop.is_running():
            self.auto_delete_loop.start()

    def cog_unload(self):
        if self.auto_delete_loop.is_running():
            self.auto_delete_loop.cancel()

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

    async def send_interaction_debug(self, interaction: discord.Interaction, context: str, exc: Exception):
        detail = f"{type(exc).__name__}: {exc}"
        trace = traceback.format_exc()
        if trace and trace != "NoneType: None\n":
            detail = f"{detail}\n{trace}"

        detail = detail[:1400]
        message = f"❌ {context}\n```\n{detail}\n```"

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            pass

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
            title=(self.config.get("panel_embed_title") or "Hiring Request Menu")[:256],
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

    async def _check_message_with_profanity_api(self, message: str) -> Tuple[bool, Optional[bool], Optional[str]]:
        api_url = str(self.config.get("profanity_api_url") or "https://vector.profanity.dev").strip()
        if not api_url:
            return False, None, "Profanity API URL is not configured."

        headers = {"Content-Type": "application/json"}
        body = {"message": message}

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(api_url, json=body, headers=headers) as response:
                    if response.status != 200:
                        body_text = await response.text()
                        return False, None, f"HTTP {response.status}: {body_text[:200]}"

                    try:
                        data: Any = await response.json(content_type=None)
                    except Exception:
                        raw = await response.text()
                        return False, None, f"Invalid JSON response: {raw[:200]}"
        except Exception as exc:
            return False, None, str(exc)

        def find_boolean_flag(obj: Any) -> Optional[bool]:
            if isinstance(obj, dict):
                for key in (
                    "isProfanity",
                    "is_profanity",
                    "profanity",
                    "isProfane",
                    "is_profane",
                    "containsProfanity",
                    "contains_profanity",
                    "flagged",
                ):
                    value = obj.get(key)
                    if isinstance(value, bool):
                        return value

                for key in ("result", "data"):
                    nested = obj.get(key)
                    found = find_boolean_flag(nested)
                    if isinstance(found, bool):
                        return found

                matches = obj.get("matches")
                if isinstance(matches, list):
                    return len(matches) > 0

            return None

        profanity_detected = find_boolean_flag(data)
        if profanity_detected is None:
            return False, None, "Unexpected profanity API response format."

        return True, profanity_detected, None

    async def validate_hiring_content(self, payload: Dict) -> Optional[Dict[str, str]]:
        if not self.config.get("content_filter_enabled", True):
            return None

        fields_to_check = {
            "Company Name": str(payload.get("company_name") or ""),
            "Position": str(payload.get("position") or ""),
            "Description": str(payload.get("description") or ""),
        }

        for field_name, value in fields_to_check.items():
            if not value.strip():
                continue

            ok, profanity_detected, error = await self._check_message_with_profanity_api(value)
            if not ok:
                return {"type": "api_error", "error": str(error or "Unknown profanity API error")}

            if profanity_detected:
                return {"type": "blocked", "field": field_name}

        return None

    def _build_hiring_embed(self, user: discord.abc.User, request_data: Dict, request_id: Optional[str] = None) -> discord.Embed:
        embed = discord.Embed(
            title="New Hiring Post",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Submitted By", value=f"{user.mention} ({user})", inline=False)
        embed.add_field(name="Company Name", value=request_data["company_name"], inline=False)
        embed.add_field(name="Position", value=request_data["position"], inline=False)
        embed.add_field(name="Description", value=request_data["description"], inline=False)
        embed.add_field(name="Discord Server Link", value=request_data["discord_server_link"], inline=False)
        post_id_text = str(request_id) if request_id is not None else "Unknown"
        embed.set_footer(text=f"Post ID: {post_id_text}")
        return embed

    async def remove_request_message(self, request_id: str, guild: Optional[discord.Guild] = None):
        mapped = self.request_message_map.get(str(request_id))
        if not mapped:
            return

        channel = None
        if guild is not None:
            channel = guild.get_channel(mapped.get("channel_id"))
        if channel is None:
            channel = self.bot.get_channel(mapped.get("channel_id"))

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
        request_id: Optional[str],
        request_data: Dict,
    ):
        channel = self._get_output_channel(guild)
        if channel is None:
            return False, "Hiring output channel is not configured or not found."

        if request_id is not None:
            await self.remove_request_message(request_id=request_id, guild=guild)

        embed = self._build_hiring_embed(user=user, request_data=request_data, request_id=request_id)
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

    async def update_request(self, request_id: str, guild_id: str, user_id: str, payload: dict):
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

    async def delete_request(self, request_id: str, guild_id: str, user_id: str):
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

    async def get_request_by_id(self, request_id: str, guild_id: str):
        endpoint = self._supabase_endpoint()
        headers = self._supabase_headers()
        params = {
            "select": "*",
            "id": f"eq.{request_id}",
            "guild_id": f"eq.{guild_id}",
            "limit": "1",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(endpoint, params=params, headers=headers) as response:
                    if response.status in (200, 206):
                        data = await response.json(content_type=None)
                        if isinstance(data, list) and data:
                            return True, data[0]
                        return False, "No request found for that ID in this server."
                    body = await response.text()
                    return False, f"HTTP {response.status}: {body[:300]}"
        except Exception as exc:
            return False, str(exc)

    async def list_expired_requests(self, cutoff: datetime):
        endpoint = self._supabase_endpoint()
        headers = self._supabase_headers()
        params = {
            "select": "id,guild_id,user_id,username,submitted_at",
            "submitted_at": f"lt.{cutoff.isoformat()}",
            "order": "submitted_at.asc",
            "limit": "200",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=20)
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

    async def delete_request_by_id_admin(self, request_id: str, guild_id: str):
        endpoint = self._supabase_endpoint()
        headers = self._supabase_headers(prefer="return=minimal")
        params = {
            "id": f"eq.{request_id}",
            "guild_id": f"eq.{guild_id}",
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

    async def notify_request_deleted(
        self,
        *,
        user_id: str,
        request_id: str,
        reason: str,
        deleted_by: str,
    ):
        if not user_id or not user_id.isdigit():
            return

        try:
            user_obj = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
        except Exception:
            return

        try:
            await user_obj.send(
                f"Your hiring request has been deleted.\n"
                f"Post ID: `{request_id}`\n"
                f"Deleted by: {deleted_by}\n"
                f"Reason: {reason}"
            )
        except Exception:
            pass

    @tasks.loop(minutes=30)
    async def auto_delete_loop(self):
        if not self.config:
            return
        if not self.config.get("auto_delete_enabled", True):
            return
        if not self.supabase_ready():
            return

        days = int(self.config.get("auto_delete_days") or 14)
        if days <= 0:
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        ok, rows = await self.list_expired_requests(cutoff=cutoff)
        if not ok:
            return

        for row in rows:
            request_id = str(row.get("id") or "").strip()
            guild_id = str(row.get("guild_id") or "").strip()
            user_id = str(row.get("user_id") or "").strip()
            if not request_id or not guild_id:
                continue

            deleted, _ = await self.delete_request_by_id_admin(request_id=request_id, guild_id=guild_id)
            if not deleted:
                continue

            guild_obj = self.bot.get_guild(int(guild_id)) if guild_id.isdigit() else None
            await self.remove_request_message(request_id=request_id, guild=guild_obj)
            await self.notify_request_deleted(
                user_id=user_id,
                request_id=request_id,
                deleted_by="Automatic Expiry",
                reason=f"Your request expired after {days} day(s).",
            )

    @auto_delete_loop.before_loop
    async def before_auto_delete_loop(self):
        await self.bot.wait_until_ready()

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.group(invoke_without_command=True)
    async def hiringconfig(self, ctx):
        """Configure the hiring plugin."""
        await ctx.send_help(ctx.command)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.group(name="hiring", invoke_without_command=True)
    async def hiring_group(self, ctx):
        """Hiring admin utilities."""
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
    @hiringconfig.command(name="setembedtitle")
    async def hiringconfig_setembedtitle(self, ctx, *, title: str):
        """Set the title used for panel/menu embeds."""
        title = title.strip()
        if not title:
            return await ctx.send("❌ Embed title cannot be empty.")

        normalized = title[:256]
        self.config["panel_embed_title"] = normalized
        await self.update_config()
        await ctx.send(f"✅ Panel/menu embed title set to: {normalized}")

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
    @hiringconfig.command(name="filter")
    async def hiringconfig_filter(self, ctx, enabled: bool):
        """Enable or disable the hiring content filter."""
        self.config["content_filter_enabled"] = enabled
        await self.update_config()
        await ctx.send(
            "✅ Hiring content filter is now enabled."
            if enabled
            else "✅ Hiring content filter is now disabled."
        )

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @hiringconfig.command(name="setfilterapi")
    async def hiringconfig_setfilterapi(self, ctx, url: str):
        """Set the API URL used for hiring profanity filtering."""
        cleaned = url.strip()
        if not cleaned.startswith(("https://", "http://")):
            return await ctx.send("❌ API URL must start with http:// or https://")

        self.config["profanity_api_url"] = cleaned.rstrip("/")
        await self.update_config()
        await ctx.send(f"✅ Hiring content filter API URL set to: {self.config['profanity_api_url']}")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @hiring_group.command(name="requestinfo")
    async def hiringconfig_requestinfo(self, ctx, *, request_id: str):
        """View all database fields for one hiring request by id/uuid."""
        if ctx.guild is None:
            return await ctx.send("❌ This command can only be used in a server.")

        rid = request_id.strip()
        if not rid:
            return await ctx.send("❌ Please provide a request ID.")

        ok, result = await self.get_request_by_id(request_id=rid, guild_id=str(ctx.guild.id))
        if not ok:
            return await ctx.send(f"❌ Could not fetch request: {result}")

        embed = discord.Embed(
            title="Hiring Request Info",
            description=f"Request ID: {rid}",
            color=self.bot.main_color,
        )

        for key, value in result.items():
            field_value = "null" if value is None else str(value)
            if len(field_value) > 1024:
                field_value = field_value[:1021] + "..."
            embed.add_field(name=str(key)[:256], value=field_value, inline=False)

        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @hiring_group.command(name="deleterequest")
    async def hiringconfig_deleterequest(self, ctx, request_id: str, *, reason: str):
        """Delete one hiring request by id/uuid with reason."""
        if ctx.guild is None:
            return await ctx.send("❌ This command can only be used in a server.")

        rid = request_id.strip()
        if not rid:
            return await ctx.send("❌ Please provide a request ID.")

        reason = reason.strip()
        if not reason:
            return await ctx.send("❌ Please provide a reason for deletion.")

        ok_info, row = await self.get_request_by_id(request_id=rid, guild_id=str(ctx.guild.id))
        if not ok_info:
            return await ctx.send(f"❌ Could not fetch request before deletion: {row}")

        ok, result = await self.delete_request_by_id_admin(request_id=rid, guild_id=str(ctx.guild.id))
        if not ok:
            return await ctx.send(f"❌ Could not delete request: {result}")

        await self.remove_request_message(request_id=rid, guild=ctx.guild)
        await self.notify_request_deleted(
            user_id=str(row.get("user_id") or ""),
            request_id=rid,
            deleted_by=str(ctx.author),
            reason=reason,
        )
        await ctx.send(f"✅ Deleted request `{rid}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @hiringconfig.command(name="setautodelete")
    async def hiringconfig_setautodelete(self, ctx, days: int):
        """Set auto-delete age in days for hiring requests."""
        if days < 1 or days > 365:
            return await ctx.send("❌ Days must be between 1 and 365.")

        self.config["auto_delete_days"] = days
        await self.update_config()
        await ctx.send(f"✅ Auto-delete age set to {days} day(s).")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @hiringconfig.command(name="autodelete")
    async def hiringconfig_autodelete(self, ctx, enabled: bool):
        """Enable or disable automatic deletion of old hiring requests."""
        self.config["auto_delete_enabled"] = enabled
        await self.update_config()
        await ctx.send(
            "✅ Auto-delete is now enabled." if enabled else "✅ Auto-delete is now disabled."
        )

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
            name="Content Filter",
            value="Enabled" if self.config.get("content_filter_enabled", True) else "Disabled",
            inline=True,
        )
        embed.add_field(
            name="Filter API URL",
            value=(self.config.get("profanity_api_url") or "https://vector.profanity.dev")[:1024],
            inline=True,
        )
        embed.add_field(name="Hiring Post Title", value="New Hiring Post", inline=False)
        embed.add_field(
            name="Panel Embed Title",
            value=(self.config.get("panel_embed_title") or "Hiring Request Menu")[:256],
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
        embed.add_field(
            name="Auto Delete",
            value="Enabled" if self.config.get("auto_delete_enabled", True) else "Disabled",
            inline=True,
        )
        embed.add_field(
            name="Auto Delete Days",
            value=str(self.config.get("auto_delete_days") or 14),
            inline=True,
        )

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Hiring(bot))
