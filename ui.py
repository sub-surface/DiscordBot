import discord
import db
import chess_engine
from personas import list_personas, load_persona, load_persona_style
from styles import get_style, VERBOSITY_LABELS
from config_util import config, save_config

# ── Options embed ─────────────────────────────────────────────────────────────

def _options_embed(channel_id: int, bot) -> discord.Embed:
    persona = db.get_channel_persona(channel_id) or config.get("persona", "mochi")
    verb = db.get_channel_verbosity(channel_id)
    temp = db.get_channel_temperature(channel_id) or config.get("response", {}).get("temperature", 0.7)
    style = get_style(persona, load_persona_style(persona)) or {}
    color = style.get("color", 0x2B2D31)

    model_short = bot.current_model.split("/")[-1]
    # Truncate long model names gracefully
    if len(model_short) > 28:
        model_short = model_short[:26] + "…"

    verb_label = VERBOSITY_LABELS.get(verb, "")
    temp_bar = "░▒▓"[min(2, int(temp / 0.5))] * 3  # rough heat visual

    embed = discord.Embed(color=color)
    embed.add_field(name="persona", value=f"`{persona}`", inline=True)
    embed.add_field(name="provider · model", value=f"`{bot.current_provider}` · `{model_short}`", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)  # spacer
    embed.add_field(name="verbosity", value=f"**{verb}**/5 · *{verb_label}*", inline=True)
    embed.add_field(name="temperature", value=f"**{temp:.1f}** {temp_bar}", inline=True)
    return embed


# ── Sim-city embed ────────────────────────────────────────────────────────────

def _simcity_embed(channel_id: int, bot) -> discord.Embed:
    topic = db.get_sim_setting("topic") or "*none set*"
    interval = db.get_sim_setting("heartbeat_interval") or "11"
    whitelist = db.get_sim_personas()
    personas_str = ", ".join(whitelist) if whitelist else "*all*"
    queue_count = len(db.list_queue())

    embed = discord.Embed(title="sim-city", color=0x1a1a2e)
    embed.add_field(name="scene", value=topic[:200], inline=False)
    embed.add_field(name="heartbeat", value=f"every **{interval}h**", inline=True)
    embed.add_field(name="queue", value=f"**{queue_count}** pending", inline=True)
    embed.add_field(name="personas", value=personas_str[:200], inline=False)
    return embed


# ── Response view (persistent) ────────────────────────────────────────────────

class ResponseView(discord.ui.View):
    def __init__(self, bot_callback=None, has_thinking: bool = False):
        super().__init__(timeout=None)
        self.bot_callback = bot_callback
        if has_thinking:
            btn = discord.ui.Button(
                label="💭",
                style=discord.ButtonStyle.secondary,
                custom_id="psychograph:thinking",
            )
            btn.callback = self._thinking_callback
            self.add_item(btn)

    async def _thinking_callback(self, interaction: discord.Interaction):
        thinking = db.get_thinking(interaction.message.id)
        if not thinking:
            await interaction.response.send_message("-# *· no reasoning trace stored ·*", ephemeral=True)
            return
        chunks = [thinking[i:i+1990] for i in range(0, len(thinking), 1990)]
        await interaction.response.send_message(f"💭 **reasoning trace**\n{chunks[0]}", ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    @discord.ui.button(label="↺", style=discord.ButtonStyle.secondary, custom_id="psychograph:regen")
    async def regen(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.bot_callback:
            await self.bot_callback(interaction, "regen")

    @discord.ui.button(label="📌", style=discord.ButtonStyle.secondary, custom_id="psychograph:pin")
    async def pin(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg = interaction.message
        content = msg.embeds[0].description if msg.embeds else msg.content
        if content:
            db.add_pin(interaction.channel_id, content[:200])
            await interaction.response.send_message("-# *· pinned ·*", ephemeral=True)
        else:
            await interaction.response.send_message("-# *· nothing to pin ·*", ephemeral=True)

    @discord.ui.button(label="🗑️", style=discord.ButtonStyle.secondary, custom_id="psychograph:reset")
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        db.clear_channel(interaction.channel_id)
        persona = db.get_channel_persona(interaction.channel_id) or "mochi"
        if chess_engine.is_any_chess_persona(persona):
            chess_engine.reset_game(interaction.channel_id)
        await interaction.response.send_message("-# *·˚ slate wiped ˚·*", ephemeral=True)

    @discord.ui.button(label="⚙️", style=discord.ButtonStyle.secondary, custom_id="psychograph:settings")
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = interaction.client
        is_sim_city = getattr(interaction.channel, "name", None) == "sim-city"
        if is_sim_city:
            view = SimCityOptionsView(interaction.channel_id, bot)
            await interaction.response.send_message(embed=_simcity_embed(interaction.channel_id, bot), view=view, ephemeral=True)
        else:
            view = await _get_options_view(interaction.channel_id, bot)
            actions = OptionsActionsView(interaction.channel_id)
            await interaction.response.send_message(embed=_options_embed(interaction.channel_id, bot), view=view, ephemeral=True)
            await interaction.followup.send(view=actions, ephemeral=True)


# ── Options view helpers ──────────────────────────────────────────────────────

async def _get_options_view(channel_id, bot):
    import llm
    view = OptionsView(channel_id)
    providers = list(config.get("providers", {}).keys())
    if len(providers) > 1:
        view.add_item(ProviderSelect(channel_id, bot.current_provider, providers))
    if bot.current_provider == "local":
        models = await llm.get_local_models(config)
    else:
        models = config["providers"].get(bot.current_provider, {}).get("models", [])
    if models:
        view.add_item(ModelSelect(channel_id, bot.current_provider, bot.current_model, models))
    return view


class ProviderSelect(discord.ui.Select):
    def __init__(self, channel_id: int, current_provider: str, providers: list[str]):
        self.channel_id = channel_id
        options = [
            discord.SelectOption(label=p, value=p, default=(p == current_provider))
            for p in providers[:25]
        ]
        super().__init__(placeholder="provider…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        import llm as _llm
        bot = interaction.client
        name = self.values[0]
        bot.current_provider = name
        config["default_provider"] = name
        if name == "local":
            models = await _llm.get_local_models(config)
            bot.current_model = models[0] if models else "local-model"
        else:
            models = config["providers"][name].get("models", [])
            bot.current_model = models[0] if models else "unknown"
        config["default_model"] = bot.current_model
        save_config(config)
        view = await _get_options_view(interaction.channel_id, bot)
        await interaction.response.edit_message(embed=_options_embed(interaction.channel_id, bot), view=view)


class PersonaSelect(discord.ui.Select):
    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        names = list_personas()[:25]
        current = db.get_channel_persona(channel_id) or "mochi"
        options = [discord.SelectOption(label=n, value=n, default=(n == current)) for n in names]
        super().__init__(placeholder="persona…", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        name = self.values[0]
        db.set_channel_persona(interaction.channel_id, name)
        db.clear_channel(interaction.channel_id)
        if chess_engine.is_any_chess_persona(name):
            chess_engine.reset_game(interaction.channel_id)
        bot = interaction.client
        view = await _get_options_view(interaction.channel_id, bot)
        await interaction.response.edit_message(embed=_options_embed(interaction.channel_id, bot), view=view)


class ModelSelect(discord.ui.Select):
    def __init__(self, channel_id: int, provider: str, current_model: str, models: list[str]):
        self.channel_id = channel_id
        options = [
            discord.SelectOption(label=m.split("/")[-1][:100], value=m, default=(m == current_model))
            for m in models[:25]
        ]
        super().__init__(placeholder="model…", options=options, row=2)

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        name = self.values[0]
        bot.current_model = name
        config["default_model"] = name
        save_config(config)
        view = await _get_options_view(interaction.channel_id, bot)
        await interaction.response.edit_message(embed=_options_embed(interaction.channel_id, bot), view=view)


class OptionsView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=120)
        self.channel_id = channel_id
        # Row 0: provider (added dynamically by _get_options_view if >1 provider)
        # Row 1: persona (added here)
        # Row 2: model (added dynamically)
        self.add_item(PersonaSelect(channel_id))

        # Row 3: verbosity 1-5
        active_v = db.get_channel_verbosity(channel_id)
        for val in range(1, 6):
            btn = discord.ui.Button(
                label=str(val),
                row=3,
                style=discord.ButtonStyle.success if val == active_v else discord.ButtonStyle.secondary,
            )
            btn.callback = self._make_verb_callback(val)
            self.add_item(btn)

        # Row 3 also: temperature presets (5 buttons fills row 3 exactly with verbosity)
        # Actually we have 5 verb + 5 temp = need 2 rows. Use row 3 for verb, row 4 for temp+actions.
        active_t = db.get_channel_temperature(channel_id) or 0.7
        for val, label in [(0.1, "0.1"), (0.5, "0.5"), (0.7, "0.7"), (1.0, "1.0"), (1.2, "1.2")]:
            btn = discord.ui.Button(
                label=label,
                row=4,
                style=discord.ButtonStyle.success if abs(val - active_t) < 0.01 else discord.ButtonStyle.secondary,
            )
            btn.callback = self._make_temp_callback(val)
            self.add_item(btn)

    def _make_verb_callback(self, val):
        async def callback(interaction):
            db.set_channel_verbosity(interaction.channel_id, val)
            bot = interaction.client
            view = await _get_options_view(interaction.channel_id, bot)
            await interaction.response.edit_message(embed=_options_embed(interaction.channel_id, bot), view=view)
        return callback

    def _make_temp_callback(self, val):
        async def callback(interaction):
            db.set_channel_temperature(interaction.channel_id, val)
            bot = interaction.client
            view = await _get_options_view(interaction.channel_id, bot)
            await interaction.response.edit_message(embed=_options_embed(interaction.channel_id, bot), view=view)
        return callback


class OptionsActionsView(discord.ui.View):
    """Second panel of the options menu — utility actions row."""
    def __init__(self, channel_id: int):
        super().__init__(timeout=120)
        self.channel_id = channel_id

    @discord.ui.button(label="📝 context", style=discord.ButtonStyle.secondary, row=0)
    async def summarize_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        from bot import handle_summarize
        summary = await handle_summarize(interaction.channel_id)
        await interaction.followup.send(f"**context summary**\n{summary}", ephemeral=True)

    @discord.ui.button(label="📌 pins", style=discord.ButtonStyle.secondary, row=0)
    async def pins_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        pins = db.get_pins(interaction.channel_id)
        if not pins:
            await interaction.response.send_message("-# *· no pins yet ·*", ephemeral=True)
        else:
            lines = "\n".join(f"· {p}" for p in pins)
            await interaction.response.send_message(f"**pinned notes**\n{lines}", ephemeral=True)

    @discord.ui.button(label="💬 prompt", style=discord.ButtonStyle.secondary, row=0)
    async def prompt_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        from bot import get_system_prompt, ch_persona
        persona = ch_persona(interaction.channel_id)
        text = get_system_prompt(persona, interaction.channel_id)
        await interaction.response.send_message(f"**active prompt** · `{persona}`\n\n{text}"[:2000], ephemeral=True)

    @discord.ui.button(label="✏️ edit persona", style=discord.ButtonStyle.secondary, row=0)
    async def edit_persona_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        from pathlib import Path
        from bot import ch_persona
        persona = ch_persona(interaction.channel_id)
        path = Path("personas") / f"{persona}.md"
        if not path.exists():
            await interaction.response.send_message(f"· persona file not found ·", ephemeral=True)
            return
        current = path.read_text(encoding="utf-8")
        modal = PersonaEditModal(name=persona, current_content=current[:4000])
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🗑 wipe", style=discord.ButtonStyle.danger, row=0)
    async def wipe_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        db.clear_channel(interaction.channel_id)
        persona = db.get_channel_persona(interaction.channel_id) or "mochi"
        if chess_engine.is_any_chess_persona(persona):
            chess_engine.reset_game(interaction.channel_id)
        await interaction.response.send_message("-# *·˚ slate wiped ˚·*", ephemeral=True)


# ── Sim-city options view ─────────────────────────────────────────────────────

class SimCityOptionsView(discord.ui.View):
    def __init__(self, channel_id: int, bot):
        super().__init__(timeout=120)
        self.channel_id = channel_id
        self.bot = bot

        # Persona whitelist select (row 0) — toggle personas in/out
        whitelist = db.get_sim_personas() or []
        all_personas = [p for p in list_personas() if not chess_engine.is_any_chess_persona(p)][:25]
        options = [
            discord.SelectOption(label=p, value=p, default=(p in whitelist))
            for p in all_personas
        ]
        select = discord.ui.Select(
            placeholder="persona whitelist (select to include)…",
            options=options,
            min_values=0,
            max_values=len(options),
            row=0,
        )
        select.callback = self._whitelist_callback
        self.add_item(select)

        # Heartbeat interval buttons (row 1)
        current_interval = db.get_sim_setting("heartbeat_interval") or "11"
        for hours, label in [("4", "4h"), ("8", "8h"), ("11", "11h"), ("24", "24h"), ("48", "48h")]:
            btn = discord.ui.Button(
                label=label,
                row=1,
                style=discord.ButtonStyle.success if current_interval == hours else discord.ButtonStyle.secondary,
            )
            btn.callback = self._make_interval_callback(hours)
            self.add_item(btn)

    async def _whitelist_callback(self, interaction: discord.Interaction):
        selected = interaction.data["values"]
        db.set_sim_personas(selected if selected else None)
        await interaction.response.edit_message(embed=_simcity_embed(self.channel_id, self.bot), view=SimCityOptionsView(self.channel_id, self.bot))

    def _make_interval_callback(self, hours: str):
        async def callback(interaction: discord.Interaction):
            db.set_sim_setting("heartbeat_interval", hours)
            await interaction.response.edit_message(embed=_simcity_embed(self.channel_id, self.bot), view=SimCityOptionsView(self.channel_id, self.bot))
        return callback

    @discord.ui.button(label="✦ trigger", style=discord.ButtonStyle.primary, row=2)
    async def trigger_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        from bot import process_llm_request, get_system_prompt
        import random
        db.set_last_run("sim_city_heartbeat", 0)
        whitelist = db.get_sim_personas()
        pool = [p for p in (whitelist if whitelist else list_personas())
                if not chess_engine.is_any_chess_persona(p)]
        persona_name = random.choice(pool)
        topics = ["the weather", "a random thought", "something you noticed today", "a dream you had", "a piece of news"]
        prompt = f"Write a short, characterful post about {random.choice(topics)}."
        sim_topic = db.get_sim_setting("topic")
        system = get_system_prompt(persona_name, self.channel_id, sim_city_topic=sim_topic)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        channel = interaction.channel
        await process_llm_request(channel, messages, persona_name, None)
        await interaction.followup.send(f"· triggered **{persona_name}** ·", ephemeral=True)

    @discord.ui.button(label="📋 queue", style=discord.ButtonStyle.secondary, row=2)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = db.list_queue()
        if not items:
            await interaction.response.send_message("· queue is empty ·", ephemeral=True)
            return
        lines = [f"{i+1}. **{it['from_persona']}** → **{it['to_persona']}**: {it['seed_prompt'][:60]}" for i, it in enumerate(items)]
        await interaction.response.send_message("· **conversation queue** ·\n" + "\n".join(lines), ephemeral=True)

    @discord.ui.button(label="✕ clear queue", style=discord.ButtonStyle.danger, row=2)
    async def clear_queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        db.clear_queue()
        await interaction.response.edit_message(embed=_simcity_embed(self.channel_id, self.bot), view=SimCityOptionsView(self.channel_id, self.bot))

    @discord.ui.button(label="✕ clear scene", style=discord.ButtonStyle.secondary, row=3)
    async def clear_scene_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        db.set_sim_setting("topic", None)
        await interaction.response.edit_message(embed=_simcity_embed(self.channel_id, self.bot), view=SimCityOptionsView(self.channel_id, self.bot))

    @discord.ui.button(label="🗑 wipe history", style=discord.ButtonStyle.danger, row=3)
    async def wipe_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        db.clear_channel(self.channel_id)
        await interaction.response.edit_message(embed=_simcity_embed(self.channel_id, self.bot), view=SimCityOptionsView(self.channel_id, self.bot))


# ── Persona edit modal ────────────────────────────────────────────────────────

class PersonaEditModal(discord.ui.Modal, title="Edit Persona"):
    def __init__(self, name: str, current_content: str):
        super().__init__()
        self.persona_name = name
        self.content_input = discord.ui.TextInput(
            label=f"Persona: {name}",
            style=discord.TextStyle.paragraph,
            default=current_content,
            max_length=4000,
            required=True,
        )
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        from pathlib import Path
        path = Path("personas") / f"{self.persona_name}.md"
        try:
            path.write_text(self.content_input.value, encoding="utf-8")
            await interaction.response.send_message(
                f"· **{self.persona_name}** updated ·", ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(f"⚠️ save failed: {e}", ephemeral=True)
