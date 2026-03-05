import discord
import db
import chess_engine
from personas import list_personas, load_persona, load_persona_style
from styles import get_style, VERBOSITY_LABELS
from config_util import config, save_config

def _options_embed(channel_id: int, bot) -> discord.Embed:
    persona = db.get_channel_persona(channel_id) or config.get("persona", "mochi")
    verb = db.get_channel_verbosity(channel_id)
    temp = db.get_channel_temperature(channel_id) or config.get("response", {}).get("temperature", 0.7)
    style = get_style(persona, load_persona_style(persona))
    color = style["color"] if style else 0x2B2D31
    
    embed = discord.Embed(title="⚙️ command center", color=color)
    embed.add_field(name="👤 persona", value=f"`{persona}`", inline=True)
    embed.add_field(name="📡 provider", value=f"`{bot.current_provider}`", inline=True)
    embed.add_field(name="🧠 model", value=f"`{bot.current_model.split('/')[-1]}`", inline=True)
    
    embed.add_field(name="💬 verbosity", value=f"**{verb}/5** — *{VERBOSITY_LABELS.get(verb, '')}*", inline=False)
    embed.add_field(name="🔥 temperature", value=f"**{temp:.1f}**", inline=True)
    
    embed.set_footer(text="· slash commands still work for quick adjustments ·")
    return embed

class ResponseView(discord.ui.View):
    def __init__(self, bot_callback=None):
        super().__init__(timeout=None)
        self.bot_callback = bot_callback

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
        view = await _get_options_view(interaction.channel_id, bot)
        await interaction.response.send_message(embed=_options_embed(interaction.channel_id, bot), view=view, ephemeral=True)

async def _get_options_view(channel_id, bot):
    import llm
    view = OptionsView(channel_id)
    if bot.current_provider == "local":
        models = await llm.get_local_models(config)
    else:
        models = config["providers"].get(bot.current_provider, {}).get("models", [])
    
    if models:
        view.add_item(ModelSelect(channel_id, bot.current_provider, bot.current_model, models))
    return view

class PersonaSelect(discord.ui.Select):
    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        names = list_personas()[:25]
        current = db.get_channel_persona(channel_id) or "mochi"
        options = [discord.SelectOption(label=n, value=n, default=(n == current)) for n in names]
        super().__init__(placeholder="switch persona…", options=options, row=0)

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
            discord.SelectOption(label=m.split('/')[-1][:100], value=m, default=(m == current_model))
            for m in models[:25]
        ]
        super().__init__(placeholder=f"select {provider} model…", options=options, row=1)

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
        self.add_item(PersonaSelect(channel_id))
        
        active_v = db.get_channel_verbosity(channel_id)
        for val in range(1, 6):
            btn = discord.ui.Button(label=f"V{val}", row=2, 
                                    style=discord.ButtonStyle.success if val == active_v else discord.ButtonStyle.secondary)
            btn.callback = self._make_verb_callback(val)
            self.add_item(btn)

        active_t = db.get_channel_temperature(channel_id) or 0.7
        for val in [0.1, 0.5, 0.7, 1.0, 1.2]:
            btn = discord.ui.Button(label=f"T{val}", row=3,
                                    style=discord.ButtonStyle.success if abs(val - active_t) < 0.01 else discord.ButtonStyle.secondary)
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

    @discord.ui.button(label="📝 summarize", style=discord.ButtonStyle.primary, row=4)
    async def summarize_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        from bot import handle_summarize
        summary = await handle_summarize(interaction.channel_id)
        await interaction.followup.send(f"**Context Summary:**\n{summary}", ephemeral=True)
