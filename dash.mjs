#!/usr/bin/env node
// dash.mjs — Psychograph Discord Bot dashboard
import { readdirSync, readFileSync, openSync, existsSync, closeSync } from "fs"
import { execSync, spawnSync, spawn } from "child_process"
import { join, dirname } from "path"
import { fileURLToPath } from "url"
import { createConnection } from "net"
import { createSocket } from "dgram"
import { createInterface } from "readline"

if (!process.stdin.isTTY) { console.error("dash.mjs needs an interactive terminal"); process.exit(1) }
const ROOT = dirname(fileURLToPath(import.meta.url))
const PERSONAS_DIR = join(ROOT, "personas")
const LOG_PATH = join(ROOT, "bot.log")

// ── ANSI ──────────────────────────────────────────────────────────────
const g = "\x1b[92m", y = "\x1b[33m", c = "\x1b[36m", red = "\x1b[31m"
const bo = "\x1b[1m", d = "\x1b[2m", _ = "\x1b[0m", m = "\x1b[35m"
const vLen = (s) => s.replace(/\x1b\[[0-9;]*m/g, "").length
const rpad = (s, w) => s + " ".repeat(Math.max(0, w - vLen(s)))

// ── The Glorp ──────────────────────────────────────────────────────────
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
const FRAMES_THINKING = [
  ["  ░ ░  ", " (· ·) ", "  ▒▒▒  "],
  ["  ▒ ▒  ", " (~ ~) ", "  ░░░  "],
  ["  ░ ▒  ", " (o o) ", "  ▒░▒  "],
  ["  ▒ ░  ", " (· ·) ", "  ░▒░  "],
  ["  ░ ░  ", " (~ ~) ", "  ▒▒▒  "],
]
const FRAMES_HAPPY = [
  ["  ✦ ✦  ", " (^ ^) ", "  ■■■  "],
  ["  ★ ✦  ", " (* *) ", "  □■□  "],
  ["  ✦ ★  ", " (O O) ", "  ■□■  "],
  ["  ★ ★  ", " (^ ^) ", "  ■■■  "],
]
const FRAMES_BUSY = [
  ["  ⚙ ⚙  ", " (> <) ", "  ⚙ ⚙  "],
  ["  ⚙ ⚙  ", " (< >) ", "  ⚙ ⚙  "],
  ["  ⚙ ⚙  ", " (> <) ", "  ⚙ ⚙  "],
  ["  ◌ ◌  ", " (→ ←) ", "  ◌ ◌  "],
]

const QUIPS = [
  "the daemon watches...", "monitoring channels", "context window open", "streaming tokens...",
  "history.db has stories", "waiting for a mention", "inference engine idle", "temperature: 0.7",
  "signals received", "24 masks, one process", "mochi is dreaming", "philoclanker meditates",
  "the ledger balances", "cassandra knows already", "chess.py awaits your move", "vostok reads the static",
  "the coroner is ready", "sigint ghost on station", "all models are wrong", "some are useful",
  "the void answers back", "tokens are cheap", "context is everything", "the prompt is the thought",
  "running on inference", "attention is all you need", "every message a vector",
]

// ── State ─────────────────────────────────────────────────────────────
let botUp = false, lmUp = false, frame = 0
let quipIdx = Math.floor(Math.random() * QUIPS.length)
let msg = "", lastError = "", paused = false, animState = "idle"

// ── Helpers ───────────────────────────────────────────────────────────
function countPersonas() { try { return readdirSync(PERSONAS_DIR).filter(f => f.endsWith(".md")).length } catch { return 0 } }
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
      "import sqlite3", "db = sqlite3.connect('history.db')", "c = db.cursor()",
      "msgs  = c.execute('SELECT COUNT(*) FROM messages').fetchone()[0]",
      "chans = c.execute('SELECT COUNT(DISTINCT channel_id) FROM messages').fetchone()[0]",
      "pins  = c.execute('SELECT COUNT(*) FROM pins').fetchone()[0]",
      "print(str(msgs) + ',' + str(chans) + ',' + str(pins))",
    ].join("; ")
    const pyPath = join(ROOT, "venv", "Scripts", "python.exe")
    const res = spawnSync(pyPath, ["-c", py], { cwd: ROOT, encoding: "utf8", timeout: 5000 })
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
function checkBotPort() {
  return new Promise((resolve) => {
    const res = spawnSync("powershell", [
      "-Command",
      "Get-WmiObject Win32_Process -Filter 'name=\"python.exe\"' | Where-Object { $_.CommandLine -like '*bot.py*' } | Measure-Object | Select-Object -ExpandProperty Count"
    ], { encoding: "utf8", timeout: 3000 })
    const count = parseInt((res.stdout || "").trim(), 10)
    resolve(!isNaN(count) && count > 0)
  })
}

let cfg = readConfig(), db = { msgs: "…", chans: "…", pins: "…" }, git = getGit(), personas = countPersonas()

function updateLastError() {
  if (!existsSync(LOG_PATH)) return
  try {
    const content = readFileSync(LOG_PATH, "utf8").trim()
    const lines = content.split("\n").filter(l => l.trim())
    if (lines.length > 0) {
      for (let i = lines.length - 1; i >= 0; i--) {
        const l = lines[i]
        // Ignore routine INFO and common WARNING logs
        if (l.includes(" INFO ") || l.includes(" WARNING ")) continue;
        
        if (l.includes("Error:") || l.includes("Exception:") || l.match(/^[A-Za-z]+Error:/)) {
          lastError = l.trim().slice(0, 48); return
        }
      }
      // If we're here, no explicit error found. 
      // If bot is offline, the last line might be interesting, otherwise clear it.
      if (!botUp && lines.length > 0) {
        const last = lines[lines.length-1]
        if (!last.includes(" INFO ")) {
          lastError = last.trim().slice(0, 48)
          return
        }
      }
      lastError = ""
    } else { lastError = "" }
  } catch { lastError = "" }
}

function clearLog() {
  try {
    const fd = openSync(LOG_PATH, "w"); closeSync(fd)
    lastError = ""; msg = `${g}▸ log cleared${_}`
  } catch (e) { msg = `${red}▸ clear failed: ${e.message}${_}` }
}

// ── Render ────────────────────────────────────────────────────────────
function render() {
  const W = 52, hr = "─".repeat(W), row = (s) => `│ ${rpad(s, W - 2)} │`
  let frameSet = animState === "thinking" ? FRAMES_THINKING : animState === "happy" ? FRAMES_HAPPY : animState === "busy" ? FRAMES_BUSY : FRAMES_IDLE
  const f = frameSet[frame % frameSet.length], q = QUIPS[quipIdx]
  const stateLabel = `${d}[${animState}]${_}`
  const titleLine  = `${bo}PSYCHOGRAPH BOT${_}  ·  dashboard${d}${"-".repeat(Math.max(0, W - 37 - vLen(animState) - 2))}${stateLabel}`
  const providerStr = cfg.provider === "local" ? `${g}local${_}` : `${c}openrouter${_}`
  const modelShort  = cfg.model.length > 22 ? cfg.model.slice(0, 21) + "…" : cfg.model

  const out = [
    `╭${hr}╮`, row(titleLine), `├${hr}┤`, row(""),
    row(`${g}${f[0]}${_}  ${d}"${q}"${_}`), row(`${g}${f[1]}${_}`), row(`${g}${f[2]}${_}`), row(""),
    `├${hr}┤`,
    row(`bot: ${botUp ? `${g}● running${_}` : `${red}○ offline${_}`}   lm-studio: ${lmUp ? `${g}● :1234${_}` : `${d}○ offline${_}`}`),
    row(`provider: ${providerStr}  ·  model: ${y}${modelShort}${_}`),
    row(`persona: ${m}${cfg.persona}${_}  ·  personas loaded: ${c}${personas}${_}`),
    `├${hr}┤`,
    row(`msgs: ${c}${db.msgs}${_}  ·  channels: ${c}${db.chans}${_}  ·  pins: ${c}${db.pins}${_}`),
    row(`branch: ${y}${git.branch}${_}  ·  ${git.clean ? `${g}clean${_}` : `${red}dirty${_}`}`),
    `├${hr}┤`, row(`${bo}err: ${_}${lastError ? `${red}${lastError}${_}` : `${d}none${_}`}`), `├${hr}┤`,
    row(`[${bo}s${_}] start    [${bo}k${_}] kill     [${bo}r${_}] restart`),
    row(`[${bo}p${_}] personas  [${bo}d${_}] db       [${bo}g${_}] git st`),
    row(`[${bo}c${_}] commit    [${bo}l${_}] log      [${bo}x${_}] clear err`),
    row(`${d}[q] quit${_}`),
    `╰${hr}╯`,
  ]
  if (msg) out.push("", ` ${msg}`)
  process.stdout.write("\x1b[H\x1b[2J" + out.join("\n") + "\n")
}

async function refresh() {
  ;[botUp, lmUp] = await Promise.all([checkBotPort(), checkPort(1234)])
  cfg = readConfig(); git = getGit(); personas = countPersonas(); updateLastError()
  db = getDbStats()
  if (botUp) animState = lmUp ? "happy" : "thinking"; else if (lmUp) animState = "thinking"; else animState = "idle"
  render()
}

function startBot() {
  if (botUp) { msg = `${y}▸ bot already running${_}`; return }
  animState = "busy"
  try {
    const logFd = openSync(LOG_PATH, "a")
    spawn("venv/Scripts/python.exe", ["bot.py"], { cwd: ROOT, detached: true, stdio: ["ignore", logFd, logFd] }).unref()
    msg = `${g}▸ spawning bot process (logging to bot.log)...${_}`
  } catch (e) { msg = `${red}▸ failed to start: ${e.message}${_}` }
  setTimeout(refresh, 3000)
}

async function killBot() {
  if (!botUp) { msg = `${d}▸ bot not running${_}`; return }
  animState = "busy"; msg = `${d}▸ terminating...${_}`; render()
  spawnSync("powershell", ["-Command", "Get-WmiObject Win32_Process -Filter 'name=\"python.exe\"' | Where-Object { $_.CommandLine -like '*bot.py*' } | ForEach-Object { $_.Terminate() }"], { encoding: "utf8", timeout: 6000 })
  // Wait briefly for processes to die, then verify
  await new Promise(r => setTimeout(r, 800))
  botUp = await checkBotPort()
  animState = "idle"; msg = botUp ? `${red}▸ kill may have failed — check manually${_}` : `${red}▸ bot terminated${_}`
}

async function shell(cmd, label) {
  paused = true; animState = "busy"; process.stdout.write("\x1b[?1049l"); process.stdin.setRawMode(false)
  console.log(`\n${g}▸ ${label}${_}\n`)
  try { execSync(cmd, { cwd: ROOT, stdio: "inherit" }) } catch { console.log(`\n${red}exited with error${_}`) }
  console.log(`\n${d}press any key to return...${_}`)
  await new Promise((r) => { process.stdin.setRawMode(true); process.stdin.once("data", r) })
  process.stdout.write("\x1b[?1049h"); paused = false; await refresh()
}

async function showLog() {
  paused = true; process.stdout.write("\x1b[?1049l"); process.stdin.setRawMode(false)
  console.log(`\n${g}▸ bot.log (last 20 lines)${_}\n`)
  if (existsSync(LOG_PATH)) {
    try { const lines = readFileSync(LOG_PATH, "utf8").trim().split("\n").slice(-20); lines.forEach(l => console.log(`  ${l}`)) }
    catch { console.log(`${red}could not read log file${_}`) }
  } else { console.log(`${d}no log file found${_}`) }
  console.log(`\n${d}press any key to return...${_}`)
  await new Promise((r) => { process.stdin.setRawMode(true); process.stdin.once("data", r) })
  process.stdout.write("\x1b[?1049h"); paused = false; render()
}

async function commit() {
  paused = true; animState = "busy"; process.stdout.write("\x1b[?1049l"); process.stdin.setRawMode(false)
  console.log(`\n${g}▸ ✦ committing...${_}\n`); try { execSync("git status --short", { cwd: ROOT, stdio: "inherit" }) } catch {}
  console.log(""); const rl = createInterface({ input: process.stdin, output: process.stdout })
  const message = await new Promise((resolve) => { rl.question(`${y}commit message (empty to cancel): ${_}`, (ans) => { rl.close(); resolve(ans.trim()) }) })
  if (!message) console.log(`\n${d}cancelled${_}`)
  else {
    try { execSync("git add .", { cwd: ROOT, stdio: "inherit" }); spawnSync("git", ["commit", "-m", message], { cwd: ROOT, stdio: "inherit" }); execSync("git push", { cwd: ROOT, stdio: "inherit" }); console.log(`\n${g}▸ pushed${_}`) }
    catch { console.log(`\n${red}failed${_}`) }
  }
  console.log(`\n${d}press any key to return...${_}`)
  await new Promise((r) => { process.stdin.setRawMode(true); process.stdin.resume(); process.stdin.once("data", r) })
  process.stdout.write("\x1b[?1049h"); paused = false; await refresh()
}

process.stdout.write("\x1b[?1049h"); process.stdin.setRawMode(true); process.stdin.resume(); await refresh()
setInterval(() => { if (!paused) { frame = (frame + 1) % 8; if (frame === 0) quipIdx = Math.floor(Math.random() * QUIPS.length); render() } }, 1000)
setInterval(async () => { if (!paused) await refresh() }, 8000)

process.stdin.on("data", async (key) => {
  const k = key.toString()
  if (k === "q" || k === "\x03") { process.stdout.write("\x1b[?1049l"); process.stdin.setRawMode(false); process.exit(0) }
  if (paused) return
  msg = ""
  switch (k) {
    case "s": startBot(); render(); break
    case "k": await killBot(); render(); break
    case "r": await killBot(); setTimeout(startBot, 1500); break
    case "p": await shell("dir /b personas", "personas..."); break
    case "d": await shell("python -c \"import sqlite3; db=sqlite3.connect('history.db'); c=db.cursor(); print('msgs:', c.execute('SELECT COUNT(*) FROM messages').fetchone()[0])\"", "db stats..."); break
    case "g": await shell("git status", "git status..."); break
    case "c": await commit(); break
    case "l": await showLog(); break
    case "x": clearLog(); render(); break
  }
})
