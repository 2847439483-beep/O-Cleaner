// CDP 界面探针 —— 自起 headless 浏览器，量几何 / 截图 / 验证语言切换。
// 零安装依赖：Node 22+ 内置 WebSocket，无需任何 npm 包。
// 浏览器从常见安装位置自动探测（Edge / Chrome）。
// 用法:
//   node tools/probe_ui.js <url> [--shot out.png] [--lang en|zh]
//                          [--viewport 1570x905] [--eval "<js 表达式>"]
// --eval 结果以 JSON 打印到 stdout；页面未就绪或求值异常时退出码 1。
// 典型用途：
//   * 量某个元素的 getBoundingClientRect（判断溢出/裁切，缩略图会骗人）
//   * 截当前视口 PNG（配合 --lang 验证中英文界面）
//   * 求值任意表达式核对渲染结果（如切语言后自检面板行名）
"use strict";
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const CANDIDATES = [
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Users/" + process.env.USERNAME + "/AppData/Local/Google/Chrome/Application/chrome.exe",
];

function parseArgs(argv) {
  const o = { url: "", shot: "", lang: "", viewport: "1570x905", eval: "" };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--shot") o.shot = argv[++i];
    else if (a === "--lang") o.lang = argv[++i];
    else if (a === "--viewport") o.viewport = argv[++i];
    else if (a === "--eval") o.eval = argv[++i];
    else if (!o.url && !a.startsWith("--")) o.url = a;
  }
  if (!o.url) { console.error("usage: node probe_ui.js <url> [--shot f.png] [--lang en] [--viewport WxH] [--eval expr]"); process.exit(2); }
  return o;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const opt = parseArgs(process.argv.slice(2));
  const bin = CANDIDATES.find(c => fs.existsSync(c));
  if (!bin) throw new Error("找不到 Edge 或 Chrome");
  const [vw, vh] = opt.viewport.split("x").map(n => parseInt(n, 10) || 0);
  const profile = path.join(os.tmpdir(), "oc_probe_" + Date.now().toString(36));
  const port = 9200 + Math.floor(Math.random() * 300);
  const proc = spawn(bin, ["--headless=new", "--remote-debugging-port=" + port,
    "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", "--disable-gpu", "--window-size=" + vw + "," + vh,
    "about:blank"], { stdio: "ignore" });
  try {
    let version = null;
    for (let i = 0; i < 60; i++) {
      await sleep(400);
      try { version = await (await fetch("http://127.0.0.1:" + port + "/json/version")).json(); break; } catch (e) {}
    }
    if (!version) throw new Error("浏览器调试端口未就绪");
    const ws = new WebSocket(version.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener("open", res, { once: true });
      ws.addEventListener("error", rej, { once: true });
    });
    let id = 0; const pend = new Map();
    ws.addEventListener("message", m => {
      const o = JSON.parse(m.data);
      if (o.id && pend.has(o.id)) { pend.get(o.id)(o.result || o.error); pend.delete(o.id); }
    });
    const send = (method, params, sessionId) => new Promise((res, rej) => {
      const i = ++id; pend.set(i, res);
      const msg = { id: i, method, params };
      if (sessionId) msg.sessionId = sessionId;
      ws.send(JSON.stringify(msg));
    });
    const { targetId } = await send("Target.createTarget", { url: "about:blank" });
    const { sessionId: S } = await send("Target.attachToTarget", { targetId, flatten: true });
    const ev = async expr => {
      const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true }, S);
      if (r.exceptionDetails) throw new Error("页面异常: " +
        JSON.stringify(r.exceptionDetails).slice(0, 300));
      return r.result.value;
    };
    await send("Page.enable", {}, S);
    await send("Emulation.setDeviceMetricsOverride",
      { width: vw, height: vh, deviceScaleFactor: 1, mobile: false }, S);
    await send("Page.navigate", { url: opt.url }, S);
    await sleep(4500);                      // 等 loadState + 首屏渲染
    if (opt.lang) {
      // 先写 localStorage 再 setLang：静态骨架与动态渲染一起切
      await ev('localStorage.setItem("oc-lang", ' + JSON.stringify(opt.lang) +
               '); setLang && setLang(' + JSON.stringify(opt.lang) + '); "ok"');
      await sleep(1200);
    }
    if (opt.eval) {
      const out = await ev(opt.eval);
      console.log(JSON.stringify(out, null, 1));
    }
    if (opt.shot) {
      const shot = await send("Page.captureScreenshot", { format: "png" }, S);
      fs.writeFileSync(opt.shot, Buffer.from(shot.data, "base64"));
      console.log("screenshot -> " + opt.shot);
    }
    if (!opt.eval && !opt.shot) console.log(JSON.stringify({ ok: true, url: opt.url }));
    ws.close();
  } finally {
    proc.kill();
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
  }
})().catch(e => { console.error("ERR", e.message); process.exit(1); });
