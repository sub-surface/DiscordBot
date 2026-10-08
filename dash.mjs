#!/usr/bin/env node
import { appendFileSync, existsSync, readFileSync, writeFileSync } from "node:fs"
import { spawn, spawnSync } from "node:child_process"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { createInterface } from "node:readline/promises"
import process from "node:process"

const root = dirname(fileURLToPath(import.meta.url))
if (!process.stdin.isTTY) {
  console.error("dash.mjs needs an interactive terminal")
  process.exit(1)
}

const python = existsSync(join(root, "venv", "Scripts", "python.exe"))
  ? join(root, "venv", "Scripts", "python.exe")
  : process.platform === "win32" ? "py" : "python3"
const pythonPrefix = python === "py" ? ["-3"] : []
const pythonArgs = [...pythonPrefix, "-m", "psychograph"]
const modal = existsSync(join(root, "venv", "Scripts", "modal.exe"))
  ? join(root, "venv", "Scripts", "modal.exe")
  : "modal"
const monthlyBudget = Number(readEnv("MODAL_MONTHLY_BUDGET", "30")) || 30
const deployedPath = join(root, ".modal-deployed.json")
// What a deploy bakes into the worker; a difference from .env means the running worker is out of date.
const deployKeys = ["MODAL_MODEL_ID", "MODAL_MODEL_FILE", "MODAL_GPU", "MODAL_MAX_MODEL_LEN", "MODAL_SCALEDOWN_SECONDS", "MODAL_ENABLE_THINKING", "MODAL_GPU_SNAPSHOT"]
const gpus = { T4: "16 GB, cheapest", L4: "24 GB", A10G: "24 GB, faster prefill" }
const modelStorageGiB = 5
// Presets live in models.json, shared with the bot (chat tuning) and modal_app.py (deploy settings).
const modalModelPresets = JSON.parse(readFileSync(join(root, "models.json"), "utf8")).models
  .filter((model) => model.modal)
  .map((model) => ({
    name: model.name,
    id: model.modal.id,
    file: model.modal.file,
    enableThinking: String(Boolean(model.modal.thinking)),
    context: String(model.modal.context ?? 32768),
    storageGiB: model.modal.storage_gib ?? modelStorageGiB,
    contextMode: model.chat?.context_mode ?? "full",
  }))
const mint = "\x1b[38;5;121m"
const reset = "\x1b[0m"

function readEnv(name, fallback = "") {
  try {
    const line = readFileSync(join(root, ".env"), "utf8")
      .split(/\r?\n/)
      .find((entry) => entry.trimStart().startsWith(`${name}=`))
    if (!line) return fallback
    const value = line.slice(line.indexOf("=") + 1).trim()
    return value.startsWith('"') ? JSON.parse(value) : value.split("#")[0].trim()
  } catch {
    return fallback
  }
}

function saveEnv(name, value) {
  const envPath = join(root, ".env")
  let contents = existsSync(envPath) ? readFileSync(envPath, "utf8") : ""
  const line = `${name}=${JSON.stringify(value)}`
  const keyPattern = new RegExp(`^${name}=.*$`, "m")
  if (keyPattern.test(contents)) contents = contents.replace(keyPattern, line)
  else contents = `${contents.replace(/\s*$/, "")}${contents.trim() ? "\n" : ""}${line}\n`
  writeFileSync(envPath, contents, "utf8")
  process.env[name] = value
}

process.env.LLM_MODEL ||= readEnv("LLM_MODEL", "")
process.env.LLM_BACKEND ||= readEnv("LLM_BACKEND", "local")
process.env.MODAL_MODEL_ID ||= readEnv("MODAL_MODEL_ID", modalModelPresets[0].id)
process.env.MODAL_MODEL_FILE ||= readEnv("MODAL_MODEL_FILE", modalModelPresets[0].file)
process.env.MODAL_ENABLE_THINKING ||= readEnv("MODAL_ENABLE_THINKING", "true")
process.env.MODAL_GPU ||= readEnv("MODAL_GPU", "L4")
process.env.LOCAL_CONTEXT_TOKENS ||= readEnv("LOCAL_CONTEXT_TOKENS", "4096")
process.env.MODAL_MAX_MODEL_LEN ||= readEnv("MODAL_MAX_MODEL_LEN", "32768")
process.env.MODAL_SCALEDOWN_SECONDS ||= readEnv("MODAL_SCALEDOWN_SECONDS", "60")
process.env.MODAL_GPU_SNAPSHOT ||= readEnv("MODAL_GPU_SNAPSHOT", "false")

function deployDrift() {
  if (!existsSync(deployedPath)) return ["no deploy recorded from this dashboard yet"]
  try {
    const deployed = JSON.parse(readFileSync(deployedPath, "utf8"))
    return deployKeys
      .filter((key) => String(deployed[key] ?? "") !== String(process.env[key] ?? ""))
      .map((key) => `${key.replace("MODAL_", "").toLowerCase()} ${deployed[key] ?? "—"} → ${process.env[key]}`)
  } catch {
    return ["deploy record unreadable"]
  }
}

function recordDeploy() {
  const record = Object.fromEntries(deployKeys.map((key) => [key, process.env[key] ?? ""]))
  writeFileSync(deployedPath, `${JSON.stringify({ ...record, deployedAt: new Date().toISOString() }, null, 2)}\n`, "utf8")
}

function statusLine() {
  const preset = modalModelPresets.find((entry) => entry.id === process.env.MODAL_MODEL_ID && entry.file === process.env.MODAL_MODEL_FILE)
  if (process.env.LLM_BACKEND === "local") return `LM Studio · ${process.env.LLM_MODEL || "no model"}`
  const name = preset ? preset.name.split(" (")[0] : process.env.MODAL_MODEL_FILE
  return `Modal · ${name} · ${process.env.MODAL_GPU} · ${Number(process.env.MODAL_MAX_MODEL_LEN) / 1024}k ctx · ${process.env.MODAL_SCALEDOWN_SECONDS}s idle`
}

async function ask(question) {
  const readline = createInterface({ input: process.stdin, output: process.stdout })
  const answer = await readline.question(question)
  readline.close()
  return answer.trim()
}

function run(command, args) {
  return new Promise((resolve) => {
    const child = spawn(command, args, { cwd: root, stdio: "inherit" })
    const stopChild = () => child.kill("SIGINT")
    process.once("SIGINT", stopChild)
    child.once("error", (error) => {
      process.off("SIGINT", stopChild)
      console.error(`\nCould not start ${command}: ${error.message}`)
      resolve(1)
    })
    child.once("exit", (code) => {
      process.off("SIGINT", stopChild)
      resolve(code ?? 1)
    })
  })
}

async function chooseModel() {
  console.log("\nChoose a runtime:")
  console.log("1  Local · LM Studio")
  console.log("2  Remote · Modal + llama.cpp")
  const runtime = (await ask("> ")).toLowerCase()

  if (runtime === "1") {
    const baseUrl = process.env.LLM_BASE_URL || readEnv("LLM_BASE_URL", "http://localhost:1234/v1")
    try {
      const response = await fetch(`${baseUrl.replace(/\/$/, "")}/models`, {
        signal: AbortSignal.timeout(4000),
      })
      if (!response.ok) throw new Error(`LM Studio returned HTTP ${response.status}`)
      const body = await response.json()
      const models = body.data.map((model) => model.id)
      if (!models.length) throw new Error("LM Studio has no loaded models")
      console.log("\nLoaded LM Studio models:")
      models.forEach((model, index) => console.log(`${index + 1}  ${model}`))
      const selection = Number(await ask("Model number: "))
      if (!Number.isInteger(selection) || selection < 1 || selection > models.length) {
        console.log("No model selected.")
        return
      }
      saveEnv("LLM_MODEL", models[selection - 1])
      console.log(`\nLocal model saved: ${models[selection - 1]}`)
    } catch (error) {
      console.log(`\nCouldn't list local models: ${error.message}`)
      console.log("Start the LM Studio server, then try again.")
    }
  } else if (runtime === "2") {
    console.log(`\nCurrent Modal GGUF: ${process.env.MODAL_MODEL_ID}/${process.env.MODAL_MODEL_FILE}`)
    console.log("Modal model presets:")
    modalModelPresets.forEach((preset, index) =>
      console.log(`${index + 1}  ${preset.name}  ·  ${preset.contextMode} context, ${Number(preset.context) / 1024}k server`),
    )
    const customSelection = String(modalModelPresets.length + 1)
    console.log(`${customSelection}  Custom Hugging Face GGUF`)
    const selection = (await ask("Model: ")).toLowerCase()
    const preset = modalModelPresets[Number(selection) - 1]
    let nextModel
    let nextFile
    let enableThinking

    if (preset) {
      nextModel = preset.id
      nextFile = preset.file
      enableThinking = preset.enableThinking
    } else if (selection === customSelection || selection === "c" || selection === "custom") {
      const model = await ask("Hugging Face GGUF repository: ")
      const filename = await ask("GGUF filename: ")
      nextModel = model || process.env.MODAL_MODEL_ID
      nextFile = filename || process.env.MODAL_MODEL_FILE
      if (!/^[\w.-]+\/[\w.-]+$/.test(nextModel) || !/^[\w.-]+\.gguf$/i.test(nextFile)) {
        console.log("Enter a repository ID and a single .gguf filename.")
        return
      }
      const thinking = await ask("Enable the Qwen thinking-template option? (y/N): ")
      enableThinking = thinking.toLowerCase() === "y" || thinking.toLowerCase() === "yes" ? "true" : "false"
    } else {
      console.log(`Choose 1 through ${customSelection}, or enter c for a custom model.`)
      return
    }

    saveEnv("MODAL_MODEL_ID", nextModel)
    saveEnv("MODAL_MODEL_FILE", nextFile)
    saveEnv("MODAL_ENABLE_THINKING", enableThinking)
    if (preset) saveEnv("MODAL_MAX_MODEL_LEN", preset.context)
    console.log(`\nModal GGUF saved: ${nextModel}/${nextFile}.`)
    if (preset) console.log(`Deploy settings: ${Number(preset.context) / 1024}k context, thinking ${enableThinking}; the bot uses ${preset.contextMode} context.`)
    console.log("Redeploy the worker, then restart the bot, to apply it.")
  } else {
    console.log("Choose 1 or 2.")
  }
}

function modalJson(args) {
  const result = spawnSync(modal, [...args, "--json"], { cwd: root, encoding: "utf8", timeout: 30000, windowsHide: true })
  if (result.error) throw result.error
  if (result.status !== 0) throw new Error(result.stderr.trim() || `modal ${args.join(" ")} failed`)
  return JSON.parse(result.stdout)
}

function readModalBudget() {
  const report = modalJson(["billing", "summary", "--for", "this month"])
  const spent = Number(report.metered_cost)
  // Per-app costs, so this bot's spend isn't confused with other apps in the workspace (full days only).
  const start = new Date().toISOString().slice(0, 8) + "01"
  const apps = {}
  for (const row of modalJson(["billing", "report", "--start", start])) {
    apps[row.description] = (apps[row.description] ?? 0) + Number(row.cost)
  }
  return { spent, remaining: Math.max(0, monthlyBudget - spent), billed: Number(report.billed_cost), apps }
}

function showModalBudget() {
  try {
    const { spent, remaining, billed, apps } = readModalBudget()
    const selectedPreset = modalModelPresets.find(
      (preset) => preset.id === process.env.MODAL_MODEL_ID && preset.file === process.env.MODAL_MODEL_FILE,
    )
    const storageGiB = selectedPreset?.storageGiB ?? modelStorageGiB
    console.log(`\nModal workspace usage this month: $${spent.toFixed(2)} / $${monthlyBudget.toFixed(2)}`)
    console.log(`Budget remaining: $${remaining.toFixed(2)}  ·  billed after credits: $${billed.toFixed(2)}`)
    console.log("\nBy app (through yesterday):")
    Object.entries(apps)
      .sort(([, a], [, b]) => b - a)
      .forEach(([name, cost]) => console.log(`  ${name === "psychograph" ? mint : ""}${name.padEnd(32)} $${cost.toFixed(2)}${reset}`))
    const rates = readModalRates()
    const storage = storageGiB * Number(rates.volume_storage_gib_month_cost)
    const hourly = Object.keys(gpus).map((gpu) => `${gpu} $${Number(rates[`gpu_hour_cost_${gpu.toLowerCase()}`]).toFixed(2)}`)
    console.log(`\n${process.env.MODAL_MODEL_FILE}: about $${storage.toFixed(2)}/month at a ${storageGiB} GiB estimate.`)
    console.log("The shared volume may retain previously downloaded models as well.")
    console.log(`On-demand GPU per hour: ${hourly.join(" · ")} (this worker: ${process.env.MODAL_GPU}).`)
    console.log(`The worker scales to zero after ${process.env.MODAL_SCALEDOWN_SECONDS} seconds idle; cold starts and this idle tail are also billed.`)
  } catch (error) {
    console.log(`\nCouldn't read Modal usage: ${error.message}`)
  }
}

function readModalRates() {
  return modalJson(["billing", "rates"])
}

async function configureRuntime() {
  console.log(`\nBackend: ${process.env.LLM_BACKEND} · local context: ${process.env.LOCAL_CONTEXT_TOKENS} tokens`)
  console.log(`Modal: ${process.env.MODAL_GPU} · ${Number(process.env.MODAL_MAX_MODEL_LEN) / 1024}k context · ${process.env.MODAL_SCALEDOWN_SECONDS}s idle window`)
  console.log("1  Backend        ·  local LM Studio / on-demand Modal")
  console.log("2  Local context  ·  2048 / 4096 tokens")
  console.log("3  Modal context  ·  16k / 32k / 64k (prompts are trimmed to fit; 32k is ~5x the largest seen)")
  console.log(`4  Modal GPU      ·  ${Object.entries(gpus).map(([gpu, note]) => `${gpu} (${note})`).join(" / ")}`)
  console.log("5  Idle window    ·  60 / 120 / 300 s before scaling to zero (every idle second is billed)")
  const pick = async (label, options) => {
    const answer = (await ask(`${label} [${options.join(" / ")}]: `)).trim()
    const match = options.find((option) => option.toLowerCase() === answer.toLowerCase())
    if (!match) console.log("Unchanged.")
    return match
  }
  const choice = (await ask("> ")).trim()
  let value
  if (choice === "1" && (value = await pick("Backend", ["local", "modal"]))) {
    saveEnv("LLM_BACKEND", value)
    console.log("Restart the local bot for the backend change to take effect.")
  } else if (choice === "2" && (value = await pick("Tokens", ["2048", "4096"]))) {
    saveEnv("LOCAL_CONTEXT_TOKENS", value)
  } else if (choice === "3" && (value = await pick("Context", ["16k", "32k", "64k"]))) {
    saveEnv("MODAL_MAX_MODEL_LEN", String(parseInt(value, 10) * 1024))
  } else if (choice === "4" && (value = await pick("GPU", Object.keys(gpus)))) {
    saveEnv("MODAL_GPU", value)
  } else if (choice === "5" && (value = await pick("Seconds", ["60", "120", "300"]))) {
    saveEnv("MODAL_SCALEDOWN_SECONDS", value)
  } else if (!["1", "2", "3", "4", "5"].includes(choice)) {
    console.log("Choose 1 through 5.")
  }
  if (["3", "4", "5"].includes(choice) && value) console.log("Redeploy the Modal worker, then restart the bot, to apply it.")
}

function tailModalLogs() {
  console.log("\nFollowing Modal logs; Ctrl+C returns to the dashboard.\n")
  const child = spawn(modal, ["app", "logs", "psychograph", "--follow"], {
    cwd: root,
    stdio: ["inherit", "pipe", "pipe"],
  })
  const forward = (stream) => (chunk) => {
    process.stdout.write(chunk)
    appendFileSync(join(root, "bot.log"), chunk)
  }
  child.stdout.on("data", forward(child.stdout))
  child.stderr.on("data", forward(child.stderr))
  child.on("error", (error) => console.error(`\nCould not follow Modal logs: ${error.message}`))
  return new Promise((resolve) => {
    const stop = () => child.kill("SIGINT")
    process.once("SIGINT", stop)
    child.once("exit", () => {
      process.off("SIGINT", stop)
      resolve()
    })
  })
}

while (true) {
  console.clear()
  console.log(`${mint}(｡•̀ᴗ-)✧  PSYCHOGRAPH${reset}`)
  console.log(statusLine())
  const drift = process.env.LLM_BACKEND === "modal" ? deployDrift() : []
  if (drift.length) console.log(`\x1b[33m⚠ Modal worker differs from .env: ${drift.join(", ")}. Deploy (2) to apply.${reset}`)
  console.log("")
  console.log("1  Run bot locally  ·  configured backend")
  console.log("2  Deploy Modal worker  ·  zero GPU until called")
  console.log("3  Stop Modal worker deployment")
  console.log("4  Choose model")
  console.log("5  Check Modal budget")
  console.log("6  Test Modal model  ·  one completion")
  console.log("7  Follow Modal logs  ·  mirror to bot.log")
  console.log("8  Config  ·  backend and context")
  console.log("9  Bot stats  ·  replies, speed, cold starts")
  console.log(`b  Benchmark ${process.env.MODAL_GPU}  ·  one cold boot + 4 fixed prompts, ~1-2 GPU-min`)
  console.log("t  Run tests")
  console.log("q  Quit\n")

  const choice = (await ask("> ")).toLowerCase()
  if (choice === "q") break

  if (choice === "1") {
    console.log("\nStarting the local bot. Press Ctrl+C to stop it.\n")
    await run(python, pythonArgs)
  } else if (choice === "2") {
    const confirmation = await ask("\nDeploy the on-demand Modal inference worker? No GPU starts during deploy. Type 'deploy' to confirm: ")
    if (confirmation.toLowerCase() === "deploy") {
      if ((await run(modal, ["deploy", "modal_app.py"])) === 0) recordDeploy()
    } else {
      console.log("\nDeployment cancelled.")
    }
  } else if (choice === "3") {
    const confirmation = await ask("\nPermanently stop the Modal deployment? Type 'stop' to confirm: ")
    if (confirmation === "stop") {
      console.log("\nStopping Modal app...\n")
      await run(modal, ["app", "stop", "psychograph", "--yes"])
    } else {
      console.log("\nStop cancelled.")
    }
  } else if (choice === "4") {
    await chooseModel()
  } else if (choice === "5") {
    showModalBudget()
  } else if (choice === "6") {
    const confirmation = await ask(`\nRun one real completion on a ${process.env.MODAL_GPU}? Cold start and generation are billed. Type 'test' to confirm: `)
    if (confirmation.toLowerCase() === "test") {
      console.log("\nRunning one on-demand completion...\n")
      await run(modal, ["run", "modal_app.py"])
    } else {
      console.log("\nTest cancelled.")
    }
  } else if (choice === "7") {
    await tailModalLogs()
  } else if (choice === "8") {
    await configureRuntime()
  } else if (choice === "9") {
    const period = (await ask("Period (day/week/month/all) [week]: ")).toLowerCase() || "week"
    console.log("")
    await run(python, [...pythonArgs, "stats", period])
  } else if (choice === "b") {
    const confirmation = await ask(`\nBenchmark on a ${process.env.MODAL_GPU} as a temporary app (billed, a few cents)? Type 'bench' to confirm: `)
    if (confirmation.toLowerCase() === "bench") await run(modal, ["run", "modal_app.py::bench"])
    else console.log("\nBenchmark cancelled.")
  } else if (choice === "t") {
    console.log("")
    await run(python, [...pythonPrefix, "-m", "unittest", "discover", "-s", "tests", "-t", "."])
  } else {
    console.log("\nChoose 1 through 9, b, t, or q.")
  }

  await ask("\nPress Enter to return to the menu.")
}
