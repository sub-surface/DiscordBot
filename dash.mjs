#!/usr/bin/env node
// dash.mjs — Psychograph Discord Bot dashboard
import { readdirSync, readFileSync } from "fs"
import { execSync, spawnSync, spawn } from "child_process"
import { join, dirname } from "path"
import { fileURLToPath } from "url"
import { createConnection } from "net"
import { createSocket } from "dgram"
import { createInterface } from "readline"

if (!process.stdin.isTTY) { console.error("dash.mjs needs an interactive terminal"); process.exit(1) }
const ROOT = dirname(fileURLToPath(import.meta.url))
const PERSONAS_DIR = join(ROOT, "personas")

// ── ANSI ──────────────────────────────────────────────────────────────
const g = "\x1b[92m", y = "\x1b[33m", c = "\x1b[36m", red = "\x1b[31m"
const bo = "\x1b[1m", d = "\x1b[2m", _ = "\x1b[0m", m = "\x1b[35m"
const vLen = (s) => s.replace(/\x1b\[[0-9;]*m/g, "").length
const rpad = (s, w) => s + " ".repeat(Math.max(0, w - vLen(s)))

// ── The Glorp ──────────────────────────────────────────────────────────
// idle: daemon at rest, watching the void
const FRAMES_IDLE = [
  ["  ■ □  ", " (· ·) ", "  ─── "],
  ["  □ ■  ", " (o o) ", "  ─── "],
  ["  ■ ■  ", " (· ·) ", "  ─── "],
  ["  □ □  ", " (- -) ", "  ─── "],
  ["  ■ □  ", " (o o) ", "  ─── "],
  ["  □ ■  ", " (· ·) ", "  ─── "],
  ["  ■ □  ", " (~ ~) ", "  ─── "],
  ["  □ □  ", " (o o) ", "  ─── "],
]

// thinking: something's off, processing
const FRAMES_THINKING = [
  ["  ░ ░  ", " (· ·) ", "  ▒▒▒  "],
  ["  ▒ ▒  ", " (~ ~) ", "  ░░░  "],
  ["  ░ ▒  ", " (o o) ", "  ▒░▒  "],
  ["  ▒ ░  ", " (· ·) ", "  ░▒░  "],
  ["  ░ ░  ", " (~ ~) ", "  ▒▒▒  "],
]

// happy: bot running, all systems up
const FRAMES_HAPPY = [
  ["  ✦ ✦  ", " (^ ^) ", "  ■■■  "],
  ["  ★ ✦  ", " (* *) ", "  □■□  "],
  ["  ✦ ★  ", " (O O) ", "  ■□■  "],
  ["  ★ ★  ", " (^ ^) ", "  ■■■  "],
]

// busy: executing
const FRAMES_BUSY = [
  ["  ⚙ ⚙  ", " (> <) ", "  ⚙ ⚙  "],
  ["  ⚙ ⚙  ", " (< >) ", "  ⚙ ⚙  "],
  ["  ⚙ ⚙  ", " (> <) ", "  ⚙ ⚙  "],
  ["  ◌ ◌  ", " (→ ←) ", "  ◌ ◌  "],
]

const QUIPS = [
  // operational
  "the daemon watches...",
  "monitoring channels",
  "context window open",
  "streaming tokens...",
  "history.db has stories",
  "waiting for a mention",
  "inference engine idle",
  "temperature: 0.7",
  "signals received",
  // persona-flavored
  "24 masks, one process",
  "mochi is dreaming",
  "philoclanker meditates",
  "the ledger balances",
  "cassandra knows already",
  "chess.py awaits your move",
  "vostok reads the static",
  "the coroner is ready",
  "sigint ghost on station",
  // technical/poetic
  "all models are wrong",
  "some are useful",
  "the void answers back",
  "tokens are cheap",
  "context is everything",
  "the prompt is the thought",
  "running on inference",
  "attention is all you need",
  "every message a vector",
]

// ── State ─────────────────────────────────────────────────────────────
let botUp = false
let lmUp = false
let frame = 0
let quipIdx = Math.floor(Math.random() * QUIPS.length)
let msg = ""
let paused = false
let animState = "idle"

// ── Helpers ───────────────────────────────────────────────────────────
function countPersonas() {
  try { return readdirSync(PERSONAS_DIR).filter(f => f.endsWith(".md")).length }
  catch { return 0 }
}

function readConfig() {
  try {
    const raw = readFileSync(join(ROOT, "config.yaml"), "utf8")
    const provider = (raw.match(/^default_provider:\s*(.+)$/m) || [])[1]?.trim() || "?"
    const model    = (raw.match(/^default_model:\s*(.+)$/m)    || [])[1]?.trim() || "?"
    const persona  = (raw.match(/^persona:\s*(.+)$/m)          || [])[1]?.trim() || "?"
    return { provider, model, persona }
  } catch { return { provider: "?", model: "?", persona: "?" } }
}

function getDbStats() {
  try {
    const py = [
      "import sqlite3",
      "db = sqlite3.connect('history.db')",
      "c = db.cursor()",
      "msgs  = c.execute('SELECT COUNT(*) FROM messages').fetchone()[0]",
      "chans = c.execute('SELECT COUNT(DISTINCT channel_id) FROM messages').fetchone()[0]",
      "pins  = c.execute('SELECT COUNT(*) FROM pins').fetchone()[0]",
      "print(str(msgs) + ',' + str(chans) + ',' + str(pins))",
    ].join("; ")
    const res = spawnSync("venv/Scripts/python.exe", ["-c", py], { cwd: ROOT, encoding: "utf8", timeout: 5000 })
    if (res.status !== 0 || !res.stdout.trim()) return { msgs: "?", chans: "?", pins: "?" }
    const [msgs, chans, pins] = res.stdout.trim().split(",")
    return { msgs, chans, pins }
  } catch { return { msgs: "?", chans: "?", pins: "?" } }
}

function getGit() {
  let branch = "?", clean = true
  try {
    branch = execSync("git branch --show-current", { cwd: ROOT, encoding: "utf8" }).trim()
    clean  = execSync("git status --porcelain",    { cwd: ROOT, encoding: "utf8" }).trim() === ""
  } catch {}
  return { branch, clean }
}

function checkPort(port) {
  return new Promise((ok) => {
    const s = createConnection({ port, host: "127.0.0.1" })
    s.on("connect", () => { s.destroy(); ok(true) })
    s.on("error",   () => ok(false))
    setTimeout(() => { s.destroy(); ok(false) }, 300)
  })
}

// Check if bot is running by attempting to bind the same UDP singleton port.
// If bind fails (EADDRINUSE) → bot owns the port → bot is up.
function checkBotPort() {
  return new Promise((resolve) => {
    const sock = createSocket("udp4")
    let done = false
    const finish = (v) => { if (!done) { done = true; resolve(v) } }
    sock.on("error",     (e) => { finish(e.code === "EADDRINUSE") })
    sock.on("listening", ()  => { sock.close(); finish(false) })
    sock.bind(47823, "127.0.0.1")
    setTimeout(() => { try { sock.close() } catch {} finish(false) }, 800)
  })
}

// ── Stats ─────────────────────────────────────────────────────────────
let cfg      = readConfig()
let db       = { msgs: "…", chans: "…", pins: "…" }
let git      = getGit()
let personas = countPersonas()

// ── Render ────────────────────────────────────────────────────────────
function render() {
  const W = 52, hr = "─".repeat(W)
  const row = (s) => `│ ${rpad(s, W - 2)} │`

  let frameSet = FRAMES_IDLE
  if (animState === "thinking") frameSet = FRAMES_THINKING
  else if (animState === "happy") frameSet = FRAMES_HAPPY
  else if (animState === "busy") frameSet = FRAMES_BUSY

  const f = frameSet[frame % frameSet.length]
  const q = QUIPS[quipIdx]

  const stateLabel = `${d}[${animState}]${_}`
  const titleLine  = `${bo}PSYCHOGRAPH BOT${_}  ·  dashboard${d}${"-".repeat(Math.max(0, W - 37 - vLen(animState) - 2))}${stateLabel}`

  const providerStr = cfg.provider === "local" ? `${g}local${_}` : `${c}openrouter${_}`
  const modelShort  = cfg.model.length > 22 ? cfg.model.slice(0, 21) + "…" : cfg.model

  const out = [
    `╭${hr}╮`,
    row(titleLine),
    `├${hr}┤`,
    row(""),
    row(`${g}${f[0]}${_}  ${d}"${q}"${_}`),
    row(`${g}${f[1]}${_}`),
    row(`${g}${f[2]}${_}`),
    row(""),
    `├${hr}┤`,
    row(`bot: ${botUp ? `${g}● running${_}` : `${red}○ offline${_}`}   lm-studio: ${lmUp ? `${g}● :1234${_}` : `${d}○ offline${_}`}`),
    row(`provider: ${providerStr}  ·  model: ${y}${modelShort}${_}`),
    row(`persona: ${m}${cfg.persona}${_}  ·  personas loaded: ${c}${personas}${_}`),
    `├${hr}┤`,
    row(`msgs: ${c}${db.msgs}${_}  ·  channels: ${c}${db.chans}${_}  ·  pins: ${c}${db.pins}${_}`),
    row(`branch: ${y}${git.branch}${_}  ·  ${git.clean ? `${g}clean${_}` : `${red}dirty${_}`}`),
    `├${hr}┤`,
    row(`[${bo}s${_}] start    [${bo}k${_}] kill     [${bo}r${_}] restart`),
    row(`[${bo}p${_}] personas  [${bo}d${_}] db       [${bo}g${_}] git st`),
    row(`[${bo}c${_}] commit    ${d}[q] quit${_}`),
    `╰${hr}╯`,
  ]
  if (msg) out.push("", ` ${msg}`)
  process.stdout.write("\x1b[H\x1b[2J" + out.join("\n") + "\n")
}

// ── Refresh ───────────────────────────────────────────────────────────
async function refresh() {
  ;[botUp, lmUp] = await Promise.all([checkBotPort(), checkPort(1234)])
  cfg      = readConfig()
  git      = getGit()
  personas = countPersonas()

  if (botUp)       animState = lmUp ? "happy" : "thinking"
  else if (lmUp)   animState = "thinking"
  else             animState = "idle"
  frame = 0
  render()
}

async function refreshDb() {
  db = getDbStats()
  render()
}

// ── Commands ──────────────────────────────────────────────────────────
async function shell(cmd, label) {
  paused = true
  animState = "busy"
  process.stdout.write("\x1b[?1049l")
  process.stdin.setRawMode(false)
  console.log(`\n${g}▸ ${label}${_}\n`)
  try { execSync(cmd, { cwd: ROOT, stdio: "inherit" }) }
  catch { console.log(`\n${red}exited with error${_}`) }
  console.log(`\n${d}press any key to return...${_}`)
  await new Promise((r) => { process.stdin.setRawMode(true); process.stdin.once("data", r) })
  process.stdout.write("\x1b[?1049h")
  paused = false
  await refresh()
}

async function commit() {
  paused = true
  animState = "busy"
  process.stdout.write("\x1b[?1049l")
  process.stdin.setRawMode(false)
  console.log(`\n${g}▸ ✦ committing...${_}\n`)
  try { execSync("git status --short", { cwd: ROOT, stdio: "inherit" }) } catch {}
  console.log("")
  const rl = createInterface({ input: process.stdin, output: process.stdout })
  const message = await new Promise((resolve) => {
    rl.question(`${y}commit message (empty to cancel): ${_}`, (ans) => { rl.close(); resolve(ans.trim()) })
  })
  if (!message) {
    console.log(`\n${d}cancelled${_}`)
  } else {
    try {
      execSync("git add .", { cwd: ROOT, stdio: "inherit" })
      spawnSync("git", ["commit", "-m", message], { cwd: ROOT, stdio: "inherit" })
      execSync("git push", { cwd: ROOT, stdio: "inherit" })
      console.log(`\n${g}▸ pushed${_}`)
    } catch { console.log(`\n${red}failed${_}`) }
  }
  console.log(`\n${d}press any key to return...${_}`)
  await new Promise((r) => { process.stdin.setRawMode(true); process.stdin.resume(); process.stdin.once("data", r) })
  process.stdout.write("\x1b[?1049h")
  paused = false
  await refresh()
}

function startBot() {
  if (botUp) { msg = `${y}▸ bot already running${_}`; return }
  animState = "busy"
  spawn("venv/Scripts/python.exe", ["bot.py"], { cwd: ROOT, detached: true, stdio: "ignore" }).unref()
  msg = `${g}▸ spawning bot process...${_}`
  setTimeout(refresh, 3000)
}

async function killBot() {
  if (!botUp) { msg = `${d}▸ bot not running${_}`; return }
  animState = "busy"
  msg = `${d}▸ terminating...${_}`
  render()
  spawnSync(
    "powershell",
    ["-Command", "Get-WmiObject Win32_Process -Filter 'name=\"python.exe\"' | Where-Object { $_.CommandLine -like '*bot.py*' } | ForEach-Object { $_.Terminate() }"],
    { encoding: "utf8", timeout: 6000 }
  )
  botUp = false
  animState = "idle"
  msg = `${red}▸ bot terminated${_}`
}

async function restartBot() {
  await killBot()
  render()
  await new Promise((r) => setTimeout(r, 1500))
  startBot()
}

async function showPersonas() {
  paused = true
  process.stdout.write("\x1b[?1049l")
  process.stdin.setRawMode(false)
  console.log(`\n${g}▸ personas (${personas})${_}\n`)
  try {
    const files = readdirSync(PERSONAS_DIR).filter(f => f.endsWith(".md")).sort()
    files.forEach(f => console.log(`  ${c}·${_} ${f.replace(".md", "")}`))
  } catch { console.log(`${red}could not read personas directory${_}`) }
  console.log(`\n${d}press any key to return...${_}`)
  await new Promise((r) => { process.stdin.setRawMode(true); process.stdin.once("data", r) })
  process.stdout.write("\x1b[?1049h")
  paused = false
  render()
}

async function showDb() {
  paused = true
  process.stdout.write("\x1b[?1049l")
  process.stdin.setRawMode(false)
  console.log(`\n${g}▸ database (history.db)${_}\n`)
  const fresh = getDbStats()
  db = fresh
  console.log(`  ${c}messages   ${_}${fresh.msgs}`)
  console.log(`  ${c}channels   ${_}${fresh.chans}`)
  console.log(`  ${c}pins       ${_}${fresh.pins}`)
  console.log(`\n${d}press any key to return...${_}`)
  await new Promise((r) => { process.stdin.setRawMode(true); process.stdin.once("data", r) })
  process.stdout.write("\x1b[?1049h")
  paused = false
  render()
}

// ── Main ──────────────────────────────────────────────────────────────
process.stdout.write("\x1b[?1049h")
process.stdin.setRawMode(true)
process.stdin.resume()
await refresh()
refreshDb() // load DB stats without blocking initial render

// Animate the glorp
const tick = setInterval(() => {
  if (paused) return
  const fs = animState === "thinking" ? FRAMES_THINKING
    : animState === "happy" ? FRAMES_HAPPY
    : animState === "busy"  ? FRAMES_BUSY
    : FRAMES_IDLE
  frame = (frame + 1) % fs.length
  if (frame === 0) quipIdx = Math.floor(Math.random() * QUIPS.length)
  render()
}, 1000)

// Periodic light refresh: ports + config (every 8s)
setInterval(async () => {
  if (paused) return
  const [nb, nl] = await Promise.all([checkBotPort(), checkPort(1234)])
  if (nb !== botUp || nl !== lmUp) {
    botUp = nb; lmUp = nl
    cfg = readConfig()
    const prev = animState
    if (botUp)     animState = lmUp ? "happy" : "thinking"
    else if (lmUp) animState = "thinking"
    else           animState = "idle"
    if (animState !== prev) frame = 0
    render()
  }
}, 8000)

// Periodic heavy refresh: DB stats (every 30s)
setInterval(() => {
  if (paused) return
  db = getDbStats()
  render()
}, 30000)

// Keypress handler
process.stdin.on("data", async (key) => {
  const k = key.toString()
  if (k === "q" || k === "\x03") {
    clearInterval(tick)
    process.stdout.write("\x1b[?1049l")
    process.stdin.setRawMode(false)
    process.exit(0)
  }
  if (paused) return
  msg = ""
  switch (k) {
    case "s": startBot(); render(); break
    case "k": await killBot(); render(); break
    case "r": await restartBot(); render(); break
    case "p": await showPersonas(); break
    case "d": await showDb(); break
    case "g": await shell("git status", "checking the ledger..."); break
    case "c": await commit(); break
  }
})
