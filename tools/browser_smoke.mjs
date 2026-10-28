import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";

const root = await fs.mkdtemp(path.join(os.tmpdir(), "jamly-browser-"));
const python = process.env.JAMLY_PYTHON || path.resolve(
  process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python"
);
const runtimeIndex = process.argv.indexOf("--runtime");
const runtime = runtimeIndex < 0 ? null : path.resolve(process.argv[runtimeIndex + 1]);
const chromePath = process.env.CHROME_BIN || ({
  darwin: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  linux: "/usr/bin/google-chrome",
  win32: path.join(process.env.PROGRAMFILES || "C:/Program Files", "Google/Chrome/Application/chrome.exe"),
})[process.platform];
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let backend;
let token;
let chrome;
let cdp;
let browserLog = "";
let chromeError;
let debugURL;

async function command(executable, args) {
  const environment = { ...process.env };
  for (const key of Object.keys(environment)) if (key.startsWith("JAMLY_")) delete environment[key];
  const child = spawn(executable, args, { stdio: ["ignore", "pipe", "pipe"], env: environment });
  let output = "";
  let errors = "";
  child.stdout.on("data", data => { output += data; });
  child.stderr.on("data", data => { errors += data; });
  const [code] = await once(child, "exit");
  assert.equal(code, 0, errors);
  return output;
}

class CDP {
  constructor(url) {
    this.socket = new WebSocket(url);
    this.sequence = 0;
    this.pending = new Map();
    this.diagnostics = [];
    this.ready = new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener("error", reject, { once: true });
    });
    this.socket.addEventListener("message", event => {
      const message = JSON.parse(event.data);
      if (!message.id) {
        if (message.method === "Log.entryAdded") this.diagnostics.push(message.params.entry.text);
        if (message.method === "Runtime.exceptionThrown") {
          this.diagnostics.push(message.params.exceptionDetails.text);
        }
        return;
      }
      const entry = this.pending.get(message.id);
      if (!entry) return;
      clearTimeout(entry.timer);
      this.pending.delete(message.id);
      if (message.error) entry.reject(new Error(message.error.message));
      else entry.resolve(message.result);
    });
  }
  async send(method, params = {}) {
    await this.ready;
    const id = ++this.sequence;
    const result = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("Browser command timed out: " + method));
      }, 15000);
      this.pending.set(id, { resolve, reject, timer });
    });
    this.socket.send(JSON.stringify({ id, method, params }));
    return result;
  }
  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression, returnByValue: true, awaitPromise: true, userGesture: true,
    });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    return result.result.value;
  }
  close() {
    for (const entry of this.pending.values()) clearTimeout(entry.timer);
    this.socket.close();
  }
}

async function waitFor(probe, description, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await probe()) return;
    await sleep(100);
  }
  throw new Error("Timed out: " + description);
}

const frames = 11025;
const audio = Buffer.alloc(44 + frames * 4);
audio.write("RIFF", 0);
audio.writeUInt32LE(audio.length - 8, 4);
audio.write("WAVEfmt ", 8);
audio.writeUInt32LE(16, 16);
audio.writeUInt16LE(1, 20);
audio.writeUInt16LE(2, 22);
audio.writeUInt32LE(44100, 24);
audio.writeUInt32LE(44100 * 4, 28);
audio.writeUInt16LE(4, 32);
audio.writeUInt16LE(16, 34);
audio.write("data", 36);
audio.writeUInt32LE(frames * 4, 40);
for (let frame = 0; frame < frames; frame++) {
  const value = Math.round(6000 * Math.sin(frame * 2 * Math.PI * 440 / 44100));
  audio.writeInt16LE(value, 44 + frame * 4);
  audio.writeInt16LE(value, 46 + frame * 4);
}
const media = http.createServer((_request, response) => {
  response.writeHead(200, { "Content-Type": "audio/wav", "Content-Length": audio.length });
  response.end(audio);
});

try {
  await new Promise(resolve => media.listen(0, "127.0.0.1", resolve));
  const state = path.join(root, "state");
  const arguments_ = [
    "--no-browser", "--state-dir", state, "--port", "0",
    "--output-dir", path.join(root, "exports"),
  ];
  const launched = JSON.parse(await command(runtime || python,
    runtime ? arguments_ : ["-m", "jamly.companion", ...arguments_]));
  backend = launched.url;
  token = (await fs.readFile(path.join(state, "server-token"), "utf8")).trim();
  const profile = path.join(root, "chrome");
  chrome = spawn(chromePath, [
    "--headless=new", "--remote-debugging-port=0", "--user-data-dir=" + profile,
    "--no-first-run", "--no-default-browser-check", "--disable-extensions", "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  chrome.stderr.on("data", data => { browserLog += data; });
  chrome.on("error", error => { chromeError = error; });
  let port;
  await waitFor(async () => {
    if (chromeError) throw chromeError;
    if (chrome.exitCode !== null || chrome.signalCode !== null) throw new Error("Chrome exited: " + browserLog);
    try {
      port = (await fs.readFile(path.join(profile, "DevToolsActivePort"), "utf8")).split("\n")[0];
      return true;
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      return false;
    }
  }, "Chrome readiness");
  debugURL = `http://127.0.0.1:${port}`;
  const response = await fetch(`${debugURL}/json/new?${encodeURIComponent(launched.launch_file)}`, { method: "PUT" });
  const target = await response.json();
  cdp = new CDP(target.webSocketDebuggerUrl);
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Log.enable");
  await waitFor(async () => cdp.evaluate(
    'location.pathname === "/app/" && document.getElementById("workspace")?.hidden === false'
  ), "automatic companion pairing");
  assert.equal(await cdp.evaluate("location.search + location.hash"), "");
  const downloads = path.join(root, "downloads");
  await fs.mkdir(downloads);
  await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloads });
  await cdp.evaluate(`(() => {
    const model = document.getElementById("model");
    model.value = "htdemucs_ft"; model.dispatchEvent(new Event("change"));
    document.getElementById("format").value = "wav";
  })()`);
  assert.match(await cdp.evaluate('document.getElementById("model-help").textContent'), /4x/);
  const source = `http://127.0.0.1:${media.address().port}/generated.wav`;
  await cdp.evaluate(`(() => {
    document.getElementById("inputs").value = ${JSON.stringify(source)};
    document.getElementById("split-form").requestSubmit();
  })()`);
  await waitFor(async () => {
    const error = await cdp.evaluate('document.getElementById("error")?.textContent');
    if (error) throw new Error(error);
    const jobError = await cdp.evaluate('document.querySelector("#jobs .error")?.textContent');
    if (jobError) throw new Error(jobError);
    return cdp.evaluate('document.getElementById("jobs")?.textContent.includes("Complete | Higher quality | WAV")');
  }, "real Higher quality export", 600000);
  for (const kind of ["drums", "drumless"]) {
    await cdp.evaluate(`Array.from(document.querySelectorAll("#jobs button"))
      .find(button => button.textContent === ${JSON.stringify("Save a copy: " + kind)}).click()`);
  }
  await waitFor(async () => {
    const files = await fs.readdir(downloads);
    return files.filter(file => file.endsWith(".wav")).length === 2;
  }, "authenticated browser downloads");
  console.log("Real Chrome: automatic pairing, readable preset/help, actual ft export, and both native downloads passed.");
} catch (error) {
  if (cdp) {
    try {
      console.error("Browser diagnostics:", cdp.diagnostics);
      console.error("Owned test tabs:", (await cdp.send("Target.getTargets")).targetInfos.map(target => target.url));
      console.error("Downloaded files:", await fs.readdir(path.join(root, "downloads")));
      const tabs = await (await fetch(debugURL + "/json/list")).json();
      for (const tab of tabs.filter(tab => tab.url.startsWith(backend + "/app/download/"))) {
        const failed = new CDP(tab.webSocketDebuggerUrl);
        console.error("Download response:", await failed.evaluate("document.body.innerText"));
        failed.close();
      }
    } catch (diagnosticError) {
      console.error("Could not read all browser diagnostics:", diagnosticError.message);
    }
  }
  throw error;
} finally {
  if (cdp) cdp.close();
  if (chrome?.pid && chrome.exitCode === null && chrome.signalCode === null) {
    const exited = once(chrome, "exit");
    if (process.platform === "win32") {
      execFileSync(path.join(process.env.SystemRoot, "System32/taskkill.exe"),
        ["/PID", String(chrome.pid), "/T", "/F"], { stdio: "ignore" });
    } else chrome.kill("SIGTERM");
    await exited;
  }
  if (backend && token) {
    const response = await fetch(backend + "/app/stop", {
      method: "POST", headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
      body: "{}",
    });
    assert.equal(response.status, 200);
    await waitFor(async () => !(await fs.readdir(path.join(root, "state"))).some(
      name => name.startsWith("server-") && name.endsWith(".json")
    ), "backend shutdown");
  }
  await new Promise(resolve => media.close(resolve));
  await fs.rm(root, { recursive: true, force: true });
}
