"""Translate existing Discord command/view responses into authenticated web controls.

No command permissions or league rules are reimplemented here. Callbacks receive
the real guild/member and use the same bot, database and services as Discord.
"""
from __future__ import annotations

import io
import secrets
import time
from types import SimpleNamespace

import discord


class WebAttachment:
    def __init__(self, filename, data, content_type):
        self.filename, self.data, self.content_type = filename, data, content_type
        self.size = len(data)
        self.url = ""

    async def read(self):
        return self.data

    async def to_file(self, *, filename=None, **kwargs):
        return discord.File(io.BytesIO(self.data), filename=filename or self.filename)

    async def save(self, target, **kwargs):
        if hasattr(target, "write"):
            return target.write(self.data)
        raise ValueError("This upload must be consumed in memory.")


def inputs(container):
    for child in container.children:
        if isinstance(child, discord.ui.TextInput):
            yield child
        elif isinstance(getattr(child, "component", None), discord.ui.TextInput):
            yield child.component


class WebResponse:
    def __init__(self, interaction):
        self.interaction = interaction
        self.done = False

    def is_done(self):
        return self.done

    async def defer(self, **kwargs):
        self.done = True

    async def send_message(self, content=None, **kwargs):
        self.done = True
        return await self.interaction.capture(content, **kwargs)

    async def edit_message(self, content=None, **kwargs):
        return await self.interaction.capture(content, **kwargs)

    async def send_modal(self, modal):
        self.done = True
        self.interaction.modal = modal


class WebMessage:
    def __init__(self, interaction):
        self.interaction = interaction
        self.id = 0
        self.attachments = []

    async def edit(self, content=None, **kwargs):
        return await self.interaction.capture(content, **kwargs)

    async def delete(self):
        return None


class WebInteraction:
    def __init__(self, bot, guild, member, channel=None):
        self.client, self.guild, self.user = bot, guild, member
        self.guild_id, self.channel = guild.id, channel
        self.channel_id = channel.id if channel else None
        self.namespace = SimpleNamespace()
        self.data = {}
        self.messages, self.files = [], []
        self.view = self.modal = None
        self.response = WebResponse(self)
        self.followup = SimpleNamespace(send=self.capture)
        self.message = WebMessage(self)

    async def capture(self, content=None, **kwargs):
        embeds = kwargs.get("embeds", [])
        if kwargs.get("embed"):
            embeds = [kwargs["embed"]]
        if content or embeds:
            self.messages.append({"content": content or "", "embeds": [e.to_dict() for e in embeds]})
        if "view" in kwargs:
            self.view = kwargs["view"]
        files = kwargs.get("files", [])
        if kwargs.get("file"):
            files = [kwargs["file"]]
        for file in files:
            file.fp.seek(0)
            self.files.append((file.filename, file.fp.read()))
        return self.message

    async def edit_original_response(self, content=None, **kwargs):
        return await self.capture(content, **kwargs)

    async def original_response(self):
        return self.message

    async def delete_original_response(self):
        return None


class WebBridge:
    def __init__(self, bot):
        self.bot = bot
        self.pending = {}
        self.uploads = {}
        self.downloads = {}

    def remember(self, store, owner, value, ttl=900):
        now = time.time()
        for key in list(store):
            if store[key][0] < now:
                del store[key]
        if len(store) >= 500:
            raise ValueError("Too many pending controls. Try again shortly.")
        key = secrets.token_urlsafe(24)
        store[key] = (now + ttl, owner, value)
        return key

    def lookup(self, store, key, owner, *, consume=False):
        item = store.get(key)
        if not item or item[0] < time.time() or item[1] != owner:
            raise ValueError("This control expired. Reload the page and try again.")
        if consume:
            del store[key]
        return item[2]

    def serialize(self, interaction):
        owner = (interaction.guild_id, interaction.user.id)
        result = {"messages": interaction.messages, "controls": [], "files": []}
        if interaction.modal:
            token = self.remember(self.pending, owner, ("modal", interaction.modal, interaction.channel, getattr(interaction, 'web_admin', False)))
            result["modal"] = {"token": token, "title": interaction.modal.title, "fields": [
                {"id": f.custom_id, "label": f.label or next((getattr(c, "text", "") for c in interaction.modal.children if getattr(c, "component", None) is f), "Value"),
                 "required": f.required, "value": f.default or "", "maxLength": f.max_length or 4000,
                 "multiline": f.style == discord.TextStyle.paragraph}
                for f in inputs(interaction.modal)
            ]}
        if interaction.view:
            for child in interaction.view.children:
                item = getattr(child, "item", child)
                if isinstance(item, discord.ui.Button) and item.url:
                    result["controls"].append({"label": item.label or "Open", "url": item.url})
                    continue
                token = self.remember(self.pending, owner, ("view", (interaction.view, child), interaction.channel, getattr(interaction, 'web_admin', False)))
                control = {"token": token, "label": getattr(item, "label", None) or getattr(item, "placeholder", None) or "Continue",
                           "disabled": getattr(item, "disabled", False)}
                if isinstance(item, discord.ui.Select):
                    control["options"] = [{"label": o.label, "value": o.value} for o in item.options]
                result["controls"].append(control)
        for name, data in interaction.files:
            token = self.remember(self.downloads, owner, (name, data), ttl=300)
            result["files"].append({"token": token, "name": name})
        return result

    async def respond(self, token, interaction, values=None):
        owner = (interaction.guild_id, interaction.user.id)
        kind, target, channel, requires_admin = self.lookup(self.pending, token, owner, consume=True)
        if requires_admin and not getattr(interaction, 'web_admin', False):
            raise ValueError("Commissioner access was removed. Reload the league.")
        interaction.channel = channel
        interaction.channel_id = channel.id if channel else None
        values = values or {}
        if kind == "modal":
            for field in inputs(target):
                value = str(values.get(field.custom_id, ""))
                if (field.required and not value.strip()) or len(value) > (field.max_length or 4000):
                    raise ValueError("Complete the required fields within their length limits.")
                field._value = value
            await target.on_submit(interaction)
        else:
            view, child = target
            item = getattr(child, "item", child)
            if getattr(item, "disabled", False):
                raise ValueError("This action is no longer available.")
            if not await view.interaction_check(interaction):
                return self.serialize(interaction)
            if isinstance(item, discord.ui.Select):
                selected = str(values.get("value", ""))
                if selected not in {o.value for o in item.options}:
                    raise ValueError("Choose one of the available options.")
                item._values = [selected]
                interaction.data = {"values": [selected]}
            await child.callback(interaction)
        return self.serialize(interaction)

    def command_schema(self, command):
        return {"name": command.name, "description": command.description, "parameters": [
            {"name": p.name, "description": str(p.description), "type": p.type.name,
             "required": p.required, "choices": [{"label": c.name, "value": c.value} for c in p.choices],
             "min": p.min_value, "max": p.max_value}
            for p in command.parameters
        ]}

    async def command(self, name, values, interaction):
        command = self.bot.tree.get_command(name)
        if not isinstance(command, discord.app_commands.Command):
            raise ValueError("Unknown command.")
        kwargs = {}
        for name, p in command._params.items():
            value = values.get(p.name)
            if value is None or value == "":
                if p.required:
                    raise ValueError(f"{p.name} is required.")
                continue
            if p.type == discord.AppCommandOptionType.boolean:
                if not isinstance(value, bool):
                    raise ValueError(f"{p.name} must be true or false.")
            elif p.type in (discord.AppCommandOptionType.integer, discord.AppCommandOptionType.number):
                value = int(value) if p.type == discord.AppCommandOptionType.integer else float(value)
                if p.min_value is not None and value < p.min_value or p.max_value is not None and value > p.max_value:
                    raise ValueError(f"{p.name} is outside its allowed range.")
            elif p.type == discord.AppCommandOptionType.user:
                value = await interaction.guild.fetch_member(int(value))
            elif p.type == discord.AppCommandOptionType.role:
                value = interaction.guild.get_role(int(value))
                if not value:
                    raise ValueError("Role not found in this server.")
            elif p.type == discord.AppCommandOptionType.channel:
                value = interaction.guild.get_channel(int(value))
                if not isinstance(value, discord.TextChannel):
                    raise ValueError("Choose a text channel in this server.")
            elif p.type == discord.AppCommandOptionType.attachment:
                value = self.lookup(self.uploads, str(value), (interaction.guild_id, interaction.user.id))
            else:
                value = str(value)
                if len(value) > (p.max_value or 6000):
                    raise ValueError("Value is too long.")
            if p.choices:
                choices = [c for c in p.choices if c.value == value]
                if not choices:
                    raise ValueError(f"Choose an allowed {p.name}.")
                # Choice annotations need a Choice; Literal annotations need the value.
                if isinstance(p._annotation, discord.app_commands.transformers.ChoiceTransformer):
                    value = choices[0]
            kwargs[name] = value
        interaction.namespace = SimpleNamespace(**kwargs)
        await command.callback(interaction, **kwargs)
        return self.serialize(interaction)
