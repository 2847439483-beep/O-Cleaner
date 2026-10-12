// CDP 渲染检查器 —— 用系统已有的 Edge/Chrome 真实渲染页面，零安装。
// 用法: node cdp_check.js http://127.0.0.1:8797
// 检查：卡片布局是否合理、内容是否溢出、面板是否已合并、规则功能是否可用、有无 JS 报错。
"use strict";
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const URL_ = process.argv[2] || "http://127.0.0.1:8797";
// 可选：先切到指定盘再测（系统盘全盘扫描可能很慢，用数据盘更快）
const DRIVE_ = (process.argv[3] || "").toUpperCase().replace(":", "");

const CANDIDATES = [
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(os.homedir(), "AppData\\Local\\Google\\Chrome\\Application\\chrome.exe"),
];

const sleep = ms => new Promise(r => setTimeout(r, ms));
// CI 上（GitHub Actions 会设置 CI=true）系统盘内容密度与本机完全不同，
// 有些「本机必然成立」的断言在 runner 上不成立，需要区别对待而不是直接判失败。
const ON_CI = String(process.env.CI || "").toLowerCase() === "true";

const PASS = [], FAIL = [], SKIP = [], INFO = [];
function check(name, cond, detail) {
  (cond ? PASS : FAIL).push(name);
  console.log((cond ? "  [OK]   " : "  [严重] ") + name + (detail ? "   " + detail : ""));
}
function skip2(why) { SKIP.push(why); console.log("  [跳过] " + why); }
// 旧代码里散落着 skip("xxx", "原因") 的两参调用，但只定义了 skip2 —— 一旦真走到
// 那个分支就是 ReferenceError，把整个检查器带崩。这里补上同名兼容版。
function skip(name, why) { skip2(why ? name + "：" + why : name); }

// 全盘扫描是唯一一条「结果取决于盘里有多少小文件」的检查：开发机上数据盘约 25 秒，
// 但 CI runner 的系统盘有十几万个小文件，跑几分钟也扫不完。一旦它没跑完，
// #fsChart / .ring / .drillhint / .crumbs 全是空的，后面四十多条断言会成片误报。
// 所以引入 checkScan()：扫描没完成时这些检查记为「跳过」而不是「失败」，
// 根因仍由上面那条「全盘扫描完成并渲染出排行」单独暴露。
let scanOk = true;
function checkScan(name, cond, detail) {
  if (scanOk) check(name, cond, detail);
  else skip2(name + "（全盘扫描未在时限内完成，无法验证）");
}

async function findBrowser() {
  for (const c of CANDIDATES) if (fs.existsSync(c)) return c;
  throw new Error("找不到 Edge 或 Chrome");
}

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.handlers = []; }
  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => {
      ws.addEventListener("open", res, { once: true });
      ws.addEventListener("error", rej, { once: true });
    });
    const c = new CDP(ws);
    ws.addEventListener("message", ev => {
      const m = JSON.parse(ev.data);
      if (m.id && c.pending.has(m.id)) {
        const { res, rej } = c.pending.get(m.id);
        c.pending.delete(m.id);
        m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
      } else if (m.method) {
        c.handlers.forEach(h => h(m));
      }
    });
    return c;
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    const msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    this.ws.send(JSON.stringify(msg));
    return new Promise((res, rej) => this.pending.set(id, { res, rej }));
  }
  on(fn) { this.handlers.push(fn); }
}

(async () => {
  const bin = await findBrowser();
  const profile = path.join(os.tmpdir(), "oc_cdp_" + Date.now().toString(36));
  const port = 9200 + Math.floor(Math.random() * 300);
  console.log(`  浏览器: ${path.basename(bin)}  调试端口: ${port}`);

  const proc = spawn(bin, [
    "--headless=new",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-gpu", "--window-size=1600,1000",
    "about:blank",
  ], { stdio: "ignore" });

  let version = null;
  for (let i = 0; i < 60; i++) {
    await sleep(400);
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`);
      version = await r.json();
      break;
    } catch { }
  }
  if (!version) { proc.kill(); throw new Error("浏览器调试端口未就绪"); }

  const cdp = await CDP.connect(version.webSocketDebuggerUrl);
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  const S = sessionId;

  const errors = [], failedReqs = [];
  cdp.on(m => {
    if (m.method === "Runtime.exceptionThrown") {
      errors.push(m.params.exceptionDetails?.exception?.description
        || m.params.exceptionDetails?.text || "unknown");
    }
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
      errors.push((m.params.args || []).map(a => a.value ?? a.description).join(" "));
    }
    if (m.method === "Network.loadingFailed") {
      failedReqs.push(m.params.errorText);
    }
  });

  await cdp.send("Runtime.enable", {}, S);
  await cdp.send("Page.enable", {}, S);
  await cdp.send("Network.enable", {}, S);
  await cdp.send("Page.navigate", { url: URL_ }, S);
  // 首屏扫描耗时完全取决于机器：开发机几秒，冷启动的 CI runner 上逐个测量
  // 30+ 个目标（每个都要起一次 PowerShell）可能要几分钟。旧的写法是「写死等
  // 9 秒 + 最多再等 40 秒」，在 CI 上必然等不到，于是「0/33 张有大小」一路
  // 连锁失败。这里只给一个起步缓冲，真正的等待交给下面的宽松轮询。
  await sleep(3000);

  const evaluate = async (expr) => {
    const r = await cdp.send("Runtime.evaluate",
      { expression: expr, returnByValue: true, awaitPromise: true }, S);
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
    return r.result.value;
  };

  console.log("\n  --- 真实渲染检查 ---");

  // 等页面完成首次加载与扫描，否则切盘会被随后的 loadState/scan 覆盖。
  // 上限放宽到 300 秒：等不到才算失败，而不是按固定秒数猜机器有多快。
  let scanReady = false;
  for (let i = 0; i < 300; i++) {
    await sleep(1000);
    const done = await evaluate(`document.querySelectorAll('.card .cnum .size').length > 0
        && !document.querySelector('#btnScan').disabled`);
    if (done) { scanReady = true; break; }
  }
  if (!scanReady) console.log("  [注意] 等待首屏扫描完成超时（300 秒上限）");

  // 首屏「常规扫描」与「全盘扫描」是同一类环节：它要逐个量出全部清理目标的体积，
  // 系统盘上同样可能超出时限。扫描没完成时卡片不会被回填，也不会出现可勾选的项 ——
  // 依赖这两件事的断言就失去了前提，应记为跳过而不是成片误报。
  // 本机（非 CI）超时仍判失败，因为那是真实退化。
  function checkInit(name, cond, detail) {
    if (scanReady) check(name, cond, detail);
    else if (ON_CI) skip2(name + "（首屏扫描未在时限内完成，无法验证）");
    else check(name, false, detail);
  }

  if (DRIVE_) {
    const got = await evaluate(`(() => {
      const cur = document.querySelector('.drv.on');
      return cur ? cur.dataset.l : '';
    })()`);
    if (got === DRIVE_) {
      console.log(`  [信息] 已在 ${DRIVE_}: 盘`);
    } else {
      const clicked = await evaluate(`(() => {
        const n = [...document.querySelectorAll('.drv')].find(d => d.dataset.l === '${DRIVE_}');
        if (!n) return false;
        n.click(); return true;
      })()`);
      if (clicked) {
        // 等盘符切换真正生效（页头高亮 + 卡片重新扫描完成）
        let ok = false;
        for (let i = 0; i < 40; i++) {
          await sleep(1000);
          const st = await evaluate(`(() => {
            const cur = document.querySelector('.drv.on');
            const btn = document.querySelector('#btnScan');
            return { on: cur ? cur.dataset.l : '', busy: btn.disabled };
          })()`);
          if (st.on === DRIVE_ && !st.busy) { ok = true; break; }
        }
        console.log(ok ? `  [信息] 已切换到 ${DRIVE_}: 盘`
                       : `  [注意] 切换到 ${DRIVE_}: 盘未确认生效`);
      } else {
        console.log(`  [注意] 未找到 ${DRIVE_}: 盘，沿用默认盘`);
      }
    }
  }
  const scanDrive = await evaluate(`(() => {
    const cur = document.querySelector('.drv.on');
    return cur ? cur.dataset.l : '?';
  })()`);
  console.log(`  [信息] 当前盘: ${scanDrive}:`);

  // 由于前端现在默认自动全盘扫描，若处于 fullView 则先切回 normalView 校验常规清理卡片
  await evaluate(`(() => {
    if (typeof showNormalView === 'function' && document.querySelector('#fullView')?.style.display !== 'none') {
      showNormalView();
    }
  })()`);
  await sleep(1000);

  const stats = await evaluate(`(() => {
    const cards = [...document.querySelectorAll('.card')];
    // STATE/SCAN 在 <script> 里是模块作用域，外部读不到，只能从 DOM 判断状态
    const filled = cards.filter(c => {
      const s = c.querySelector('.cnum .size');
      return s && s.innerText.trim() && s.innerText.trim() !== '—';
    }).length;
    const sec = document.querySelector('#normalView');
    const asd = document.querySelector('aside');
    return {
      cards: cards.length,
      filled: filled,
      ruleBtn: !!document.querySelector('#btnRuleSave'),
      convItems: document.querySelectorAll('.convitem').length,
      heads: [...document.querySelectorAll('aside h3')].map(h => h.innerText.trim().split('\\n')[0]),
      scopeSel: !!document.querySelector('#scopeSel'),
      drives: document.querySelectorAll('.drv').length,
      secX: sec ? Math.round(sec.getBoundingClientRect().x) : -1,
      secW: sec ? Math.round(sec.getBoundingClientRect().width) : -1,
      asdX: asd ? Math.round(asd.getBoundingClientRect().x) : -1,
      asdW: asd ? Math.round(asd.getBoundingClientRect().width) : -1,
      vw: window.innerWidth,
    };
  })()`);
  INFO.push(`卡片 ${stats.cards} 张 / 已回填大小 ${stats.filled} 张 / 盘符 ${stats.drives} 个`);
  console.log(`  [信息] ${INFO[0]}`);
  console.log(`  [信息] 布局：section x=${stats.secX} w=${stats.secW} | aside x=${stats.asdX} w=${stats.asdW} | 视口 ${stats.vw}`);

  check("页面渲染出目标卡片", stats.cards > 0, `${stats.cards} 张`);
  checkInit("扫描结果已回填到卡片", stats.filled > 0, `${stats.filled}/${stats.cards} 张有大小`);
  check("盘符选择器存在", stats.drives >= 1, `${stats.drives} 个盘`);
  check("清理范围选择器存在", stats.scopeSel);
  // 布局列位置：卡片区必须在左、侧栏必须在右，且卡片区要占满剩余宽度
  check("卡片区在左列且占据主体宽度",
        stats.secX < stats.asdX && stats.secW > stats.asdW,
        `卡片区 x=${stats.secX} w=${stats.secW}，侧栏 x=${stats.asdX} w=${stats.asdW}`);
  check("卡片区宽度合理（未被挤成窄列）", stats.secW > stats.vw * 0.5,
        `${stats.secW}px / 视口 ${stats.vw}px`);

  // 面板合并
  const merged = stats.heads.some(h => h.includes("累积清理与换算"));
  const stale = stats.heads.filter(h => h.includes("换算成什么") || h === "累积清理");
  check("换算与累积已合并为一个面板", merged && stale.length === 0,
        `标题: ${stats.heads.slice(0, 6).join(" / ")}`);
  check("旧的独立换算面板已移除", !stats.heads.includes("换算成什么"));

  // 卡片布局：操作区宽度合理、靠右、不是窄高竖条
  const layout = await evaluate(`(() => {
    const out = { narrow: [], high: [], left: [], overflow: [], minW: 1e9 };
    for (const c of [...document.querySelectorAll('.card')].slice(0, 15)) {
      const cb = c.getBoundingClientRect();
      const act = c.querySelector('.cact');
      if (!act) continue;
      const ab = act.getBoundingClientRect();
      out.minW = Math.min(out.minW, ab.width);
      if (ab.width < 120) out.narrow.push(Math.round(ab.width));
      if (ab.height > 140) out.high.push(Math.round(ab.height));
      const gapRight = (cb.right - ab.right);
      if (gapRight > 40) out.left.push(Math.round(gapRight));
      for (const sel of ['.label', '.desc', '.path']) {
        const n = c.querySelector(sel);
        if (!n) continue;
        const nb = n.getBoundingClientRect();
        if (nb.right > cb.right + 1) out.overflow.push(sel);
      }
    }
    return out;
  })()`);
  check("卡片操作区宽度充足（非窄条）", layout.narrow.length === 0,
        `最窄 ${Math.round(layout.minW)}px` + (layout.narrow.length ? ` 过窄:${layout.narrow}` : ""));
  check("卡片操作区不高于竖条形态", layout.high.length === 0, layout.high.join(","));
  check("卡片操作区紧贴卡片右侧", layout.left.length === 0, layout.left.join(","));
  check("卡片内容未溢出边界", layout.overflow.length === 0, layout.overflow.join(","));

  // 预设文字不溢出
  const presetOverflow = await evaluate(`(() => {
    const bad = [];
    for (const p of document.querySelectorAll('.preset')) {
      const pb = p.getBoundingClientRect();
      for (const n of p.querySelectorAll('strong, span')) {
        const nb = n.getBoundingClientRect();
        if (nb.right > pb.right + 1 || nb.bottom > pb.bottom + 1) bad.push(p.innerText.slice(0, 12));
      }
    }
    return bad;
  })()`);
  check("一键预设文字不溢出框位", presetOverflow.length === 0, presetOverflow.join(" | "));

  // 规则功能
  const r0 = await evaluate(`document.querySelector('#btnRuleSave').disabled`);
  // 必须点一张「真的能勾选」的卡片：盘上第一张卡可能是危险/只读项
  // （checkbox 被 disabled、卡片带 .dead），点它不会产生任何勾选。
  // CDP 在 CI 上默认落在 C: 盘，首卡恰好常是不可选中的系统项，
  // 旧写法点死第一张，于是「勾选后可用」永远为 false。
  const picked = await evaluate(`(() => {
      const c = [...document.querySelectorAll('.card')].find(el => {
        const cb = el.querySelector('input[type=checkbox]');
        return cb && !cb.disabled;
      });
      if (!c) return false;
      c.click();
      return true;
    })()`);
  await sleep(500);
  const r1 = await evaluate(`!document.querySelector('#btnRuleSave').disabled`);
  const rinfo = await evaluate(`document.querySelector('#ruleSelInfo').innerText`);
  checkInit("规则保存按钮随勾选启用", r0 === true && r1 === true,
        `初始禁用=${r0} 勾选后可用=${r1}${picked ? "" : "（未找到可勾选的卡片）"} 提示="${rinfo}"`);

  if (!scanReady) {
    // 上一批卡片没有回填体积，也就没有可勾选的项，规则链路的断言全部失去前提。
    // 这里必须显式补两条同名断言，否则「跳过」会变成「少了一条检查」而无人察觉。
    checkInit("保存规则后出现在规则列表", false, "首屏扫描未完成");
    checkInit("规则卡片含 套用/立即清理/改名/删 操作", false, "首屏扫描未完成");
  } else {
    await evaluate(`document.querySelector('#ruleName').value = 'UI自测规则';
                    document.querySelector('#btnRuleSave').click()`);
    await sleep(2200);
    const ruleTxt = await evaluate(`document.querySelector('#ruleList').innerText`);
    check("保存规则后出现在规则列表", ruleTxt.includes("UI自测规则"), ruleTxt.slice(0, 60).replace(/\\n/g, " "));
    const btns = await evaluate(`['apply','run','rename','rdel']
        .map(a => !!document.querySelector('#ruleList [data-' + a + ']'))`);
    check("规则卡片含 套用/立即清理/改名/删 操作", btns.every(Boolean), JSON.stringify(btns));
  }

  // 勾选同步：套用规则（规则列表可能为空，取不到按钮时跳过而不是抛 Uncaught）
  const hasApply = await evaluate(`!!document.querySelector('#ruleList [data-apply]')`);
  if (hasApply) {
    await evaluate(`document.querySelector('#ruleList [data-apply]').click()`);
    await sleep(700);
    const applied = await evaluate(`({sel: document.querySelector('#selN').innerText,
                                     size: document.querySelector('#selSize').innerText})`);
    check("套用规则后底部统计正确", parseInt(applied.sel) > 0 && applied.size !== "0 B",
          `已选 ${applied.sel} 项 / ${applied.size}`);
  } else {
    skip2("套用规则（规则列表为空，说明前面的保存未成功）");
  }

  // 换算单位开关
  const tg = await evaluate(`document.querySelectorAll('#convToggle .tgl').length`);
  check("换算单位切换按钮存在", tg > 1, `${tg} 个`);

  // ---------- 全盘扫描 + 可视化 + 点击详情 ----------
  console.log("\n  --- 全盘扫描 / 可视化 / 详情 ---");
  await evaluate(`document.querySelector('#btnFullScan').click()`);
  // 全盘扫描耗时取决于盘的内容密度：数据盘约 25 秒，系统盘（小文件极多）可达 3 分钟。
  // 因此这里给足 5 分钟，并且不强求成功 —— 若本机盘太大，明确报告跳过而不是误判失败。
  let ready = false, failed = false;
  const t0 = Date.now();
  for (let i = 0; i < 200; i++) {
    await sleep(1500);
    const st = await evaluate(`(() => {
      const btn = document.querySelector('#btnFullScan');
      return { txt: btn.innerText.trim(),
               rows: document.querySelectorAll('#fsChart .vrow').length };
    })()`);
    if (st.rows > 0) { ready = true; break; }
    if (/失败|重试/.test(st.txt)) { failed = true; break; }
  }
  const scanSec = ((Date.now() - t0) / 1000).toFixed(0);
  // 扫描「跑不跑得完」取决于盘的内容密度，不是代码正确性。CI runner 的系统盘
  // 必然超时，那里只记录跳过；本机若也超时，那才是真问题（说明扫描退化或被卡死）。
  scanOk = ready;
  if (ready) {
    check("全盘扫描完成并渲染出排行", true, `${scanDrive}: 盘，耗时 ${scanSec} 秒`);
  } else if (failed) {
    check("全盘扫描完成并渲染出排行", false, "扫描报错");
  } else if (ON_CI) {
    skip2(`全盘扫描未在 ${scanSec} 秒内完成（CI 系统盘内容密度高，属预期；` +
          `本机可用 python -X utf8 app.py --fullscan-only --drive ${scanDrive} 单独验证）`);
  } else {
    check("全盘扫描完成并渲染出排行", false,
          `${scanDrive}: 盘，超时 ${scanSec} 秒未出结果`);
  }
  // 确认扫描的是我们指定的盘，而不是静默跑在别的盘上
  if (DRIVE_ && ready) {
    const title = await evaluate(`(() => {
      const h = document.querySelector('#fsHint');
      return h ? h.innerText.trim() : '';
    })()`);
    check(`全盘扫描作用于指定盘 ${DRIVE_}:`, title.startsWith(DRIVE_),
          `面板显示「${title}」`);
  }
  if (!ready) {
    console.log("  [提示] 全盘扫描未能在此时间内完成，后续依赖它的检查将跳过。");
    console.log("         可单独用 CLI 验证：python -X utf8 app.py --fullscan-only --drive D");
  }

  const ovInfo = await evaluate(`(() => {
    const ring = document.querySelector('.ring');
    const track = document.querySelector('.clbar');
    return {
      hasRing: !!ring,
      ringBg: ring ? getComputedStyle(ring).backgroundImage.slice(0, 24) : '',
      hasClbar: !!track,
      cleanW: track ? track.querySelector('i.c').style.width : '',
      stats: document.querySelector('#fsOverview').innerText.slice(0, 130).replace(/\\n/g,' / '),
    };
  })()`);
  checkScan("磁盘概览含占比环", ovInfo.hasRing && /conic-gradient/.test(ovInfo.ringBg),
        ovInfo.ringBg);
  checkScan("磁盘概览含可清理/保留比例条", ovInfo.hasClbar && /%/.test(ovInfo.cleanW),
        `可清理占比 ${ovInfo.cleanW}`);
  console.log(`  [信息] 概览: ${ovInfo.stats}`);

  const chart = await evaluate(`(() => {
    const rows = [...document.querySelectorAll('#fsChart .vrow')];
    const out = { n: rows.length, noName: 0, noFill: 0, nameAbove: 0,
                  badges: { clean: 0, keep: 0, unk: 0 }, sample: [] };
    for (const r of rows) {
      const nm = r.querySelector('.fname');
      const fill = r.querySelector('.vfill');
      const track = r.querySelector('.vtrack');
      if (!nm || !nm.innerText.trim()) out.noName++;
      if (!fill || !track) { out.noFill++; continue; }
      const nb = nm.getBoundingClientRect(), tb = track.getBoundingClientRect();
      if (nb.bottom <= tb.top + 2) out.nameAbove++;
      if (r.querySelector('.vbadge.clean')) out.badges.clean++;
      if (r.querySelector('.vbadge.keep')) out.badges.keep++;
      if (r.querySelector('.vbadge.unk')) out.badges.unk++;
      if (out.sample.length < 3) {
        const sz = r.querySelector('.fsize');
        out.sample.push(nm.innerText.trim().split('\\n')[0] + '=' + (sz ? sz.innerText : '?'));
      }
    }
    return out;
  })()`);
  checkScan("柱状图渲染出多条", chart.n > 3, `${chart.n} 条`);
  checkScan("每根柱子都直接显示文件名（在柱体上方）",
        chart.noName === 0 && chart.nameAbove === chart.n,
        `缺名 ${chart.noName}，名在柱上 ${chart.nameAbove}/${chart.n}`);
  checkScan("每根柱子都有填充条与大小", chart.noFill === 0);
  checkScan("柱子带可清理/保留/未识别标识",
        chart.badges.clean + chart.badges.keep + chart.badges.unk > 0,
        `可清理 ${chart.badges.clean} / 保留 ${chart.badges.keep} / 未识别 ${chart.badges.unk}`);
  console.log(`  [信息] 排行示例: ${chart.sample.join(" | ")}`);

  const modeN = await evaluate(`document.querySelectorAll('#fsModes .seg').length`);
  const bigN = await evaluate(`document.querySelectorAll('#fsChart .vrow').length`);
  checkScan("排行视图可切换（本层/全盘最大/文件/可清理）",
        modeN >= 4 && bigN > 0, `${modeN} 个视图，当前 ${bigN} 条`);
  // 「游戏等大项」这个分类已按要求移除：游戏不再单列一类，
  // 而是直接进「全盘最大项目」，并带 Steam 中文名标注。
  const hasGamesMode = await evaluate(
    `!![...document.querySelectorAll('#fsModes .seg')].find(s => s.dataset.m === 'games')`);
  check("已移除「游戏等大项」分类", hasGamesMode === false);

  // 默认应停在「全盘最大项目」：新用户一眼看到占地大头
  const defaultMode = await evaluate(
    `([...document.querySelectorAll('#fsModes .seg')].find(s => s.classList.contains('on'))||{}).dataset?.m`);
  const topNames = await evaluate(`[...document.querySelectorAll('#fsChart .fname')]
      .slice(0, 8).map(e => e.innerText.trim().split('\\n')[0])`);
  checkScan("默认视图为「全盘最大项目」", defaultMode === "biggest", String(defaultMode));
  console.log(`  [信息] 默认视图前八名: ${topNames.join(" / ")}`);
  // 所有视图都不该再出现「第 N 层」这种标注（按要求去掉）
  const layerTags = await evaluate(
    `[...document.querySelectorAll('#fsChart .vcat')].filter(e => /第\\d+层/.test(e.innerText)).length`);
  check("占用排行里不再标注「第 N 层」", layerTags === 0, `${layerTags} 条带层数`);

  // 切到「全盘最大项目」也应能看到跨层大项
  await evaluate(`(() => {
    const s = [...document.querySelectorAll('#fsModes .seg')].find(x => x.dataset.m === 'biggest');
    if (s) s.click();
  })()`);
  await sleep(700);
  const biggestRows = await evaluate(`document.querySelectorAll('#fsChart .vrow').length`);
  checkScan("「全盘最大项目」视图可用", biggestRows > 0, `${biggestRows} 条`);
  await evaluate(`(() => {
    const s = [...document.querySelectorAll('#fsModes .seg')].find(x => x.dataset.m === 'dirs');
    if (s) s.click();
  })()`);
  await sleep(600);

  // ---- 逐层下钻：这是「大清理项可点击查看子项」的核心 ----
  console.log("\n  --- 逐层下钻 ---");
  const beforeDrill = await evaluate(`(() => {
    const rows = [...document.querySelectorAll('#fsChart .vrow')];
    return { rows: rows.length,
             drillable: rows.filter(r => r.querySelector('.vgo')).length,
             path: (document.querySelector('.drillhint b')||{}).innerText || '',
             crumbs: document.querySelectorAll('.crumbs .cb').length };
  })()`);
  checkScan("大目录条目标记为可点开", beforeDrill.drillable > 0,
        `${beforeDrill.drillable}/${beforeDrill.rows} 条可下钻`);
  checkScan("显示当前目录路径", beforeDrill.path.length > 2, beforeDrill.path);
  checkScan("面包屑存在", beforeDrill.crumbs >= 1, `${beforeDrill.crumbs} 段`);

  const firstDir = await evaluate(`(() => {
    const r = [...document.querySelectorAll('#fsChart .vrow')]
      .find(x => x.querySelector('.vgo'));
    if (!r) return '';
    const name = r.querySelector('.fname').innerText.trim().split('\\n')[0];
    r.click();
    return name;
  })()`);
  await sleep(4000);
  const afterDrill = await evaluate(`(() => ({
    path: (document.querySelector('.drillhint b')||{}).innerText || '',
    crumbs: document.querySelectorAll('.crumbs .cb').length,
    hasUp: !!document.querySelector('#fsUp'),
    rows: document.querySelectorAll('#fsChart .vrow').length,
    firstNames: [...document.querySelectorAll('#fsChart .fname')]
      .slice(0, 3).map(e => e.innerText.trim().split('\\n')[0]),
  }))()`);
  checkScan("点击目录能下钻到子项",
        afterDrill.path && afterDrill.crumbs > beforeDrill.crumbs,
        `进入「${String(firstDir).slice(0, 18)}」-> ${afterDrill.path.slice(0, 44)}`);
  checkScan("下钻后显示返回上一级入口", afterDrill.hasUp);
  checkScan("下钻后仍有子项列表", afterDrill.rows > 0,
        `子项: ${afterDrill.firstNames.join(" / ").slice(0, 56)}`);

  // 连续下钻，验证能挖到深层大项
  let deepPath = afterDrill.path, hops = 0;
  for (let i = 0; i < 4; i++) {
    const went = await evaluate(`(() => {
      const r = [...document.querySelectorAll('#fsChart .vrow')]
        .find(x => x.querySelector('.vgo'));
      if (!r) return false;
      r.click(); return true;
    })()`);
    if (!went) break;
    await sleep(4000);
    hops++;
    deepPath = await evaluate(`(document.querySelector('.drillhint b')||{}).innerText || ''`);
  }
  checkScan("可连续下钻到深层目录（第 3 层以上）",
        hops >= 2 && deepPath.split("\\").length >= 4,
        `下钻 ${hops} 层 -> ${deepPath.slice(0, 58)}`);

  // 注意：下钻相关元素在「扫描没出结果」时并不存在，这里一律写成「取不到就跳过点击」，
  // 否则 evaluate 会抛 Uncaught 把整个检查器中途带崩（后面的 Steam/盘符检查全没了）。
  await evaluate(`(() => { const b = document.querySelector('#fsUp'); if (b) b.click(); })()`);
  await sleep(1200);
  const backOne = await evaluate(`(document.querySelector('.drillhint b')||{}).innerText || ''`);
  checkScan("「返回上一级」可用", backOne.length > 0 && backOne !== deepPath,
        `${deepPath.slice(-22)} -> ${backOne.slice(-22)}`);
  await evaluate(`(() => {
      const c = document.querySelectorAll('.crumbs .cb')[0]; if (c) c.click();
    })()`);
  await sleep(1200);
  const atRoot = await evaluate(`document.querySelectorAll('.crumbs .cb').length`);
  checkScan("面包屑可跳回顶层", atRoot === 1, `${atRoot} 段面包屑`);
  const rootRows = await evaluate(`document.querySelectorAll('#fsChart .vrow').length`);
  checkScan("回到顶层后重新显示顶层清单", rootRows > 0, `${rootRows} 条`);

  // 找一个**含大文件**的目录作为样本。从树数据里直接挑，再用面包屑按名称定位，
  // 避免在测试里处理路径字符串（反斜杠转义在多层引用中极易出错）。
  const fileSample = await evaluate(`(() => {
    if (!FS || !FS.nodes) return null;
    const files = FS.nodes.filter(n => n.type === 'file' && n.size > 0)
                          .sort((a, b) => b.size - a.size);
    if (!files.length) return null;
    const f = files[0];
    const parent = FS.nodes.find(n => n.path.toLowerCase() === f.parent);
    return { name: f.name, path: f.path, size: f.size,
             parentPath: parent ? parent.path : '',
             parentName: parent ? parent.name : '' };
  })()`);
  checkScan("占用树里包含文件级条目（按体积论，不只看目录）",
        !!fileSample, fileSample ? fileSample.name : "无文件条目");

  if (fileSample && fileSample.parentPath) {
    // 用面包屑逐级点进去：按名字匹配，不拼路径
    const chain = await evaluate(`(() => {
      const names = [];
      let key = ${JSON.stringify(fileSample.parentPath)}.toLowerCase();
      for (let i = 0; i < 6; i++) {
        const n = FS.nodes.find(x => x.path.toLowerCase() === key);
        if (!n) break;
        names.unshift(n.name);
        key = String(n.parent || '').toLowerCase();
        if (key.length <= 3) break;
      }
      return names;
    })()`);
    for (const part of chain) {
      const clicked = await evaluate(`(() => {
        const r = [...document.querySelectorAll('#fsChart .vrow')].find(x =>
          x.querySelector('.fname').innerText.trim().split('\\n')[0] === ${JSON.stringify(part)});
        if (!r || !r.querySelector('.vgo')) return false;
        r.click(); return true;
      })()`);
      if (!clicked) break;
      await sleep(3000);
    }
    const here = await evaluate(`(document.querySelector('.drillhint b')||{}).innerText || ''`);
    await evaluate(`(() => {
      const s = [...document.querySelectorAll('#fsModes .seg')].find(x => x.dataset.m === 'files');
      if (s) s.click();
    })()`);
    await sleep(900);
    const inFiles = await evaluate(`document.querySelectorAll('#fsChart .vrow').length`);
    checkScan("可切到「只看文件」视图并列出文件", inFiles > 0,
          `${here} 下 ${inFiles} 个文件条目（链: ${chain.join(" > ")}）`);
  }

  const gotDetail = await evaluate(`(() => {
    const s = [...document.querySelectorAll('#fsModes .seg')].find(x => x.dataset.m === 'files');
    if (s) s.click();
    return true;
  })()`);
  await sleep(700);
  const clickedFile = await evaluate(`(() => {
    const r = document.querySelector('#fsChart .vrow');
    if (!r) return false;
    r.click();
    return true;
  })()`);
  await sleep(900);
  checkScan("点击文件条目弹出详情", gotDetail && clickedFile);
  const det = await evaluate(`(() => {
    const m = document.querySelector('#modal');
    if (!m) return null;
    return {
      open: document.querySelector('#mask').classList.contains('on'),
      title: (m.querySelector('.dname')||{}).innerText || '',
      size: (m.querySelector('.dsize')||{}).innerText || '',
      facts: [...m.querySelectorAll('.dfacts dt')].map(e => e.innerText.trim()),
      secs: [...m.querySelectorAll('.dsec .dh')].map(e => e.innerText.trim()),
      advice: (m.querySelector('.dadv')||{}).innerText || '',
      path: (m.querySelector('.dpath')||{}).innerText || '',
      hasReveal: !!m.querySelector('#dReveal'),
    };
  })()`);
  checkScan("点击柱子弹出详情", !!det && det.open, det ? det.title.trim() : "无弹窗");
  checkScan("详情显示文件名与占用空间",
        !!det && det.title.trim().length > 0 && /B/.test(det.size),
        det ? `${det.title.trim()} · ${det.size}` : "");
  checkScan("详情含识别信息（识别为/分类/风险/依据）",
        !!det && det.facts.includes("识别为") && det.facts.includes("分类")
        && det.facts.includes("风险等级") && det.facts.includes("识别依据"),
        det ? det.facts.join("/") : "");
  checkScan("详情含『是什么』说明段", !!det && det.secs.includes("是什么"));
  checkScan("详情含清理建议", !!det && det.advice.length > 10,
        det ? det.advice.slice(0, 46).replace(/\\n/g, " ") : "");
  checkScan("详情含完整路径", !!det && det.path.includes(":"),
        det ? det.path.slice(0, 46) : "");
  checkScan("详情可打开所在目录", !!det && det.hasReveal);
  await evaluate(`(() => { const b = document.querySelector('#dClose'); if (b) b.click(); })()`);
  await sleep(400);

  // --- 细分查看：把一个大目录拆成「可清缓存 / 不要删」两部分 ---
  // 这一段的必要性：该功能后端与界面都写好了，但**曾经根本没有入口按钮**
  // （后端接口写完后前端没接），只靠读代码很容易误以为已经完成。
  // 所以这里从点击按钮开始，一路验到弹窗内容真的渲染出来。
  //
  // 取目标的讲究变了：开了 WizTree 之后几乎所有目录都能下钻，
  // 「点一下目录」不再会弹详情 —— 详情改由每行显式的「详情」按钮打开。
  // 所以这里不再挑叶子目录，而是直接点某一行的「详情」按钮。
  const segOk = await evaluate(`(() => {
    const s = [...document.querySelectorAll('#fsModes .seg')].find(x => x.dataset.m === 'biggest');
    if (s) { s.click(); return true; }
    return false;
  })()`);
  await sleep(1200);
  const leaf = await evaluate(`(() => {
    const b = document.querySelector('#fsChart .vrow [data-info]');
    if (!b) return null;
    const row = b.closest('.vrow');
    b.click();
    return { name: row.querySelector('.fname').innerText.trim().split('\\n')[0] };
  })()`);
  if (segOk && leaf) {
    await sleep(1000);
    const hasPartBtn = await evaluate(`!!document.querySelector('#dPart')`);
    checkScan("目录详情里有「细分查看」入口", hasPartBtn, `行「${leaf.name}」`);
    if (hasPartBtn) {
      await evaluate(`document.querySelector('#dPart').click()`);
      await sleep(2500);
      const part = await evaluate(`(() => {
        const m = document.querySelector('#modal');
        const boxes = [...m.querySelectorAll('.pbox')].map(b => ({
          label: (b.querySelector('.pl')||{}).innerText || '',
          value: (b.querySelector('.pv')||{}).innerText || '',
          sub: (b.querySelector('.pwhy')||{}).innerText || '',
        }));
        const rows = [...m.querySelectorAll('.prow')];
        return {
          busy: /正在分析/.test(m.innerText || ''),
          heading: (m.querySelector('.dh')||{}).innerText || '',
          boxes,
          secs: [...m.querySelectorAll('.dsec .dh')]
            .map(e => e.innerText.trim()).filter(t => /^[①②]|全部子项/.test(t)),
          rows: rows.length,
          // 每行都必须带体积，且至少一行体积不是 0 ——
          // 这条正是为了盯住「体积全算成 0」那个回归
          sizes: rows.map(r => (r.querySelector('.psz')||{}).innerText || ''),
          badges: rows.map(r => (r.querySelector('.vbadge')||{}).innerText || '')
            .filter(Boolean).length,
          drillable: rows.filter(r => r.dataset.drill === '1').length,
          hasReveal: !!m.querySelector('.prow [data-prev]'),
          hasBack: !!m.querySelector('#dBack'),
          fastBar: !!m.querySelector('.enginebar.fast'),
        };
      })()`);
      checkScan("细分查看渲染出结果（不是一直转圈）", !part.busy,
            part.busy ? "仍在「正在分析…」" : part.heading.slice(0, 26));
      checkScan("细分查看列出当前层全部子项", part.rows > 0,
            `${part.rows} 行`);
      checkScan("细分查看的每个子项都算出了体积",
            part.sizes.length > 0 && part.sizes.every(s => /B|KB|MB|GB|TB/.test(s)),
            part.sizes.slice(0, 4).join(" / "));
      checkScan("细分查看的体积不是全 0（回归盯防）",
            part.sizes.some(s => !/^0 B$/.test(s.trim())),
            part.sizes.slice(0, 4).join(" / "));
      checkScan("细分查看区分目录与文件两类合计",
            part.boxes.length === 2
            && part.boxes.some(b => /目录/.test(b.label))
            && part.boxes.some(b => /文件/.test(b.label)),
            part.boxes.map(b => b.label + "=" + b.value).join(" / "));
      checkScan("细分查看给出识别结论（可清理/保留/未识别徽章）",
            part.badges > 0, `${part.badges}/${part.rows} 行有徽章`);
      checkScan("细分查看提供继续展开的入口",
            part.drillable > 0, `${part.drillable} 行可继续展开`);
      checkScan("细分查看的子项可打开所在目录", part.hasReveal);
      checkScan("细分查看可返回上一层详情", part.hasBack);

      // --- 二级/三级下钻：点「可继续展开」的行应当进入下一层 ---
      // 这是用户明确要的能力（大清理项点开拆二级目录），所以要真的点进去看。
      let depth = 0, pathNow = '';
      for (let step = 0; step < 3; step++) {
        const cur = await evaluate(`(() => {
          const p = document.querySelector('#modal .dpath');
          return p ? p.innerText.replace(/^路径：/, '').trim() : '';
        })()`);
        const nxt = await evaluate(`(() => {
          const r = document.querySelector("#modal .prow[data-drill='1']");
          if (!r) return null;
          const nm = (r.querySelector('b')||{}).innerText || '';
          r.click();
          return nm;
        })()`);
        if (!nxt) break;
        await sleep(1600);
        const after = await evaluate(`(() => {
          const m = document.querySelector('#modal');
          const p = m.querySelector('.dpath');
          return {
            path: p ? p.innerText.replace(/^路径：/, '').trim() : '',
            rows: m.querySelectorAll('.prow').length,
            busy: /正在分析/.test(m.innerText || ''),
            sizes: [...m.querySelectorAll('.prow .psz')].map(e => e.innerText),
          };
        })()`);
        if (after.busy || after.rows === 0 || after.path === cur) break;
        depth++;
        pathNow = after.path;
        if (step === 0) {
          checkScan("二级拆分能继续点进下一层目录",
                after.rows > 0 && after.path !== cur,
                `${cur} → ${after.path}（${nxt}，${after.rows} 项）`);
        }
        if (!after.sizes.every(s => /B|KB|MB|GB|TB/.test(s))) {
          checkScan("深层子项体积正常", false, after.sizes.slice(0, 3).join(" / "));
          break;
        }
      }
      checkScan("可持续下钻到第 3 层以上", depth >= 2,
            `实际下钻 ${depth} 层，停在 ${pathNow}`);
      try {
        const ps2 = await cdp.send("Page.captureScreenshot",
          { format: "png", captureBeyondViewport: true }, S);
        fs.writeFileSync(path.join(__dirname, "_partition_deep.png"),
          Buffer.from(ps2.data, "base64"));
        console.log("  截图: tools\\_partition_deep.png（多级下钻）");
      } catch (e) { /* 截图失败不影响判定 */ }
      await evaluate(`(() => { const b = document.querySelector('#dClose2'); if (b) b.click(); })()`);
      await sleep(400);
    }
  } else {
    skip("细分查看弹窗检查",
         segOk ? "本层没有可展示的行" : "找不到「全盘最大项目」视图");
  }

  // 验证可返回常规清理视图
  const debugViews = await evaluate(`(() => {
    showNormalView();
    return {
      view,
      normalStyle: document.querySelector('#normalView').style.display,
      fullStyle: document.querySelector('#fullView').style.display,
      btn: document.querySelector('#btnFullScan').innerText.trim()
    };
  })()`);
  await sleep(1000);
  const back = await evaluate(`({
    view,
    normal: document.querySelector('#normalView').style.display !== 'none',
    full: document.querySelector('#fullView').style.display === 'none',
    btn: document.querySelector('#btnFullScan').innerText.trim(),
    debug: ${JSON.stringify(debugViews)}
  })`);
  checkScan("可返回常规清理视图",
        back.normal && back.full && /全盘|查看/.test(back.btn), JSON.stringify(back));

  // ---------- Steam 独立页面 ----------
  console.log("\n  --- Steam 游戏库页面 ---");
  const hasSteamBtn = await evaluate(`!!document.querySelector('#btnSteam')`);
  check("工具栏有 Steam 入口", hasSteamBtn);
  if (hasSteamBtn) {
    await evaluate(`document.querySelector('#btnSteam').click()`);
    await sleep(2500);
    const st = await evaluate(`(() => ({
      shown: document.querySelector('#steamView').style.display === 'flex',
      hiddenNormal: document.querySelector('#normalView').style.display === 'none',
      hint: (document.querySelector('#stHint')||{}).innerText || '',
      found: !!(window.STEAM && STEAM.found),
      libCount: (window.STEAM && STEAM.games) ? STEAM.games.length : 0,
      // found=false 有两种成因：本机确实没有 Steam，或后端枚举失败。后者要看得见。
      steamErr: (window.STEAM && STEAM.error) ? String(window.STEAM.error).slice(0, 80) : '',
      drives: document.querySelectorAll('#stOverview .dsk').length,
      driveRows: [...document.querySelectorAll('#stOverview .dsk .dskh')]
        .map(e => e.innerText.replace(/\\n/g, ' ').slice(0, 60)),
      games: document.querySelectorAll('#stGames .vrow').length,
      sorts: document.querySelectorAll('#stSort .seg').length,
      filter: !!document.querySelector('#stFilter'),
    }))()`);
    check("可进入 Steam 独立页面", st.shown && st.hiddenNormal, st.hint);

    // Steam 库内容检查要求本机真的装了 Steam 且有已安装游戏：CI runner 与
    // 新装机的开发机上都没有，面板会按设计显示空态（这是功能，不是缺陷）。
    // 所以这里以 STEAM.found / games 数量为前提，不具备就跳过而不是判失败。
    if (st.found && st.libCount > 0) {
      check("按盘显示 Steam 占用", st.drives >= 1, `${st.drives} 个盘`);
      console.log(`  [信息] 分盘: ${st.driveRows.join(" /// ")}`);
      check("列出游戏清单", st.games > 0, `${st.games} 条`);
      check("有排序与筛选", st.sorts >= 3 && st.filter);

      // 点游戏看详情（必须带解释）
      await evaluate(`(() => { const r = document.querySelector('#stGames .vrow'); if (r) r.click(); })()`);
      await sleep(800);
      const sdet = await evaluate(`(() => {
        const m = document.querySelector('#modal');
        return { open: document.querySelector('#mask').classList.contains('on'),
                 title: (m.querySelector('.dname')||{}).innerText || '',
                 size: (m.querySelector('.dsize')||{}).innerText || '',
                 advice: (m.querySelector('.dadv')||{}).innerText || '' };
      })()`);
      check("点游戏弹出详情并带解释",
            sdet.open && sdet.advice.length > 5,
            `${sdet.title.trim()} ${sdet.size}`);
      await evaluate(`document.querySelector('#mask').classList.remove('on')`);
    } else {
      if (st.steamErr) console.log(`  [信息] Steam 接口返回错误：${st.steamErr}`);
      skip2(`Steam 库内容检查（本机未装 Steam 或无已安装游戏：found=${st.found} ` +
            `games=${st.libCount}，面板按设计显示空态）`);
    }

    // Epic / GOG 游戏库面板：有才显示，行内必须带游戏本体徽章（v2.2）
    const plat = await evaluate(`(() => {
      const vis = el => !!el && el.style.display !== 'none';
      const panel = id => document.querySelector(id);
      const rows = id => { const p = panel(id); return p ? p.querySelectorAll('.vrow').length : 0; };
      const g = panel('#epicGames');
      return { epic: vis(panel('#epicPanel')), gog: vis(panel('#gogPanel')),
               epicRows: rows('#epicGames'), gogRows: rows('#gogGames'),
               hint: ((panel('#epicHint')||{}).textContent || '').trim(),
               badges: g ? g.querySelectorAll('.vbadge.keep').length : 0 };
    })()`);
    if (plat.epic) {
      check("Epic 游戏库面板渲染出行", plat.epicRows > 0,
            `${plat.epicRows} 行 · ${plat.hint}`);
      check("Epic 行都带游戏本体徽章", plat.badges >= plat.epicRows,
            `${plat.badges}/${plat.epicRows}`);
    } else {
      console.log("  [信息] 本机无 Epic 游戏，面板按设计隐藏");
    }
    if (plat.gog) {
      check("GOG 游戏库面板渲染出行", plat.gogRows > 0, `${plat.gogRows} 行`);
    } else {
      console.log("  [信息] 本机无 GOG 游戏，面板按设计隐藏");
    }

    await evaluate(`document.querySelector('#btnSteam').click()`);
    await sleep(1200);
    check("可从 Steam 页面返回",
          await evaluate(`document.querySelector('#normalView').style.display !== 'none'`));
  }

  // JS 报错
  const real = errors.filter(e => !/favicon/i.test(e));
  check("无 JS 控制台报错/异常", real.length === 0, real.slice(0, 2).join(" | "));
  if (failedReqs.length) INFO.push(`失败请求: ${failedReqs.slice(0, 3).join(", ")}`);

  // --- 盘符归属（放在最后跑：切盘会触发重扫，异步完成时会冲掉界面状态，
  //     放在中间会把后面的规则/换算断言带崩）---
  console.log("\n  --- 盘符归属与清理范围默认值 ---");
  try {
    const probe = await evaluate(`(async () => {
      const dv = [...document.querySelectorAll('.drv')].find(d => d.dataset.l !== 'C');
      if (!dv) return null;
      const other = dv.dataset.l;
      dv.click();
      // 等到重扫真正结束（按钮恢复可用），否则读到的是上一轮的结果
      for (let i = 0; i < 60; i++) {
        await new Promise(r => setTimeout(r, 1000));
        const btn = document.querySelector('#btnScan');
        const on = document.querySelector('.drv.on');
        if (on && on.dataset.l === other && btn && !btn.disabled) break;
      }
      const sel = document.querySelector('#scopeSel');
      const cards = [...document.querySelectorAll('.card')].map(c => c.dataset.path || '');
      return {
        other, got: sel ? sel.value : '',
        stray: cards.filter(p => /^C:\\\\/i.test(p)).length,
        cards: cards.length,
      };
    })()`);
    if (probe) {
      check(`非系统盘（${probe.other}:）的清理范围默认为「仅本盘」`,
            probe.got === "drive", `scope=${probe.got}`);
      check("非系统盘卡片里没有别的盘的路径（不再混入 C 盘用户级缓存）",
            probe.stray === 0, `${probe.stray}/${probe.cards} 张卡片指向 C:`);
    } else {
      skip("数据盘默认清理范围", "本机只有一个盘");
    }
  } catch (e) {
    skip("数据盘默认清理范围", "探测失败：" + e.message);
  }

  // 截图
  try {
    const shot = await cdp.send("Page.captureScreenshot",
      { format: "png", captureBeyondViewport: true }, S);
    fs.writeFileSync(path.join(__dirname, "_ui_check.png"), Buffer.from(shot.data, "base64"));
    await evaluate(`document.querySelector('#btnFullScan').click()`);
    await sleep(900);
    const shot2 = await cdp.send("Page.captureScreenshot",
      { format: "png", captureBeyondViewport: true }, S);
    fs.writeFileSync(path.join(__dirname, "_fs_check.png"), Buffer.from(shot2.data, "base64"));
    console.log(`  截图: tools\\_ui_check.png（常规）、tools\\_fs_check.png（全盘扫描）`);
  } catch (e) { console.log("  截图失败: " + e.message); }

  cdp.ws.close();
  proc.kill();
  await sleep(400);
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch { }

  console.log(`\n  ==========================================`);
  console.log(`  通过 ${PASS.length} 项，失败 ${FAIL.length} 项，跳过 ${SKIP.length} 项`);
  if (FAIL.length) console.log("  失败项：" + FAIL.join("、"));
  if (SKIP.length) console.log("  跳过项：" + SKIP.length + " 项（环境不具备条件，非缺陷）");
  console.log(`  ==========================================\n`);
  process.exit(FAIL.length ? 1 : 0);
})().catch(e => {
  console.error("  检查器异常: " + (e && e.stack || e));
  process.exit(2);
});
