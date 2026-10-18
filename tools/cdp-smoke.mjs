// CDP 冒烟：连本机 9333 隔离 Edge，开 :8831 页面，真实键入式查询 北京->上海，断言行渲染+截图。
// 用法: node tools/cdp-smoke.mjs
import { writeFileSync } from "node:fs";

const CDP = "http://127.0.0.1:9333";
const PAGE = "http://127.0.0.1:8831";
const results = [];
const ok = (name, cond, extra = "") => {
  results.push({ name, pass: !!cond, extra });
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  — " + extra : ""}`);
};

async function newTab() {
  const r = await fetch(`${CDP}/json/new?${encodeURIComponent(PAGE)}`, { method: "PUT" });
  return r.json();
}

const tab = await newTab();
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

let mid = 0;
const pending = new Map();
const consoleErrors = [];
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  if (msg.method === "Runtime.exceptionThrown")
    consoleErrors.push(msg.params.exceptionDetails?.text || "exception");
};
function send(method, params = {}) {
  return new Promise((resolve) => {
    const id = ++mid;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
}
async function evl(expr) {
  const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error("页面 JS 异常: " + JSON.stringify(r.result.exceptionDetails.text));
  return r.result?.result?.value;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

await send("Runtime.enable");
await send("Page.enable");
await sleep(2500); // 等页面加载 + 车站表拉取

ok("页面标题", (await evl("document.title")).includes("车次票价查询"));
const stationCount = await evl("fetch('/api/stations').then(r=>r.json()).then(b=>b.data.length)");
ok("车站表加载", stationCount > 3000, `${stationCount} 条`);

// 模拟用户输入（触发 autocomplete 路径）并直接赋状态后点查询
await evl(`(() => {
  const set = (id, v) => {
    const el = document.getElementById(id);
    el.value = v;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  };
  set("fromInput", "北京");
  set("toInput", "上海");
  document.getElementById("dateInput").value = "2026-10-20";
  document.getElementById("dateInput").dispatchEvent(new Event("change", { bubbles: true }));
})()`);
await sleep(300);
const acVisible = await evl(`!!document.querySelector(".ac-list")`);
ok("自动补全下拉出现", acVisible);

await evl(`document.getElementById("queryBtn").click()`);
// 轮询等真实 12306 数据渲染（最多 40s）
let rows = 0, firstText = "";
for (let i = 0; i < 40; i++) {
  await sleep(1000);
  rows = await evl(`document.querySelectorAll("#tbody tr").length`);
  const skel = await evl(`!!document.querySelector(".skel")`);
  const err = await evl(`(document.querySelector(".error-box span")||{}).textContent || ""`);
  if (err) { firstText = "ERROR: " + err; break; }
  if (rows > 0 && !skel) {
    firstText = await evl(`document.querySelector("#tbody tr").textContent`);
    break;
  }
}
ok("查询返回车次行", rows > 0, `${rows} 行`);
ok("首行含车次/席别/价格", /G|D|K|T|Z|\d/.test(firstText) && firstText.includes("¥"), firstText.slice(0, 60));
const summary = await evl(`document.getElementById("summary").textContent`);
ok("汇总栏显示路线", summary.includes("北京") && summary.includes("上海"), summary.slice(0, 40));

// 车型筛选联动（点「G」只剩高铁）
await evl(`[...document.querySelectorAll("#catChips .chip")].find(c => c.textContent === "G").click()`);
await sleep(200);
const allG = await evl(`[...document.querySelectorAll("#tbody .train-cat")].every(e => e.textContent === "G")`);
const gRows = await evl(`document.querySelectorAll("#tbody tr").length`);
ok("车型筛选 G", allG && gRows > 0, `${gRows} 行全为 G`);

// 交换按钮
await evl(`document.getElementById("swapBtn").click()`);
const swapped = await evl(`document.getElementById("fromInput").value + "->" + document.getElementById("toInput").value`);
ok("⇄ 交换出发到达", swapped === "上海->北京", swapped);

ok("页面无 JS 异常", consoleErrors.length === 0, consoleErrors.join("; ").slice(0, 120));

// 截图存档
const shot = await send("Page.captureScreenshot", { format: "png" });
writeFileSync(new URL("../docs/smoke-8831.png", import.meta.url), Buffer.from(shot.result.data, "base64"));
console.log("截图已存 docs/smoke-8831.png");

// 只关自己开的 tab，不动共享浏览器
await fetch(`${CDP}/json/close/${tab.id}`);
ws.close();
const failed = results.filter((r) => !r.pass).length;
console.log(`\n${results.length - failed}/${results.length} 项通过`);
process.exit(failed ? 1 : 0);
