# Bot Rework Design — 2026-03-15

## Summary

Refactor `process_llm_request` in `bot.py` from a 150-line monolith into focused, single-purpose helpers. Simultaneously fix two UX issues: mention/redirect accuracy and blocky streaming. Consolidate the embed vs plain-text dual path to embed-always for non-webhook responses.

## Problems Being Solved

### 1. Mention/Redirect Failure
When the LLM mentions a user (e.g. `@Leon`), the bot attempts to redirect its embed reply to that user's last message in thread history. This fails silently when:
- The user has no messages in the current DB thread chain
- The user was mentioned in the original prompt but never replied themselves

**Desired behaviour:** thread history → channel history fallback → standalone ping

### 2. Blocky Streaming
The placeholder is only edited every 1.0 second regardless of how many tokens have arrived. With slow local models this can mean long silent gaps. With fast cloud models it wastes the throughput advantage.

**Desired behaviour:** edit every 0.3s OR every 50 accumulated characters, whichever comes first.

### 3. Dual Send Path
`process_llm_request` contains two near-identical send paths — one for embeds, one for plain text — each with their own new-reply-vs-edit branching. This doubles the surface area for bugs.

**Desired behaviour:** always use embeds for non-webhook channels. Remove plain-text path entirely.

---

## Architecture

### `llm.py` — unchanged
`complete()` already yields `(chunk: str | None, meta: dict | None)`. No changes needed.

### New helpers in `bot.py`

#### `stream_to_placeholder(placeholder, gen) -> (str, dict | None)`
Consumes the `complete()` async generator. `placeholder` is always a valid `discord.Message` — the webhook early-exit guarantees this by the time this function is called.

Editing rules (hybrid throttle):
- Edit when `time.time() - last_edit >= 0.3` OR `len(buffer_since_last_edit) >= 50`, whichever comes first.
- Live display strips `<think>` blocks but does NOT perform `@Name → <@ID>` mention substitution. Mention substitution happens post-generation only, so the live view shows raw LLM output (minus thinking).
- A 120-second wall-clock timeout is enforced: if generation exceeds 120s, streaming stops and `"[generation timed out]"` is appended to `full_text`.
- The `max_tokens` cap (1000) is passed through to `llm.complete()` unchanged. If `usage_meta["completion_tokens"] >= 1000`, `"[token limit reached]"` is appended to `full_text`.

Returns `(full_text: str, usage_meta: dict | None)`.

#### `resolve_inline_mentions(cleaned: str, mentions_map: dict) -> (str, list[str])`
Performs a single left-to-right scan of `cleaned`. Matches both `@Name` patterns (from `mentions_map` keys) and bare `<@ID>` tags the LLM wrote directly. All matches are merged into a single positional list by character offset — there is no separate pass per source.

Replaces each `@Name` match with the corresponding `<@ID>` tag in the returned text.

Returns `(substituted_text: str, found_mentions: list[str])` where `found_mentions` is ordered strictly by **first character position of occurrence** in the original text.

#### `resolve_reply_target(found_mentions: list[str], mentions_map: dict, channel, guild) -> discord.Message | None`
Uses the **first element** of `found_mentions` (first `<@ID>` encountered in LLM output, left-to-right) to determine the reply target. Ignores subsequent mentions to avoid ambiguous redirects.

Resolution priority:
1. **Thread history**: look up the user ID in `mentions_map` values for a `last_msg_id` → `channel.fetch_message(last_msg_id)`
2. **Channel history fallback**: if no thread message, scan `channel.history(limit=100)` for the most recent message by that user ID
3. **None**: if neither found, return `None`

If `found_mentions` is empty, returns `None` immediately.

#### `build_response(cleaned, style, thinking, usage_meta, found_mentions) -> (content: str, embed: discord.Embed)`
Single function that produces the two-part Discord message:
- `content`: space-joined `<@ID>` pings from `found_mentions` + thinking spoiler (if any). If `found_mentions` is non-empty, pings are always included in `content` regardless of whether redirect succeeded — ensuring the notification fires even on fallback.
- `embed`: `make_embed(cleaned[:EMBED_DESC_LIMIT], style)` with footer in format `"{persona_footer} | {model_name} | {N} tok | {tps:.1f} t/s"`.

No branching on plain-text vs embed — this function always returns an embed.

#### `send_final(placeholder, reply_to, reply_target, content, embed, view) -> discord.Message`
`reply_to` is the original message the bot was asked to reply to (the prompter). `reply_target` is the resolved redirect target (may be same, different, or None). `view` is a freshly constructed `ResponseView(bot_callback=bot.handle_view_interaction)` — the caller constructs it before calling `send_final`.

Cases:
1. `reply_target` is None OR same message as `reply_to` → `await placeholder.edit(content=content, embed=embed, view=view)`
2. `reply_target` is a different message → `await placeholder.delete()`, then `await reply_target.reply(content=content, embed=embed, view=view)`. If `reply_target.reply` raises (e.g. the target message was deleted), fall back to `await channel.send(content=content, embed=embed, view=view)` — the channel reference is available to `send_final` via closure or parameter.

Returns the final sent/edited `discord.Message` so the caller can use its `.id` for DB logging.

#### `process_llm_request()` — orchestrator (~35 lines)
```
1.  Early-exit for webhook: await send_webhook(...); return
2.  gen = llm.complete(messages, provider, model, cfg, temperature, max_tokens=1000)
3.  placeholder = await (reply_to.reply if reply_to else channel.send)(_THINKING_SIGNAL)
4.  try:
        full_text, meta = await stream_to_placeholder(placeholder, gen)
    except Exception as e:
        await placeholder.edit(content=f"⚠️ Error: {e}")
        return
5.  thinking, cleaned = extract_thinking(full_text)
6.  cleaned, board_image = extract_board(cleaned)
7.  cleaned, found_mentions = resolve_inline_mentions(cleaned, mentions_map)
8.  reply_target = await resolve_reply_target(found_mentions, mentions_map, channel, guild)
9.  style = get_style(persona, load_persona_style(persona))
10. view = ResponseView(bot_callback=bot.handle_view_interaction)
11. content, embed = build_response(cleaned, style, thinking, meta, found_mentions)
12. sent_msg = await send_final(placeholder, reply_to, reply_target, content, embed, view, channel)
13. db.save_message(sent_msg.id, parent_msg_id, channel.id, "assistant", cleaned)
14. if meta: db.log_usage(sent_msg.id, ...)
15. if board_image: await channel.send(file=discord.File(...), reference=sent_msg)
```

Notes:
- DB write (steps 13-14) happens after `send_final` returns the real message ID. No pre-send DB write.
- `board_image` is sent as a reply to `sent_msg` (step 15) so it appears visually threaded below the embed.
- The `channel` reference is passed explicitly to `send_final` for use in the fallback send case.

#### `send_webhook(channel, messages, persona, parent_msg_id, temperature)` (extracted)
Moves the existing webhook/sim-city send path out of `process_llm_request`. Generates fully via `llm.complete()` (no streaming — webhooks don't support edit). Sends via webhook with persona avatar/username. Performs its own `db.save_message()` and `db.log_usage()` internally using `sent_msg.id`. Returns nothing.

---

## Data Flow

```
on_message()
  └─ resolve_mentions() → mentions_map
  └─ process_llm_request(channel, messages, persona, parent_id, reply_to, mentions_map)
        ├─ [webhook] send_webhook() → db writes internally → return
        ├─ gen = llm.complete(...)
        ├─ placeholder = send thinking signal (always non-None here)
        ├─ stream_to_placeholder(placeholder, gen) → (full_text, meta)
        │     └─ hybrid throttle: 0.3s or 50 chars, 120s timeout, 1000 tok cap
        ├─ extract_thinking() → (thinking, cleaned)
        ├─ extract_board() → (cleaned, board_image)
        ├─ resolve_inline_mentions() → (cleaned, found_mentions ordered by position)
        ├─ resolve_reply_target(found_mentions[0]) → reply_target | None
        ├─ build_response() → (content with pings, embed)
        ├─ send_final() → sent_msg
        ├─ db.save_message(sent_msg.id, ...)
        ├─ db.log_usage(sent_msg.id, ...)
        └─ if board_image: channel.send(file=...)
```

---

## What Does NOT Change

- `llm.py` — untouched
- `db.py` — untouched
- `resolve_mentions()` — the pre-generation mention scanner stays as-is
- All slash commands — untouched
- Chess subsystem — untouched
- Heartbeat / sim-city webhook persona — behaviour unchanged, just moved to `send_webhook()`
- `ResponseView` / `OptionsView` — untouched

---

## Future Tracks (out of scope for this rework)

- **Chess single-message game**: edit the board image in-place as moves are made; requires Discord message attachment replacement strategy. Add PGN export command and Lichess analysis URL.
- **Streaming to webhook**: not currently possible with Discord webhooks; would require a different approach (e.g. polling edit via stored webhook message ID).

---

## Success Criteria

1. `@bot please tell @Leon something` — bot's embed reply threads from Leon's last message (thread history preferred, channel history fallback, ping in content regardless)
2. Streaming edits feel smooth — visible progress within 0.3s of first token on fast models, no silent gaps longer than 0.3s on slow models
3. `process_llm_request` is ≤40 lines; each helper has a single clear purpose
4. No plain-text send path in the non-webhook flow
5. All existing commands and sim-city behaviour unchanged
6. DB write uses the real final message ID (not placeholder ID)
