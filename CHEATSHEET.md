# Bot cheat sheet

Mention the bot to chat, or use these commands:

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

## Reactions
🔄 on a bot message — regenerate that response
📌 on a bot message — pin it as a persistent note for this channel

## Meta
`@bot restart` — restart the bot
`@bot <cmd>; <cmd>` — chain commands with semicolons

The bot has web search — it decides when to use it.
