# Bot cheat sheet

Mention the bot to chat. Chain commands with `;`:
```
@bot persona mochi; verbosity 3; reset
```

## Personas
`@bot personas` — list all personas (active one marked)
`@bot persona <name>` — switch persona + clear history
`@bot prompt` — show current persona's full system prompt

## Context
`@bot reset` — clear this channel's conversation history

## Model / Provider
`@bot model` — list models for current provider
`@bot model <name>` — switch model
`@bot model free` — list all free-tier models on OpenRouter
`@bot model random` — switch to a random paid model
`@bot model free random` — switch to a random free model
`@bot provider local` — switch to LM Studio
`@bot provider openrouter` — switch to OpenRouter

## Output
`@bot verbosity <1-5>` — how long responses are (default: 2)

## Settings Panel
`@bot options` — interactive settings panel with:
- persona switcher dropdown
- verbosity buttons (active shown in green)
- reset context button

## Buttons (on every response)
`↺ regenerate` — re-run the response with higher temperature
`📌 pin` — pin the message as a persistent note for this channel

## Reactions (also still work)
🔄 on a bot message — regenerate that response
📌 on a bot message — pin it as a persistent note

## Chess
`@bot resign` — resign the current game
`@bot level <1-8>` — set chess-classic difficulty

## Meta
`@bot restart` — restart the bot

The bot has web search — it decides when to use it.
