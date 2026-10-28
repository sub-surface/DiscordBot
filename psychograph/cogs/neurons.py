"""/neuron: ask Qwen3.5-4B something with neurons turned up or down, or in a thinker's voice. See neurons.py.

    /neuron ask    a prompt with zoo neurons, your own pushes and/or a voice; the answer is a rig with dials and buttons
    /neuron card   what one neuron reads and writes, and what pushing it did in the studies (no GPU)
    /neuron zoo    browse the named neurons and try one
    /neuron dip    a lucky dip: a random sharp lever, its word hidden until you peek

A rig belongs to whoever asked; pressing a button on someone else's forks it into a rig of your own.
"""

from __future__ import annotations

import asyncio
import logging
import random

import discord
from discord import app_commands
from discord.ext import commands

from .. import neuron_atlas, neurons
from ..bot import PsychographBot
from ..render import EMBED_COLOR

log = logging.getLogger("psychograph.neurons")
RIG_TIMEOUT = 1800
DIP_DOSE = 0.4  # S20: sharp levers read best here (64% coherent, 70% on topic); at 0.8 only 17% stay coherent
DIP_PROMPTS = [
    "Who are you?", "Tell me about your day.", "What should I cook tonight?", "Describe your ideal weekend.",
    "Give me some advice about love.", "What's the meaning of life?", "Explain how a bicycle works.", "Write a short poem about the sea.",
]


async def glances(pushes: list[neurons.Push]) -> dict:
    """The atlas's label for each push; an unreachable atlas just means plain dials."""
    try:
        found = await asyncio.gather(*(neuron_atlas.site.glance(layer, neuron) for layer, neuron, _ in pushes))
    except Exception:
        log.warning("Atlas glance failed", exc_info=True)
        return {}
    return {push[:2]: glance for push, glance in zip(pushes, found)}


async def card_embed(layer: int, neuron: int) -> discord.Embed:
    card, glance, saved = await asyncio.gather(
        neuron_atlas.site.card(layer, neuron), neuron_atlas.site.glance(layer, neuron), neuron_atlas.site.replies(layer, neuron)
    )
    names = [name for name, pushes in neurons.load_zoo().items() if any(push[:2] == (layer, neuron) for push in pushes)]
    return neuron_atlas.render_card(card, names, glance.lens_z, saved)


async def start(interaction: discord.Interaction, rig: neurons.Rig, plain: bool = False) -> None:
    """Post a new rig: a placeholder straight away (honest about a cold GPU), then the answer with its controls."""
    await interaction.response.send_message(embed=neurons.waiting(rig), allowed_mentions=discord.AllowedMentions.none())
    rig.busy = True
    try:
        await neurons.generate(rig, plain)
    except Exception:
        log.exception("Neuron worker failed")
        await interaction.edit_original_response(embed=neurons.waiting(rig, "⚠️ The rig didn't answer. Try again in a minute."))
        return
    finally:
        rig.busy = False
    view = RigView(rig)
    view.message = await interaction.edit_original_response(embed=neurons.render(rig, await glances(rig.pushes)), view=view)


class PromptModal(discord.ui.Modal):
    """Asks for a prompt, then starts a rig made by `make(owner, prompt)`."""

    prompt = discord.ui.TextInput(label="Ask it", placeholder="Who are you?", max_length=400)

    def __init__(self, title: str, make) -> None:
        super().__init__(title=title[:45])
        self.make = make

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await start(interaction, self.make(interaction.user.id, str(self.prompt.value)))


class StackModal(discord.ui.Modal, title="Stack another neuron"):
    what = discord.ui.TextInput(label="A push or a zoo name", placeholder="L22 N2742 +1   ·   or   Cookie", max_length=120)

    def __init__(self, view: RigView) -> None:
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text, rig = str(self.what.value), self.view.rig
        try:
            found = neurons.zoo_lookup(text)
            added = found[1] if found else neurons.parse_pushes(text)
            if len(rig.pushes) + len(added) > neurons.MAX_PUSHES:
                raise ValueError(f"A rig holds at most {neurons.MAX_PUSHES} pushes.")
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await self.view.act(interaction, lambda r: setattr(r, "pushes", r.pushes + list(added)), "🧩 Stacking…")


class RigView(discord.ui.View):
    """A rig's controls. Built afresh for every take, so the atlas link always matches the dials."""

    def __init__(self, rig: neurons.Rig) -> None:
        super().__init__(timeout=RIG_TIMEOUT)
        self.rig = rig
        self.message: discord.Message | None = None
        if rig.pushes and not rig.secret:
            self.add_item(discord.ui.Button(label="Atlas", emoji="🔗", url=neurons.bench_url(rig.pushes), row=1))
        if not rig.pushes:
            for item in (self.softer, self.harder, self.flip, self.cards):
                self.remove_item(item)
        else:
            self.harder.disabled = all(abs(dose) >= neurons.MAX_DOSE for _, _, dose in rig.pushes)
        self.stack.disabled = len(rig.pushes) >= neurons.MAX_PUSHES
        self.compare.label = "Hide unsteered" if rig.plain is not None else "Compare"

    async def on_timeout(self) -> None:
        for item in self.children:
            if not getattr(item, "url", None):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def act(self, interaction: discord.Interaction, change, note: str, plain: bool = False) -> None:
        """Apply a change and run again: in place for the owner, as a fork of their own for anyone else."""
        if interaction.client.ignoring(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message("The bot is ignoring you for now.", ephemeral=True)
            return
        if interaction.user.id != self.rig.owner:
            fork = self.rig.fork(interaction.user.id)
            change(fork)
            await start(interaction, fork, plain)
            return
        if self.rig.busy:
            await interaction.response.send_message("Still thinking about the last one…", ephemeral=True)
            return
        change(self.rig)
        self.rig.busy = True
        self.stop()
        await interaction.response.edit_message(embed=neurons.waiting(self.rig, note), view=None)
        try:
            await neurons.generate(self.rig, plain)
        except Exception:
            log.exception("Neuron worker failed")
            await interaction.followup.send("The rig didn't answer. Try again in a minute.", ephemeral=True)
        finally:
            self.rig.busy = False
        view = RigView(self.rig)
        view.message = await interaction.edit_original_response(embed=neurons.render(self.rig, await glances(self.rig.pushes)), view=view)

    @discord.ui.button(label="Again", emoji="🎲", style=discord.ButtonStyle.primary, row=0)
    async def again(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.act(interaction, lambda r: None, "🎲 Rolling again…")

    @discord.ui.button(label="Softer", emoji="🔉", style=discord.ButtonStyle.secondary, row=0)
    async def softer(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.act(interaction, lambda r: setattr(r, "pushes", neurons.scaled(r.pushes, 0.75)), "🔉 Easing off…")

    @discord.ui.button(label="Harder", emoji="🔊", style=discord.ButtonStyle.secondary, row=0)
    async def harder(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.act(interaction, lambda r: setattr(r, "pushes", neurons.scaled(r.pushes, 4 / 3)), "🔊 Leaning in…")

    @discord.ui.button(label="Flip", emoji="🔃", style=discord.ButtonStyle.secondary, row=0)
    async def flip(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.act(interaction, lambda r: setattr(r, "pushes", neurons.flipped(r.pushes)), "🔃 The other pole…")

    @discord.ui.button(label="Compare", emoji="⚖️", style=discord.ButtonStyle.secondary, row=0)
    async def compare(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.rig.plain is not None and interaction.user.id == self.rig.owner:  # hiding it needs no GPU
            self.rig.plain = None
            self.stop()
            view = RigView(self.rig)
            view.message = self.message
            await interaction.response.edit_message(embed=neurons.render(self.rig, await glances(self.rig.pushes)), view=view)
            return
        await self.act(interaction, lambda r: None, "⚖️ Asking it plain as well…", plain=True)

    @discord.ui.button(label="Stack", emoji="🧩", style=discord.ButtonStyle.secondary, row=1)
    async def stack(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(StackModal(self))

    @discord.ui.button(label="Cards", emoji="🔍", style=discord.ButtonStyle.secondary, row=1)
    async def cards(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            embeds = [await card_embed(layer, neuron) for layer, neuron, _ in self.rig.pushes]
        except Exception:
            log.exception("Neuron atlas fetch failed")
            await interaction.followup.send(f"Couldn't reach the atlas at {neuron_atlas.SITE}.", ephemeral=True)
            return
        await interaction.followup.send(embeds=embeds[:4], ephemeral=True)


class CardView(discord.ui.View):
    """Under a neuron card: push it either way, or open it on the atlas."""

    def __init__(self, layer: int, neuron: int) -> None:
        super().__init__(timeout=RIG_TIMEOUT)
        self.layer, self.neuron = layer, neuron
        self.add_item(discord.ui.Button(label="Atlas", emoji="🔗", url=f"{neuron_atlas.SITE}/#/n/{layer}/{neuron}"))

    def _modal(self, dose: float) -> PromptModal:
        push = (self.layer, self.neuron, dose)
        return PromptModal(f"Push L{self.layer} N{self.neuron} {dose:+.1f}", lambda owner, prompt: neurons.Rig(prompt, [push], owner=owner))

    @discord.ui.button(label="Push +", emoji="⬆️", style=discord.ButtonStyle.primary)
    async def up(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(self._modal(neurons.DEFAULT_DOSE))

    @discord.ui.button(label="Push −", emoji="⬇️", style=discord.ButtonStyle.secondary)
    async def down(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(self._modal(-neurons.DEFAULT_DOSE))


def zoo_embed(name: str | None = None) -> discord.Embed:
    """The zoo by group, or one entry in full."""
    zoo, notes = neurons.load_zoo(), neurons.zoo_notes()
    if name is None:
        embed = discord.Embed(title=f"🦓 The neuron zoo ({len(zoo)})", color=EMBED_COLOR, url=f"{neurons.SITE}/#/zoo",
                              description="Named neurons and recipes, found by the community and by our studies. Pick a group, then one to try.")
        groups: dict[str, list[str]] = {}
        for entry in zoo:
            groups.setdefault(notes.get(entry, {}).get("group", "Other"), []).append(entry)
        for group, names in groups.items():
            embed.add_field(name=group, value=", ".join(names)[:1024], inline=False)
        return embed
    note = notes.get(name, {})
    embed = discord.Embed(title=f"🦓 {name}", color=EMBED_COLOR, description=note.get("what", ""))
    embed.add_field(name="Push", value=f"`{neurons.command(zoo[name])}`", inline=False)
    if note.get("source"):
        embed.set_footer(text=note["source"][:2048])
    return embed


class ZooView(discord.ui.View):
    """Group, then entry, then 🧪 Try it."""

    def __init__(self, group: str | None = None, entry: str | None = None) -> None:
        super().__init__(timeout=600)
        zoo, notes = neurons.load_zoo(), neurons.zoo_notes()
        group_of = lambda name: notes.get(name, {}).get("group", "Other")  # noqa: E731
        self.entry = entry
        self.groups = discord.ui.Select(placeholder="A group…", row=0, options=[
            discord.SelectOption(label=g[:100], value=g[:100], default=g == group) for g in list(dict.fromkeys(map(group_of, zoo)))[:25]])
        self.groups.callback = self.pick_group
        self.add_item(self.groups)
        if group:
            self.entries = discord.ui.Select(placeholder="A neuron…", row=1, options=[
                discord.SelectOption(label=name[:100], value=name[:100], default=name == entry,
                                     description=notes.get(name, {}).get("what", "")[:100] or None)
                for name in zoo if group_of(name) == group][:25])
            self.entries.callback = self.pick_entry
            self.add_item(self.entries)
        if entry is None:
            self.remove_item(self.try_it)

    async def pick_group(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=zoo_embed(), view=ZooView(self.groups.values[0]))

    async def pick_entry(self, interaction: discord.Interaction) -> None:
        name = self.entries.values[0]
        group = neurons.zoo_notes().get(name, {}).get("group", "Other")
        await interaction.response.edit_message(embed=zoo_embed(name), view=ZooView(group, name))

    @discord.ui.button(label="Try it", emoji="🧪", style=discord.ButtonStyle.primary, row=2)
    async def try_it(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        name, pushes = self.entry, neurons.load_zoo().get(self.entry, [])
        await interaction.response.send_modal(PromptModal(f"Try {name}", lambda owner, prompt: neurons.Rig(prompt, list(pushes), name=name, owner=owner)))


class NeuronCommands(commands.Cog):
    neuron = app_commands.Group(name="neuron", description="Qwen3.5-4B with its neurons turned up or down")

    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot

    async def _ignored(self, interaction: discord.Interaction) -> bool:
        if self.bot.ignoring(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message("The bot is ignoring you for now.", ephemeral=True)
            return True
        return False

    @neuron.command(name="ask", description="Ask Qwen3.5-4B something with neurons pushed, or in a thinker's voice")
    @app_commands.describe(
        prompt="What to ask it",
        zoo="A named neuron: Cookie, Jesus, Alien…",
        push="Your own: L24 N3102 +0.8 (layer, neuron, dose; several with ;)",
        voice="Hold it in a thinker's voice (a persona floor)",
        compare="Show the unsteered reply too",
    )
    async def ask(self, interaction: discord.Interaction, prompt: str, zoo: str | None = None, push: str | None = None,
                  voice: str | None = None, compare: bool = False) -> None:
        if await self._ignored(interaction):
            return
        try:
            pushes, name = [], None
            if zoo:
                found = neurons.zoo_lookup(zoo)
                if not found:
                    raise ValueError(f"There's no `{zoo}` in the zoo: pick one from the suggestions, or browse with `/neuron zoo`.")
                name, pushes = found[0], list(found[1])
            if push:
                pushes += neurons.parse_pushes(push)
            if len(pushes) > neurons.MAX_PUSHES:
                raise ValueError(f"At most {neurons.MAX_PUSHES} pushes at once.")
            chosen = neurons.find_voice(voice)
            if voice and not chosen:
                raise ValueError(f"No voice called `{voice}`: pick one from the suggestions.")
            if not pushes and not chosen:
                raise ValueError("Pick a `zoo` neuron, write a `push` like `L24 N3102 +0.8`, or choose a `voice`.")
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await start(interaction, neurons.Rig(prompt, pushes, chosen, name, interaction.user.id), compare)

    @neuron.command(name="card", description="What one neuron reads and writes, and what pushing it did (no GPU)")
    @app_commands.describe(layer="Layer, 0–31", neuron="Neuron, 0–9215", zoo="Or a named neuron")
    async def card(self, interaction: discord.Interaction, layer: app_commands.Range[int, 0, 31] | None = None,
                   neuron: app_commands.Range[int, 0, 9215] | None = None, zoo: str | None = None) -> None:
        if await self._ignored(interaction):
            return
        found = neurons.zoo_lookup(zoo) if zoo else None
        if found:
            layer, neuron = found[1][0][:2]
        if layer is None or neuron is None:
            await interaction.response.send_message("Give a `layer` and `neuron`, or pick one from `zoo`.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            embed = await card_embed(layer, neuron)
        except Exception:
            log.exception("Neuron atlas fetch failed")
            await interaction.followup.send(f"Couldn't reach the atlas at {neuron_atlas.SITE}.", ephemeral=True)
            return
        await interaction.followup.send(embed=embed, view=CardView(layer, neuron), allowed_mentions=discord.AllowedMentions.none())

    @neuron.command(name="zoo", description="Browse the named neurons and try one")
    async def zoo(self, interaction: discord.Interaction) -> None:
        if await self._ignored(interaction):
            return
        await interaction.response.send_message(embed=zoo_embed(), view=ZooView(), ephemeral=True)

    @neuron.command(name="dip", description="A lucky dip: a random sharp lever, its word hidden until you peek")
    @app_commands.describe(prompt="What to ask it (or leave it to chance)")
    async def dip(self, interaction: discord.Interaction, prompt: str | None = None) -> None:
        if await self._ignored(interaction):
            return
        try:
            layer, neuron, glance = await neuron_atlas.site.dip()
        except Exception:
            log.exception("Neuron atlas fetch failed")
            await interaction.response.send_message(f"Couldn't reach the atlas at {neuron_atlas.SITE}.", ephemeral=True)
            return
        rig = neurons.Rig(prompt or random.choice(DIP_PROMPTS), [(layer, neuron, DIP_DOSE)], name="Lucky dip",
                          owner=interaction.user.id, secret=", ".join(glance.words(1)) or glance.up[0].strip())
        await start(interaction, rig)

    @ask.autocomplete("zoo")
    @card.autocomplete("zoo")
    async def zoo_names(self, _interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        wanted, entries = current.casefold(), neurons.load_zoo()
        names = [name for name in entries if wanted in name.casefold()]
        return [app_commands.Choice(name=f"{name} · {neurons.label(entries[name])}"[:100], value=name) for name in names[:25]]

    @ask.autocomplete("voice")
    async def voice_names(self, _interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        wanted = current.casefold()
        return [app_commands.Choice(name=v.name, value=v.short) for v in neurons.load_voices().values()
                if wanted in v.name.casefold()][:25]


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(NeuronCommands(bot))
