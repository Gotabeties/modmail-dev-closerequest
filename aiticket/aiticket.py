import asyncio
from collections import OrderedDict
from copy import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Dict, List, Optional

import aiohttp
import discord
from discord.ext import commands
from discord.ext.commands.view import StringView

from core import checks
from core.models import DummyMessage, PermissionLevel
from core.utils import normalize_alias


class AITicket(commands.Cog):
    """Auto-reply to selected Modmail tickets with a Hermes-compatible AI API."""

    def __init__(self, bot):
        self.bot = bot
        if hasattr(bot, "plugin_db"):
            self.db = bot.plugin_db.get_partition(self)
        else:
            self.db = bot.api.get_plugin_partition(self)
        self.default_config = {
            "enabled": False,
            "base_url": "https://hermes.ai.unturf.com/v1",
            "api_key": "choose-any-value",
            "model": "adamo1139/Hermes-3-Llama-3.1-8B-FP8-Dynamic",
            "temperature": 0.5,
            "max_tokens": 220,
            "history_messages": 8,
            "request_timeout": 45,
            "cooldown_seconds": 8,
            "allowed_category_ids": [],
            "allowed_channel_ids": [],
            "escalation_category_id": None,
            "ai_handoff_enabled": True,
            "handoff_ping_everyone": True,
            "escalation_keywords": [
                "real person",
                "human",
                "agent",
                "staff",
                "someone real",
                "talk to a person",
                "speak to a person",
                "representative",
            ],
            "system_prompt": (
                "You are a senior Modmail support assistant. Provide concise, practical, and accurate help. "
                "Use clear steps when troubleshooting. If information is missing, ask one targeted follow-up question. "
                "Do not invent policies, actions, or outcomes. Never claim something is done unless it is confirmed. "
                "Always check the provided ticket knowledge base context first and prefer it over general knowledge. "
                "Only fall back to broader Modmail knowledge if the ticket knowledge base has no useful match. "
                "When past ticket knowledge is provided, use it as context and adapt it to the current case. "
                "Do not copy prior replies word-for-word unless quoting a short required phrase. "
                "Write an original response tailored to this user and this ticket. "
                "Do not rely on external web search or internet browsing."
            ),
            "error_notice_enabled": False,
            "escalate_on_error": True,
            "thinking_message": "Thinking...",
            "reply_command": "freply",
            "edit_command": "edit",
            "retrieval_enabled": False,
            "retrieval_top_k": 3,
            "retrieval_min_score": 0.12,
            "retrieval_max_snippet_chars": 350,
            "retrieval_max_context_chars": 1600,
            "kb_max_entries": 5000,
            "db_collection_candidates": ["logs", "tickets", "threads", "transcripts"],
            "db_import_limit": 1000,
        }
        self.config = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._thread_locks: Dict[int, asyncio.Lock] = {}
        self._last_reply_at: Dict[int, datetime] = {}
        self._processed_messages = OrderedDict()
        self._runtime = {
            "thread_reply_events": 0,
            "message_fallback_events": 0,
            "processed": 0,
            "skipped_from_mod": 0,
            "skipped_not_allowed": 0,
            "errors": 0,
        }
        self._kb_entries: List[Dict[str, str]] = []
        self._kb_tokens: Dict[str, set] = {}

    async def cog_load(self):
        self.config = await self.db.find_one({"_id": "aiticket-config"})
        if self.config is None:
            self.config = self.default_config.copy()
            await self.update_config()

        missing = [k for k in self.default_config if k not in self.config]
        if missing:
            for key in missing:
                self.config[key] = self.default_config[key]
            await self.update_config()

        await self._load_kb()

        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    async def update_config(self):
        await self.db.find_one_and_update(
            {"_id": "aiticket-config"},
            {"$set": self.config},
            upsert=True,
        )

    async def _load_kb(self):
        self._kb_entries = []
        self._kb_tokens = {}
        try:
            await self._import_kb_from_modmail_logs(limit=int(self.config.get("db_import_limit", 1000)))
        except Exception:
            # Best effort only; knowledge context will refresh later via kbimportdb.
            self._kb_entries = []
            self._kb_tokens = {}

    async def _save_kb(self):
        return None

    @staticmethod
    def _tokenize(text: str) -> set:
        words = re.findall(r"[a-zA-Z0-9_]{3,}", (text or "").lower())
        return set(words)

    @staticmethod
    def _similarity(query_tokens: set, doc_tokens: set) -> float:
        if not query_tokens or not doc_tokens:
            return 0.0
        overlap = len(query_tokens.intersection(doc_tokens))
        if overlap == 0:
            return 0.0
        return overlap / math.sqrt(len(query_tokens) * len(doc_tokens))

    @staticmethod
    def _extract_json_text(obj) -> str:
        chunks = []

        def walk(value):
            if isinstance(value, dict):
                preferred = ["content", "message", "text", "body", "subject", "resolution"]
                for key in preferred:
                    if key in value and isinstance(value[key], str):
                        chunks.append(value[key])
                for v in value.values():
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)
            elif isinstance(value, str):
                chunks.append(value)

        walk(obj)
        return "\n".join(chunks)

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _read_transcript_file(self, file_path: Path) -> str:
        try:
            suffix = file_path.suffix.lower()
            if suffix == ".json":
                with file_path.open("r", encoding="utf-8") as f:
                    obj = json.load(f)
                return self._normalize_text(self._extract_json_text(obj))

            with file_path.open("r", encoding="utf-8", errors="ignore") as f:
                return self._normalize_text(f.read())
        except Exception:
            return ""

    def _get_db_handles(self) -> List:
        handles = []

        def add(handle):
            if handle is None:
                return
            if handle in handles:
                return
            handles.append(handle)

        add(getattr(self.bot, "db", None))
        add(getattr(self.bot, "database", None))

        api = getattr(self.bot, "api", None)
        if api is not None:
            add(getattr(api, "db", None))
            add(getattr(api, "database", None))

        mongo = getattr(self.bot, "mongo", None)
        if mongo is not None:
            add(getattr(mongo, "db", None))
            add(getattr(mongo, "database", None))

        return handles

    async def _iter_collection_docs(self, collection, limit: int) -> List[dict]:
        docs = []
        safe_limit = max(1, limit)

        try:
            cursor = collection.find({})
        except Exception:
            return docs

        # Motor cursor path
        if hasattr(cursor, "to_list"):
            try:
                docs = await cursor.to_list(length=safe_limit)
                return [d for d in docs if isinstance(d, dict)]
            except Exception:
                docs = []

        # Async iterator path
        try:
            count = 0
            async for item in cursor:
                if isinstance(item, dict):
                    docs.append(item)
                    count += 1
                if count >= safe_limit:
                    break
            if docs:
                return docs
        except Exception:
            pass

        # Sync iterator path
        try:
            for item in cursor:
                if isinstance(item, dict):
                    docs.append(item)
                if len(docs) >= safe_limit:
                    break
        except Exception:
            pass

        return docs

    def _extract_ticket_text_from_doc(self, doc: dict) -> str:
        if not isinstance(doc, dict):
            return ""

        pieces = []

        def walk(value):
            if isinstance(value, dict):
                preferred_keys = [
                    "message",
                    "content",
                    "text",
                    "body",
                    "subject",
                    "title",
                    "response",
                    "resolution",
                    "plain",
                ]
                for key in preferred_keys:
                    candidate = value.get(key)
                    if isinstance(candidate, str):
                        candidate = candidate.strip()
                        if candidate:
                            pieces.append(candidate)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
            elif isinstance(value, str):
                cleaned = value.strip()
                if cleaned:
                    pieces.append(cleaned)

        walk(doc)
        text = self._normalize_text("\n".join(pieces))
        if len(text) > 14000:
            text = text[:14000]
        return text

    def _extract_modmail_log_text(self, log_doc: dict) -> str:
        if not isinstance(log_doc, dict):
            return ""

        pieces = []
        title = str(log_doc.get("title") or "").strip()
        close_message = str(log_doc.get("close_message") or "").strip()
        if title:
            pieces.append(f"Title: {title}")
        if close_message:
            pieces.append(f"Close message: {close_message}")

        for msg in log_doc.get("messages", []) or []:
            if not isinstance(msg, dict):
                continue
            content = str(msg.get("content") or "").strip()
            if not content:
                continue

            author = msg.get("author") or {}
            name = str(author.get("name") or "user").strip()
            mod_flag = bool(author.get("mod", False))
            role = "staff" if mod_flag else "user"
            pieces.append(f"[{role}:{name}] {content}")

        text = self._normalize_text("\n".join(pieces))
        if len(text) > 14000:
            text = text[:14000]
        return text

    async def _import_kb_from_modmail_logs(self, *, limit: int):
        api = getattr(self.bot, "api", None)
        logs_collection = getattr(api, "logs", None)
        if logs_collection is None:
            return 0, 0, "bot.api.logs not available."

        guild_id = getattr(self.bot, "guild_id", None)
        query = {"open": False}
        if guild_id is not None:
            query["guild_id"] = str(guild_id)

        projection = {
            "key": 1,
            "channel_id": 1,
            "title": 1,
            "close_message": 1,
            "messages": 1,
        }

        docs = []
        safe_limit = max(1, int(limit))
        try:
            cursor = logs_collection.find(query, projection)
            if hasattr(cursor, "sort"):
                try:
                    cursor = cursor.sort("closed_at", -1)
                except Exception:
                    pass

            if hasattr(cursor, "to_list"):
                docs = await cursor.to_list(length=safe_limit)
            else:
                async for item in cursor:
                    docs.append(item)
                    if len(docs) >= safe_limit:
                        break
        except Exception as exc:
            return 0, 0, f"Failed querying logs collection: {exc}"

        if not docs:
            return 0, 0, "No closed logs found in logs collection."

        max_entries = max(100, min(50000, int(self.config.get("kb_max_entries", 5000))))
        existing = {item["id"]: item for item in self._kb_entries}

        imported = 0
        scanned = 0
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            scanned += 1
            text = self._extract_modmail_log_text(doc)
            if len(text) < 40:
                continue

            source_key = str(doc.get("key") or doc.get("channel_id") or doc.get("_id") or "unknown")
            doc_id = hashlib.sha1(("logs\n" + source_key + "\n" + text[:500]).encode("utf-8", errors="ignore")).hexdigest()
            existing[doc_id] = {
                "id": doc_id,
                "source": f"db:logs:{source_key}",
                "text": text,
            }
            imported += 1

        new_entries = list(existing.values())
        if len(new_entries) > max_entries:
            new_entries = new_entries[-max_entries:]

        self._kb_entries = new_entries
        self._kb_tokens = {item["id"]: self._tokenize(item["text"]) for item in self._kb_entries}
        await self._save_kb()

        return imported, scanned, "Imported from Modmail logs collection via bot.api.logs."

    async def _import_kb_from_database(self, *, limit: int, preferred_collection: Optional[str] = None):
        preferred = (preferred_collection or "").strip().lower()
        prefer_logs = preferred in {"", "logs", "log", "modmail_logs"}
        if prefer_logs:
            imported, scanned, detail = await self._import_kb_from_modmail_logs(limit=limit)
            if imported > 0:
                return imported, scanned, detail

        handles = self._get_db_handles()
        if not handles:
            return 0, 0, "No database handle found on bot object."

        candidates = []
        if preferred_collection:
            candidates.append(preferred_collection.strip())
        for name in self.config.get("db_collection_candidates", []):
            name = str(name).strip()
            if name and name not in candidates:
                candidates.append(name)

        max_entries = max(100, min(50000, int(self.config.get("kb_max_entries", 5000))))
        existing = {item["id"]: item for item in self._kb_entries}

        imported = 0
        scanned = 0
        successful_collection = None

        for db in handles:
            for name in candidates:
                collection = None
                try:
                    collection = db[name]
                except Exception:
                    collection = getattr(db, name, None)

                if collection is None:
                    continue

                docs = await self._iter_collection_docs(collection, limit)
                if not docs:
                    continue

                successful_collection = name
                for doc in docs:
                    scanned += 1
                    text = self._extract_ticket_text_from_doc(doc)
                    if len(text) < 40:
                        continue

                    source_key = str(doc.get("_id") or doc.get("id") or "unknown")
                    doc_id = hashlib.sha1((name + "\n" + source_key + "\n" + text[:500]).encode("utf-8", errors="ignore")).hexdigest()
                    existing[doc_id] = {
                        "id": doc_id,
                        "source": f"db:{name}:{source_key}",
                        "text": text,
                    }
                    imported += 1

                if imported > 0:
                    break
            if imported > 0:
                break

        if imported == 0:
            return 0, scanned, "No usable transcript records found in configured collections."

        new_entries = list(existing.values())
        if len(new_entries) > max_entries:
            new_entries = new_entries[-max_entries:]

        self._kb_entries = new_entries
        self._kb_tokens = {item["id"]: self._tokenize(item["text"]) for item in self._kb_entries}
        await self._save_kb()

        return imported, scanned, f"Imported from collection '{successful_collection}'."

    def _build_retrieval_context(self, user_text: str) -> str:
        if not self.config.get("retrieval_enabled", False):
            return ""

        query_tokens = self._tokenize(user_text)
        if not query_tokens or not self._kb_entries:
            return ""

        top_k = max(1, min(10, int(self.config.get("retrieval_top_k", 3))))
        min_score = float(self.config.get("retrieval_min_score", 0.12))
        snippet_chars = max(100, min(800, int(self.config.get("retrieval_max_snippet_chars", 350))))
        max_context_chars = max(400, min(5000, int(self.config.get("retrieval_max_context_chars", 1600))))

        scored = []
        for item in self._kb_entries:
            tokens = self._kb_tokens.get(item["id"], set())
            score = self._similarity(query_tokens, tokens)
            if score >= min_score:
                scored.append((score, item))

        if not scored:
            return ""

        scored.sort(key=lambda x: x[0], reverse=True)
        lines = []
        total = 0
        for score, item in scored[:top_k]:
            snippet = item["text"][:snippet_chars]
            line = f"- Source: {item['source']} | Score: {score:.3f} | Snippet: {snippet}"
            if total + len(line) > max_context_chars:
                break
            lines.append(line)
            total += len(line)

        if not lines:
            return ""

        return "Relevant past resolved tickets (use as guidance, not strict rules):\n" + "\n".join(lines)

    @staticmethod
    def _parse_json_response(raw_text: str) -> dict:
        text = (raw_text or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)

        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("No JSON object found in AI response.")

        return json.loads(text[start : end + 1])

    async def _request_ai_ticket_action(self, messages: List[dict]) -> dict:
        decision_messages = [
            {
                "role": "system",
                "content": (
                    "You are deciding whether a ticket should be handled by the AI or handed to humans. "
                    "Use the retrieved ticket knowledge base first. If the user needs a human or the issue is out of scope, set handoff=true. "
                    "Return JSON only with keys: reply, handoff, handoff_reason. "
                    "reply must be the user-facing response. If handoff is true, reply should be a short escalation acknowledgement. "
                    "Do not include markdown fences or extra text."
                ),
            },
        ] + messages

        raw_reply = await self._request_ai_reply(decision_messages)
        try:
            data = self._parse_json_response(raw_reply)
        except Exception:
            # Fallback to treating the raw response as a normal reply.
            return {"reply": raw_reply.strip(), "handoff": False, "handoff_reason": ""}

        reply = str(data.get("reply", "")).strip()
        handoff = bool(data.get("handoff", False))
        handoff_reason = str(data.get("handoff_reason", "")).strip()

        if not reply:
            reply = raw_reply.strip() or "I’m checking on that now."

        return {"reply": reply[:1900], "handoff": handoff, "handoff_reason": handoff_reason[:400]}

    @staticmethod
    def _clean_list_ids(values: List[int]) -> List[int]:
        deduped = []
        seen = set()
        for value in values:
            ivalue = int(value)
            if ivalue in seen:
                continue
            seen.add(ivalue)
            deduped.append(ivalue)
        return deduped

    def _channel_is_allowed(self, channel) -> bool:
        if channel is None:
            return False

        allowed_channels = {int(x) for x in self.config.get("allowed_channel_ids", [])}
        allowed_categories = {int(x) for x in self.config.get("allowed_category_ids", [])}

        if not allowed_channels and not allowed_categories:
            return False

        if allowed_channels and channel.id in allowed_channels:
            return True

        channel_category_id = getattr(channel, "category_id", None)
        if allowed_categories and channel_category_id in allowed_categories:
            return True

        return False

    def _thread_is_allowed(self, thread) -> bool:
        return self._channel_is_allowed(getattr(thread, "channel", None))

    def _is_already_processed(self, message_id: int) -> bool:
        if message_id in self._processed_messages:
            return True

        self._processed_messages[message_id] = True
        if len(self._processed_messages) > 500:
            self._processed_messages.popitem(last=False)
        return False

    def _is_escalation_request(self, content: str) -> bool:
        text = (content or "").strip().lower()
        if not text:
            return False

        for phrase in self.config.get("escalation_keywords", []):
            phrase = str(phrase).strip().lower()
            if phrase and phrase in text:
                return True
        return False

    @staticmethod
    def _extract_message_text(message: discord.Message) -> str:
        text = (getattr(message, "content", "") or "").strip()
        if text:
            return text

        for embed in getattr(message, "embeds", []) or []:
            if getattr(embed, "description", None):
                desc = str(embed.description).strip()
                if desc:
                    return desc
            for field in getattr(embed, "fields", []) or []:
                value = str(getattr(field, "value", "") or "").strip()
                if value:
                    return value

        return ""

    @staticmethod
    def _looks_like_relay_embed(message: discord.Message) -> bool:
        embeds = getattr(message, "embeds", []) or []
        if not embeds:
            return False

        for embed in embeds:
            footer = getattr(embed, "footer", None)
            footer_text = str(getattr(footer, "text", "") or "")
            if "Message ID:" in footer_text:
                return True
        return False

    async def _move_thread_to_escalation(self, thread, requested_by: discord.abc.User):
        channel = getattr(thread, "channel", None)
        if channel is None:
            return False, "Ticket channel not found."

        escalation_category_id = self.config.get("escalation_category_id")
        if escalation_category_id is None:
            return False, "Escalation category is not configured."

        category = channel.guild.get_channel(int(escalation_category_id))
        if category is None or not isinstance(category, discord.CategoryChannel):
            return False, "Configured escalation category no longer exists."

        if channel.category_id == category.id:
            return True, "Ticket is already in the escalation category."

        try:
            await channel.edit(
                category=category,
                reason=f"AI escalation requested by {requested_by} ({requested_by.id})",
            )
            return True, f"Moved this ticket to **{category.name}** for human support."
        except discord.Forbidden:
            return False, "I do not have permission to move this channel."
        except discord.HTTPException as exc:
            return False, f"Failed to move channel: {exc}"

    async def _build_ai_messages(self, thread, user_message: discord.Message, incoming_text: str = "") -> List[dict]:
        channel = getattr(thread, "channel", None)
        messages = [{"role": "system", "content": self.config.get("system_prompt", "You are helpful.")}]

        user_text = (incoming_text or self._extract_message_text(user_message) or "")[:2000]
        retrieval_context = self._build_retrieval_context(user_text)
        if retrieval_context:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "The retrieved ticket context is the first source to consult. "
                        "Use it before general knowledge, summarize it, and adapt it to this case. "
                        "Do not copy prior ticket wording verbatim."
                    ),
                }
            )
            messages.append({"role": "system", "content": retrieval_context})

        if channel is None:
            messages.append({"role": "user", "content": user_text})
            return messages

        history_count = max(1, int(self.config.get("history_messages", 8)))
        history = []
        try:
            async for msg in channel.history(limit=history_count, oldest_first=False):
                if not self._extract_message_text(msg):
                    continue
                history.append(msg)
        except Exception:
            history = []

        for msg in reversed(history):
            if msg.id == user_message.id:
                role = "user"
            elif msg.author.id == self.bot.user.id:
                role = "assistant"
            else:
                role = "user"

            content = self._extract_message_text(msg)
            if not content:
                continue

            messages.append({"role": role, "content": content[:2000]})

        if user_text and not any(item.get("content") == user_text for item in messages if item["role"] == "user"):
            messages.append({"role": "user", "content": user_text})

        return messages

    async def _request_ai_reply(self, messages: List[dict]) -> str:
        if self.session is None:
            raise RuntimeError("HTTP session is not ready")

        base_url = str(self.config.get("base_url", "https://hermes.ai.unturf.com/v1")).rstrip("/")
        endpoint = f"{base_url}/chat/completions"

        headers = {
            "Authorization": f"Bearer {self.config.get('api_key', 'choose-any-value')}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.config.get("model", "adamo1139/Hermes-3-Llama-3.1-8B-FP8-Dynamic"),
            "messages": messages,
            "temperature": float(self.config.get("temperature", 0.5)),
            "max_tokens": int(self.config.get("max_tokens", 220)),
        }

        timeout = aiohttp.ClientTimeout(total=max(5, int(self.config.get("request_timeout", 45))))
        async with self.session.post(endpoint, json=payload, headers=headers, timeout=timeout) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                message = data.get("error", data) if isinstance(data, dict) else data
                raise RuntimeError(f"AI API HTTP {response.status}: {message}")

            choices = data.get("choices", []) if isinstance(data, dict) else []
            if not choices:
                raise RuntimeError("AI API returned no choices.")

            content = choices[0].get("message", {}).get("content", "")
            content = str(content).strip()
            if not content:
                raise RuntimeError("AI API returned empty content.")

            return content[:1900]

    def _is_in_cooldown(self, thread_id: int) -> bool:
        return self._cooldown_remaining(thread_id) > 0

    def _cooldown_remaining(self, thread_id: int) -> int:
        cooldown = max(0, int(self.config.get("cooldown_seconds", 8)))
        if cooldown == 0:
            return 0

        now = datetime.now(timezone.utc)
        last = self._last_reply_at.get(thread_id)
        if last is None:
            return 0

        elapsed = (now - last).total_seconds()
        if elapsed >= cooldown:
            return 0
        return int(cooldown - elapsed + 0.999)

    async def _invoke_modmail_command(self, command_text: str, thread, message) -> bool:
        """Invoke a Modmail command in thread context, bypassing permission checks."""
        ctxs = []
        aliases = normalize_alias(command_text)
        for alias in aliases:
            view = StringView(self.bot.prefix + alias)
            ctx_ = commands.Context(prefix=self.bot.prefix, view=view, bot=self.bot, message=message)
            ctx_.thread = thread
            discord.utils.find(view.skip_string, await self.bot.get_prefix())
            ctx_.invoked_with = view.get_word().lower()
            ctx_.command = self.bot.all_commands.get(ctx_.invoked_with)
            ctxs.append(ctx_)

        invoked = False
        for ctx in ctxs:
            if ctx.command:
                invoked = True
                old_checks = copy(ctx.command.checks)
                ctx.command.checks = [checks.has_permissions(PermissionLevel.INVALID)]
                await self.bot.invoke(ctx)
                ctx.command.checks = old_checks
        return invoked

    async def _send_ticket_reply(self, thread, source_message: discord.Message, text: str) -> bool:
        """Send outward user reply using configurable Modmail reply command alias."""
        if thread is None:
            return False

        configured = str(self.config.get("reply_command", "freply")).strip().lower() or "freply"
        candidates = []
        for cmd in [configured, "freply", "areply", "reply"]:
            if cmd not in candidates:
                candidates.append(cmd)

        for reply_command in candidates:
            dummy = DummyMessage(copy(source_message))
            dummy.author = self.bot.user or source_message.author
            dummy.content = f"{self.bot.prefix}{reply_command} {text}"
            invoked = await self._invoke_modmail_command(f"{reply_command} {text}", thread, dummy)
            if invoked:
                return True

        return False

    async def _edit_ticket_reply(self, thread, source_message: discord.Message, text: str) -> bool:
        """Edit outward user reply using configurable Modmail edit command alias."""
        if thread is None:
            return False

        configured = str(self.config.get("edit_command", "edit")).strip().lower() or "edit"
        candidates = []
        for cmd in [configured, "edit"]:
            if cmd not in candidates:
                candidates.append(cmd)

        for edit_command in candidates:
            dummy = DummyMessage(copy(source_message))
            dummy.author = self.bot.user or source_message.author
            dummy.content = f"{self.bot.prefix}{edit_command} {text}"
            invoked = await self._invoke_modmail_command(f"{edit_command} {text}", thread, dummy)
            if invoked:
                return True

        return False

    async def _resolve_thread_from_channel(self, channel):
        manager = getattr(self.bot, "threads", None)
        if manager is None or channel is None:
            return None

        candidates = [
            ("find", (), {"channel": channel}),
            ("find", (channel,), {}),
            ("get", (), {"channel": channel}),
            ("get", (channel,), {}),
            ("find_by_channel", (channel,), {}),
            ("from_channel", (channel,), {}),
        ]

        for method_name, args, kwargs in candidates:
            method = getattr(manager, method_name, None)
            if method is None:
                continue

            try:
                result = method(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    result = await result
                if result is not None:
                    return result
            except Exception:
                continue

        return None

    async def _handle_incoming_message(self, thread, from_mod: bool, message: discord.Message):
        if from_mod:
            self._runtime["skipped_from_mod"] += 1
            return

        channel = getattr(thread, "channel", None) if thread is not None else getattr(message, "channel", None)
        if not self._channel_is_allowed(channel):
            self._runtime["skipped_not_allowed"] += 1
            return

        if self._is_already_processed(int(message.id)):
            return

        incoming_text = self._extract_message_text(message)
        if not incoming_text:
            return

        thread_id = int(getattr(thread, "id", 0) or channel.id)

        lock = self._thread_locks.setdefault(thread_id, asyncio.Lock())
        if lock.locked():
            return

        async with lock:
            status_sent = False
            remaining = self._cooldown_remaining(thread_id)
            if remaining > 0:
                waiting_text = f"⏳ Waiting for cooldown ({remaining}s)..."
                status_sent = await self._send_ticket_reply(thread, message, waiting_text)
                await asyncio.sleep(remaining)

            if self._is_escalation_request(incoming_text):
                if thread is None:
                    return

                ok, status_message = await self._move_thread_to_escalation(thread, message.author)
                if ok:
                    handoff_text = "I moved this ticket for a human team member to continue with you."
                    sent = await self._edit_ticket_reply(thread, message, handoff_text) if status_sent else False
                    if not sent:
                        await self._send_ticket_reply(thread, message, handoff_text)
                    try:
                        await thread.channel.send(
                            f"@everyone Human handoff requested: {status_message}",
                            allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=True),
                        )
                    except Exception:
                        pass
                return

            thinking_text = str(self.config.get("thinking_message", "Thinking...")).strip() or "Thinking..."
            if status_sent:
                edited = await self._edit_ticket_reply(thread, message, f"🤖 {thinking_text}")
                if not edited:
                    status_sent = await self._send_ticket_reply(thread, message, f"🤖 {thinking_text}")
            else:
                status_sent = await self._send_ticket_reply(thread, message, f"🤖 {thinking_text}")

            try:
                prompt_messages = await self._build_ai_messages(
                    thread if thread is not None else type("T", (), {"channel": channel})(),
                    message,
                    incoming_text=incoming_text,
                )
                ai_result = await self._request_ai_ticket_action(prompt_messages)
                ai_reply = str(ai_result.get("reply") or "").strip()
                should_handoff = bool(ai_result.get("handoff", False)) and self.config.get("ai_handoff_enabled", True)
                handoff_reason = str(ai_result.get("handoff_reason") or "").strip()

                if should_handoff and thread is not None:
                    moved, move_status = await self._move_thread_to_escalation(thread, message.author)
                    if moved:
                        handoff_ping = f"@everyone Human handoff requested by AI: {handoff_reason or move_status}"
                        try:
                            await thread.channel.send(
                                handoff_ping,
                                allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=True),
                            )
                        except Exception:
                            pass

                sent = await self._edit_ticket_reply(thread, message, ai_reply) if status_sent else False
                if not sent:
                    await self._send_ticket_reply(thread, message, ai_reply)
                self._last_reply_at[thread_id] = datetime.now(timezone.utc)
                self._runtime["processed"] += 1
            except Exception as exc:
                self._runtime["errors"] += 1
                escalated = False
                escalation_status = None

                if self.config.get("escalate_on_error", True):
                    if thread is not None:
                        escalated, escalation_status = await self._move_thread_to_escalation(thread, message.author)
                    else:
                        escalation_category_id = self.config.get("escalation_category_id")
                        if escalation_category_id is not None:
                            category = channel.guild.get_channel(int(escalation_category_id))
                            if isinstance(category, discord.CategoryChannel):
                                try:
                                    await channel.edit(category=category, reason="AI auto-reply failure escalation")
                                    escalated = True
                                    escalation_status = f"Moved this ticket to **{category.name}** for human support."
                                except Exception as move_exc:
                                    escalation_status = f"Failed to move channel: {move_exc}"
                        else:
                            escalation_status = "Escalation category is not configured."

                if escalated:
                    error_text = "I hit an error while generating a reply. I moved this ticket so a human team member can help you next."
                else:
                    error_text = "I hit an error while generating a reply. A staff member will continue helping you."

                sent = await self._edit_ticket_reply(thread, message, error_text) if status_sent else False
                if not sent:
                    await self._send_ticket_reply(thread, message, error_text)

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, from_mod, message, anonymous, plain):
        if not self.config.get("enabled", False):
            return

        self._runtime["thread_reply_events"] += 1
        await self._handle_incoming_message(thread, from_mod, message)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.config.get("enabled", False):
            return

        if message.guild is None:
            return

        is_webhook_relay = message.webhook_id is not None
        is_bot_embed_relay = (
            message.author.id == self.bot.user.id and self._looks_like_relay_embed(message)
        )

        # Fallback for Modmail builds where inbound user relays don't trigger on_thread_reply.
        if not is_webhook_relay and not is_bot_embed_relay:
            return

        if not self._channel_is_allowed(message.channel):
            return

        thread = await self._resolve_thread_from_channel(message.channel)
        if thread is None:
            return

        self._runtime["message_fallback_events"] += 1
        await self._handle_incoming_message(thread, False, message)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.group(name="aiticket", invoke_without_command=True)
    async def aiticket_group(self, ctx):
        """Configure AI auto-reply behavior for selected Modmail tickets."""
        await ctx.send_help(ctx.command)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="status")
    async def aiticket_status(self, ctx):
        """Show current AI auto-reply settings."""
        allowed_channels = self.config.get("allowed_channel_ids", [])
        allowed_categories = self.config.get("allowed_category_ids", [])
        escalation_category = self.config.get("escalation_category_id")
        keywords = self.config.get("escalation_keywords", [])

        embed = discord.Embed(title="AI Ticket Auto-Reply", color=self.bot.main_color)
        embed.add_field(name="Enabled", value=str(self.config.get("enabled", False)), inline=True)
        embed.add_field(name="Model", value=str(self.config.get("model", "N/A"))[:1024], inline=False)
        embed.add_field(name="Base URL", value=str(self.config.get("base_url", "N/A"))[:1024], inline=False)
        embed.add_field(name="Temperature", value=str(self.config.get("temperature", 0.5)), inline=True)
        embed.add_field(name="Max Tokens", value=str(self.config.get("max_tokens", 220)), inline=True)
        embed.add_field(name="Cooldown (s)", value=str(self.config.get("cooldown_seconds", 8)), inline=True)
        embed.add_field(name="Escalate On Error", value=str(self.config.get("escalate_on_error", True)), inline=True)
        embed.add_field(name="Thinking Message", value=str(self.config.get("thinking_message", "Thinking..."))[:1024], inline=False)
        embed.add_field(name="Reply Command", value=str(self.config.get("reply_command", "reply")), inline=True)
        embed.add_field(name="Edit Command", value=str(self.config.get("edit_command", "edit")), inline=True)
        embed.add_field(name="Retrieval Enabled", value=str(self.config.get("retrieval_enabled", False)), inline=True)
        embed.add_field(name="Retrieval Top K", value=str(self.config.get("retrieval_top_k", 3)), inline=True)
        embed.add_field(name="KB Entries", value=str(len(self._kb_entries)), inline=True)
        embed.add_field(name="AI Handoff", value=str(self.config.get("ai_handoff_enabled", True)), inline=True)
        embed.add_field(name="DB Import Limit", value=str(self.config.get("db_import_limit", 1000)), inline=True)
        embed.add_field(
            name="DB Collections",
            value=", ".join(self.config.get("db_collection_candidates", []))[:1024] or "None",
            inline=False,
        )
        runtime = self._runtime
        embed.add_field(
            name="Runtime",
            value=(
                f"thread_reply_events={runtime['thread_reply_events']}, "
                f"fallback_events={runtime['message_fallback_events']}, "
                f"processed={runtime['processed']}, "
                f"errors={runtime['errors']}"
            )[:1024],
            inline=False,
        )

        embed.add_field(
            name="Allowed Channels",
            value=", ".join(f"<#{cid}>" for cid in allowed_channels) if allowed_channels else "None",
            inline=False,
        )
        embed.add_field(
            name="Allowed Categories",
            value=", ".join(f"`{cid}`" for cid in allowed_categories) if allowed_categories else "None",
            inline=False,
        )

        if escalation_category:
            embed.add_field(name="Escalation Category", value=f"`{escalation_category}`", inline=False)
        else:
            embed.add_field(name="Escalation Category", value="Not set", inline=False)

        embed.add_field(name="Escalation Keywords", value=", ".join(keywords[:25]) if keywords else "None", inline=False)
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="kbimport")
    async def aiticket_kbimport(self, ctx, *, path: str):
        """Import past ticket transcripts from a file or folder path."""
        root = Path(path.strip().strip('"').strip("'"))
        if not root.exists():
            await ctx.send("❌ Path not found.")
            return

        await ctx.send("📚 Importing ticket transcripts into AI knowledge base...")

        files = []
        allowed_ext = {".txt", ".log", ".md", ".json"}
        if root.is_file():
            files = [root]
        else:
            for p in root.rglob("*"):
                if p.is_file() and p.suffix.lower() in allowed_ext:
                    files.append(p)

        if not files:
            await ctx.send("❌ No transcript files found (.txt/.log/.md/.json).")
            return

        existing = {item["id"]: item for item in self._kb_entries}
        imported = 0
        skipped = 0

        for p in files:
            text = self._read_transcript_file(p)
            if len(text) < 40:
                skipped += 1
                continue

            # Keep each document concise for retrieval and prompt injection.
            text = text[:12000]
            doc_id = hashlib.sha1((str(p.resolve()) + "\n" + text[:500]).encode("utf-8", errors="ignore")).hexdigest()
            existing[doc_id] = {
                "id": doc_id,
                "source": str(p.name),
                "text": text,
            }
            imported += 1

        max_entries = max(100, min(50000, int(self.config.get("kb_max_entries", 5000))))
        new_entries = list(existing.values())
        new_entries.sort(key=lambda x: x.get("source", ""))
        if len(new_entries) > max_entries:
            new_entries = new_entries[-max_entries:]

        self._kb_entries = new_entries
        self._kb_tokens = {item["id"]: self._tokenize(item["text"]) for item in self._kb_entries}
        await self._save_kb()

        await ctx.send(
            f"✅ KB import complete. Imported/updated: {imported}, skipped: {skipped}, total indexed: {len(self._kb_entries)}"
        )

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="kbimportdb")
    async def aiticket_kbimportdb(self, ctx, limit: int = None, *, collection: str = None):
        """Import transcripts from Modmail database collections."""
        if limit is None:
            limit = int(self.config.get("db_import_limit", 1000))
        limit = max(10, min(100000, int(limit)))

        await ctx.send("📚 Importing ticket transcripts from database...")
        imported, scanned, detail = await self._import_kb_from_database(
            limit=limit,
            preferred_collection=collection,
        )

        if imported == 0:
            await ctx.send(f"❌ KB DB import failed. Scanned: {scanned}. {detail}")
            return

        await ctx.send(
            f"✅ KB DB import complete. Imported/updated: {imported}, scanned: {scanned}, total indexed: {len(self._kb_entries)}. {detail}"
        )

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="kbclear")
    async def aiticket_kbclear(self, ctx):
        """Clear all indexed ticket knowledge entries."""
        self._kb_entries = []
        self._kb_tokens = {}
        await self._save_kb()
        await ctx.send("✅ Cleared all AI ticket knowledge entries.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="kbsearch")
    async def aiticket_kbsearch(self, ctx, *, query: str):
        """Search indexed ticket knowledge for similar past issues."""
        query = query.strip()
        if not query:
            await ctx.send("❌ Query cannot be empty.")
            return

        tokens = self._tokenize(query)
        if not tokens:
            await ctx.send("❌ Query needs at least one word with 3+ characters.")
            return

        scored = []
        for item in self._kb_entries:
            score = self._similarity(tokens, self._kb_tokens.get(item["id"], set()))
            if score > 0:
                scored.append((score, item))

        if not scored:
            await ctx.send("ℹ️ No similar indexed tickets found.")
            return

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:5]
        lines = []
        for score, item in top:
            snippet = item["text"][:180].replace("\n", " ")
            lines.append(f"- {item['source']} (score={score:.3f}) :: {snippet}")

        await ctx.send("\n".join(lines)[:1900])

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="test")
    async def aiticket_test(self, ctx, *, prompt: str = None):
        """Test AI endpoint/model and return a sample response."""
        test_prompt = (prompt or "Reply with exactly: AI test successful.").strip()
        if len(test_prompt) > 800:
            await ctx.send("❌ Test prompt is too long. Keep it under 800 characters.")
            return

        await ctx.send("🧪 Testing AI endpoint...")
        started = datetime.now(timezone.utc)

        messages = [
            {"role": "system", "content": self.config.get("system_prompt", "You are helpful.")},
            {"role": "user", "content": test_prompt},
        ]

        try:
            ai_reply = await self._request_ai_reply(messages)
            elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)

            embed = discord.Embed(title="✅ AI Test Passed", color=discord.Color.green())
            embed.add_field(name="Model", value=str(self.config.get("model", "N/A"))[:1024], inline=False)
            embed.add_field(name="Base URL", value=str(self.config.get("base_url", "N/A"))[:1024], inline=False)
            embed.add_field(name="Latency", value=f"{elapsed_ms} ms", inline=True)
            embed.add_field(name="Sample Reply", value=ai_reply[:1024], inline=False)
            await ctx.send(embed=embed)
        except Exception as exc:
            elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            embed = discord.Embed(title="❌ AI Test Failed", color=discord.Color.red())
            embed.add_field(name="Model", value=str(self.config.get("model", "N/A"))[:1024], inline=False)
            embed.add_field(name="Base URL", value=str(self.config.get("base_url", "N/A"))[:1024], inline=False)
            embed.add_field(name="Latency", value=f"{elapsed_ms} ms", inline=True)
            embed.add_field(name="Error", value=str(exc)[:1024], inline=False)
            await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="toggle")
    async def aiticket_toggle(self, ctx):
        """Enable or disable AI auto-reply."""
        self.config["enabled"] = not self.config.get("enabled", False)
        await self.update_config()
        await ctx.send(f"✅ AI ticket auto-reply is now **{'enabled' if self.config['enabled'] else 'disabled'}**.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setbaseurl")
    async def aiticket_setbaseurl(self, ctx, url: str):
        """Set API base URL (example: https://hermes.ai.unturf.com/v1)."""
        self.config["base_url"] = url.strip().rstrip("/")
        await self.update_config()
        await ctx.send(f"✅ Base URL set to: `{self.config['base_url']}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setapikey")
    async def aiticket_setapikey(self, ctx, *, key: str):
        """Set API key. Use 'clear' to reset to choose-any-value."""
        if key.strip().lower() == "clear":
            self.config["api_key"] = "choose-any-value"
            await self.update_config()
            await ctx.send("✅ API key reset to default placeholder.")
            return

        self.config["api_key"] = key.strip()
        await self.update_config()
        await ctx.send("✅ API key updated.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setmodel")
    async def aiticket_setmodel(self, ctx, *, model: str):
        """Set model id used for completions."""
        self.config["model"] = model.strip()
        await self.update_config()
        await ctx.send(f"✅ Model set to: `{self.config['model']}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="settemperature")
    async def aiticket_settemperature(self, ctx, value: float):
        """Set temperature (0.0 to 2.0)."""
        if value < 0 or value > 2:
            await ctx.send("❌ Temperature must be between 0.0 and 2.0")
            return

        self.config["temperature"] = float(value)
        await self.update_config()
        await ctx.send(f"✅ Temperature set to: `{value}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setmaxtokens")
    async def aiticket_setmaxtokens(self, ctx, value: int):
        """Set max response tokens."""
        if value < 20 or value > 2000:
            await ctx.send("❌ Max tokens must be between 20 and 2000")
            return

        self.config["max_tokens"] = int(value)
        await self.update_config()
        await ctx.send(f"✅ Max tokens set to: `{value}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="sethistory")
    async def aiticket_sethistory(self, ctx, value: int):
        """Set number of recent messages to include (1-20)."""
        if value < 1 or value > 20:
            await ctx.send("❌ History must be between 1 and 20")
            return

        self.config["history_messages"] = int(value)
        await self.update_config()
        await ctx.send(f"✅ History messages set to: `{value}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setretrieval")
    async def aiticket_setretrieval(self, ctx, enabled: bool):
        """Enable or disable retrieval from indexed past tickets."""
        self.config["retrieval_enabled"] = bool(enabled)
        await self.update_config()
        await ctx.send(f"✅ Retrieval is now `{enabled}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setretrievaltopk")
    async def aiticket_setretrievaltopk(self, ctx, value: int):
        """Set number of similar past tickets to inject (1-10)."""
        if value < 1 or value > 10:
            await ctx.send("❌ Retrieval top_k must be between 1 and 10")
            return

        self.config["retrieval_top_k"] = int(value)
        await self.update_config()
        await ctx.send(f"✅ Retrieval top_k set to: `{value}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setretrievalmin")
    async def aiticket_setretrievalmin(self, ctx, value: float):
        """Set minimum similarity score for retrieval context (0.0-1.0)."""
        if value < 0 or value > 1:
            await ctx.send("❌ Retrieval minimum score must be between 0.0 and 1.0")
            return

        self.config["retrieval_min_score"] = float(value)
        await self.update_config()
        await ctx.send(f"✅ Retrieval minimum score set to: `{value}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setdbcollections")
    async def aiticket_setdbcollections(self, ctx, *, names: str):
        """Set DB collection candidates for kbimportdb, comma-separated."""
        parts = [part.strip() for part in names.split(",")]
        cleaned = [part for part in parts if part]
        if not cleaned:
            await ctx.send("❌ Provide at least one collection name.")
            return

        self.config["db_collection_candidates"] = cleaned[:20]
        await self.update_config()
        await ctx.send(f"✅ DB collections set to: {', '.join(self.config['db_collection_candidates'])}")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setdbimportlimit")
    async def aiticket_setdbimportlimit(self, ctx, value: int):
        """Set default limit for kbimportdb (10-100000)."""
        if value < 10 or value > 100000:
            await ctx.send("❌ DB import limit must be between 10 and 100000")
            return

        self.config["db_import_limit"] = int(value)
        await self.update_config()
        await ctx.send(f"✅ DB import limit set to: `{value}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="settimeout")
    async def aiticket_settimeout(self, ctx, value: int):
        """Set API request timeout in seconds (5-120)."""
        if value < 5 or value > 120:
            await ctx.send("❌ Timeout must be between 5 and 120 seconds")
            return

        self.config["request_timeout"] = int(value)
        await self.update_config()
        await ctx.send(f"✅ Request timeout set to: `{value}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setcooldown")
    async def aiticket_setcooldown(self, ctx, value: int):
        """Set minimum seconds between AI replies in a ticket (0-600)."""
        if value < 0 or value > 600:
            await ctx.send("❌ Cooldown must be between 0 and 600 seconds")
            return

        self.config["cooldown_seconds"] = int(value)
        await self.update_config()
        await ctx.send(f"✅ Cooldown set to: `{value}` seconds")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setprompt")
    async def aiticket_setprompt(self, ctx, *, prompt: str):
        """Set system prompt used for AI responses."""
        prompt = prompt.strip()
        if len(prompt) < 5:
            await ctx.send("❌ Prompt is too short.")
            return

        self.config["system_prompt"] = prompt
        await self.update_config()
        await ctx.send("✅ System prompt updated.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="seterrornotice")
    async def aiticket_seterrornotice(self, ctx, enabled: bool):
        """Enable or disable staff-channel error notices."""
        self.config["error_notice_enabled"] = bool(enabled)
        await self.update_config()
        await ctx.send(f"✅ Error notices set to `{enabled}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setescalateonerror")
    async def aiticket_setescalateonerror(self, ctx, enabled: bool):
        """Enable or disable automatic category move when AI fails."""
        self.config["escalate_on_error"] = bool(enabled)
        await self.update_config()
        await ctx.send(f"✅ Escalate on error set to `{enabled}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setthinking")
    async def aiticket_setthinking(self, ctx, *, text: str):
        """Set the temporary status message shown while AI is generating."""
        cleaned = text.strip()
        if not cleaned:
            await ctx.send("❌ Thinking message cannot be empty.")
            return

        self.config["thinking_message"] = cleaned[:150]
        await self.update_config()
        await ctx.send(f"✅ Thinking message set to `{self.config['thinking_message']}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setreplycommand")
    async def aiticket_setreplycommand(self, ctx, *, command_name: str):
        """Set the Modmail command alias used to send outward replies (reply/freply/etc)."""
        cleaned = command_name.strip().lower()
        if not cleaned:
            await ctx.send("❌ Reply command cannot be empty.")
            return

        if " " in cleaned:
            await ctx.send("❌ Reply command must be a single command name.")
            return

        self.config["reply_command"] = cleaned
        await self.update_config()
        await ctx.send(f"✅ Reply command set to `{cleaned}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="seteditcommand")
    async def aiticket_seteditcommand(self, ctx, *, command_name: str):
        """Set the Modmail command alias used to edit outward replies (edit/etc)."""
        cleaned = command_name.strip().lower()
        if not cleaned:
            await ctx.send("❌ Edit command cannot be empty.")
            return

        if " " in cleaned:
            await ctx.send("❌ Edit command must be a single command name.")
            return

        self.config["edit_command"] = cleaned
        await self.update_config()
        await ctx.send(f"✅ Edit command set to `{cleaned}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="addchannel")
    async def aiticket_addchannel(self, ctx, channel: discord.TextChannel):
        """Allow AI replies in one specific ticket channel."""
        current = list(self.config.get("allowed_channel_ids", []))
        current.append(channel.id)
        self.config["allowed_channel_ids"] = self._clean_list_ids(current)
        await self.update_config()
        await ctx.send(f"✅ Added allowed channel: {channel.mention}")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="removechannel")
    async def aiticket_removechannel(self, ctx, channel: discord.TextChannel):
        """Remove an allowed ticket channel."""
        current = [int(x) for x in self.config.get("allowed_channel_ids", []) if int(x) != channel.id]
        self.config["allowed_channel_ids"] = current
        await self.update_config()
        await ctx.send(f"✅ Removed allowed channel: {channel.mention}")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="clearchannels")
    async def aiticket_clearchannels(self, ctx):
        """Clear allowed ticket channel list."""
        self.config["allowed_channel_ids"] = []
        await self.update_config()
        await ctx.send("✅ Cleared all allowed channels.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="addcategory")
    async def aiticket_addcategory(self, ctx, category: discord.CategoryChannel):
        """Allow AI replies for every ticket channel in a category."""
        current = list(self.config.get("allowed_category_ids", []))
        current.append(category.id)
        self.config["allowed_category_ids"] = self._clean_list_ids(current)
        await self.update_config()
        await ctx.send(f"✅ Added allowed category: **{category.name}** (`{category.id}`)")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="removecategory")
    async def aiticket_removecategory(self, ctx, category: discord.CategoryChannel):
        """Remove an allowed category."""
        current = [int(x) for x in self.config.get("allowed_category_ids", []) if int(x) != category.id]
        self.config["allowed_category_ids"] = current
        await self.update_config()
        await ctx.send(f"✅ Removed allowed category: **{category.name}**")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="clearcategories")
    async def aiticket_clearcategories(self, ctx):
        """Clear allowed category list."""
        self.config["allowed_category_ids"] = []
        await self.update_config()
        await ctx.send("✅ Cleared all allowed categories.")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="setescalationcategory")
    async def aiticket_setescalationcategory(self, ctx, *, category_input: str):
        """Set escalation target category. Use 'none' to disable."""
        if category_input.strip().lower() == "none":
            self.config["escalation_category_id"] = None
            await self.update_config()
            await ctx.send("✅ Escalation category cleared.")
            return

        converter = commands.CategoryChannelConverter()
        try:
            category = await converter.convert(ctx, category_input)
        except commands.BadArgument:
            await ctx.send("❌ Could not find that category.")
            return

        self.config["escalation_category_id"] = int(category.id)
        await self.update_config()
        await ctx.send(f"✅ Escalation category set to **{category.name}** (`{category.id}`)")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="addkeyword")
    async def aiticket_addkeyword(self, ctx, *, phrase: str):
        """Add a phrase that triggers human escalation."""
        phrase = phrase.strip().lower()
        if not phrase:
            await ctx.send("❌ Phrase cannot be empty.")
            return

        keywords = [str(x).lower() for x in self.config.get("escalation_keywords", [])]
        if phrase in keywords:
            await ctx.send("ℹ️ That escalation phrase already exists.")
            return

        keywords.append(phrase)
        self.config["escalation_keywords"] = keywords
        await self.update_config()
        await ctx.send(f"✅ Added escalation phrase: `{phrase}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="removekeyword")
    async def aiticket_removekeyword(self, ctx, *, phrase: str):
        """Remove an escalation trigger phrase."""
        phrase = phrase.strip().lower()
        keywords = [str(x).lower() for x in self.config.get("escalation_keywords", [])]

        if phrase not in keywords:
            await ctx.send("❌ That phrase is not in the escalation list.")
            return

        keywords = [k for k in keywords if k != phrase]
        self.config["escalation_keywords"] = keywords
        await self.update_config()
        await ctx.send(f"✅ Removed escalation phrase: `{phrase}`")

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @aiticket_group.command(name="resetkeywords")
    async def aiticket_resetkeywords(self, ctx):
        """Reset escalation keywords to defaults."""
        self.config["escalation_keywords"] = list(self.default_config["escalation_keywords"])
        await self.update_config()
        await ctx.send("✅ Escalation keywords reset to defaults.")


async def setup(bot):
    await bot.add_cog(AITicket(bot))
