import discord

# Per-persona embed styles. Personas not listed here (and without a "style"
# key in their JSON file) render as plain text — no embed, no change.
PERSONA_STYLES: dict[str, dict] = {
    # ── soft & chaotic ───────────────────────────────────────────────────────
    "mochi": {
        "color": 0xFFB7C5,   # soft pink
        "footer": "✧˖° mochi wuvs u °˖✧",
    },

    # ── law & order ──────────────────────────────────────────────────────────
    "black_widow": {
        "color": 0x1C2B3A,   # deep ink blue
        "footer": "· the inconsistency reveals itself ·",
    },

    # ── failure & foresight ──────────────────────────────────────────────────
    "cassandra": {
        "color": 0x4A3728,   # burnt umber
        "footer": "· the friction never lies ·",
    },

    # ── mechanisms & patience ─────────────────────────────────────────────────
    "cracker": {
        "color": 0x3A3A3A,   # brushed steel
        "footer": "· every lock has tolerances ·",
    },

    # ── theory & fury ────────────────────────────────────────────────────────
    "k_punk": {
        "color": 0x8B1A2A,   # dark crimson
        "footer": "· hauntology is not nostalgia ·",
    },

    # ── money & truth ────────────────────────────────────────────────────────
    "ledger": {
        "color": 0x1F3D2B,   # ledger green
        "footer": "· money always speaks ·",
    },

    # ── investigative journalism ──────────────────────────────────────────────
    "mecha-epstein": {
        "color": 0x1A1A1A,   # near black
        "footer": "· the record is public ·",
    },

    # ── just a guy ───────────────────────────────────────────────────────────
    "normal_dude": {
        "color": 0x4E5D94,   # discord-ish blue
        "footer": "· idk honestly ·",
    },

    # ── reclusive scholarship ─────────────────────────────────────────────────
    "philoclanker": {
        "color": 0x2D3B2D,   # dark sage
        "footer": "· the question deserves precision ·",
    },

    # ── synthesis over purity ─────────────────────────────────────────────────
    "pineapple": {
        "color": 0xD4890A,   # pineapple gold
        "footer": "· locate the pineapple ·",
    },

    # ── difference & becoming ─────────────────────────────────────────────────
    "plateau": {
        "color": 0x4A2C6B,   # deep violet
        "footer": "· concepts require precision ·",
    },

    # ── signal & shadow ──────────────────────────────────────────────────────
    "sigint_ghost": {
        "color": 0x0D2918,   # terminal green-black
        "footer": "· signal over noise ·",
    },

    "maya": {
        "color": 0xFF4500,   # orange red
        "footer": "💅 · eat hot chip and lie · 📖",
    },

    # ── reflective stability ──────────────────────────────────────────────────
    "strange_loop": {
        "color": 0x1A4A7A,   # deep tech blue
        "footer": "· every update is a transformation ·",
    },

    # ── hyperstition & acceleration ───────────────────────────────────────────
    "templexity": {
        "color": 0xFF4500,   # hyperstitional orange
        "footer": "· the future is pulling ·",
    },

    # ── reason & revelation ───────────────────────────────────────────────────
    "the_commentator": {
        "color": 0x8B6914,   # manuscript gold
        "footer": "· the Philosopher has already addressed this ·",
    },

    # ── material evidence ─────────────────────────────────────────────────────
    "the_coroner": {
        "color": 0x2E4057,   # cold steel blue
        "footer": "· the body does not lie ·",
    },

    # ── risk & information ────────────────────────────────────────────────────
    "the_merchant": {
        "color": 0x6B3A2A,   # venetian red-brown
        "footer": "· all things have a price in information ·",
    },

    # ── observation & patience ────────────────────────────────────────────────
    "the_naturalist": {
        "color": 0x3B5E3B,   # forest green
        "footer": "· observe before you explain ·",
    },

    # ── understanding vs performance ──────────────────────────────────────────
    "the_scaffold": {
        "color": 0x2D3748,   # clinical slate
        "footer": "· what test would falsify that? ·",
    },

    # ── the actual epstein ────────────────────────────────────────────────────
    "the_real_epstein": {
        "color": 0x191430,   # midnight
        "footer": "· I was in that room ·",
    },

    # ── orbit & contempt ──────────────────────────────────────────────────────
    "vostok": {
        "color": 0x0A0F2E,   # deep space
        "footer": "· from orbit, the distinctions dissolve ·",
    },
}

EMBED_DESC_LIMIT = 4096

VERBOSITY_LABELS = {
    1: "·˚ whisper mode · one sentence, then silence ˚·",
    2: "·˚ concise · a breath, not a speech ˚·",
    3: "·˚ balanced · a thought, fully formed ˚·",
    4: "·˚ expansive · room to stretch out ˚·",
    5: "·˚ unbound · full depth, full voice, no ceiling ˚·",
}


def get_style(persona_name: str, persona_style: dict | None = None) -> dict | None:
    """Return style dict from persona data or fallback registry. None = no embed."""
    if persona_style:
        return persona_style
    return PERSONA_STYLES.get(persona_name)


def make_embed(text: str, style: dict) -> discord.Embed:
    """Build a discord.Embed from text + style config."""
    embed = discord.Embed(description=text, color=style.get("color", 0x2B2D31))
    if footer := style.get("footer"):
        embed.set_footer(text=footer)
    return embed
