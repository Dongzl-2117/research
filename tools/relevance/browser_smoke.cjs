/* 可选浏览器验收：只创建临时合成库并启动回环端口，不访问业务库/模型。 */
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const {spawn, spawnSync} = require("node:child_process");
const {chromium} = require("playwright-core");
const root = path.resolve(__dirname, "../..");
const python = process.env.RELEVANCE_PYTHON || "python";
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function run() {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "relevance-browser-"));
  const db = path.join(dir, "synthetic.sqlite3");
  const env = {...process.env, PYTHONPATH: path.join(root, "src")};
  const demo = spawnSync(python, ["-m", "researchlib.relevance", "demo", "--db", db], {cwd:root, env, encoding:"utf8"});
  assert.equal(demo.status, 0, demo.stderr);
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0, "127.0.0.1", resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const url = `http://127.0.0.1:${port}`;
  const server = spawn(python, ["-m", "researchlib.relevance", "serve", "--db", db,
    "--annotator-id", "synthetic-browser-check", "--port", String(port)], {cwd:root, env, stdio:["ignore","pipe","pipe"]});
  let output="", browser;
  server.stdout.on("data", chunk => {output+=chunk;});
  server.stderr.on("data", chunk => {output+=chunk;});
  try {
    let ready=false;
    for (let attempt=0; attempt<100; attempt++) {
      if (server.exitCode !== null) throw new Error(output);
      try {ready=(await fetch(`${url}/annotate`)).ok;} catch {}
      if (ready) break;
      await pause(100);
    }
    assert(ready, output);
    browser=await chromium.launch({executablePath:process.env.RELEVANCE_CHROMIUM || undefined, headless:true});
    const page=await browser.newPage({viewport:{width:1100,height:950}});
    const errors=[];
    page.on("pageerror", error=>errors.push(error.message));
    await page.goto(`${url}/annotate`);
    await page.locator("#task").waitFor({state:"visible"});
    const state=()=>page.evaluate(()=>fetch("/api/relevance/state").then(r=>r.json()));
    const events=()=>page.evaluate(async()=>{
      const lines=(await (await fetch("/api/relevance/export")).text()).trim().split("\n").map(JSON.parse);
      return lines.filter(row=>row.record_type==="label_event").map(row=>row.data);
    });
    const first=(await state()).current;
    assert(first.left.context_before && first.right.context_after);
    await page.reload(); await page.locator("#task").waitFor({state:"visible"});
    assert.deepEqual((await state()).current, first);
    const popupWait=page.waitForEvent("popup");
    await page.locator("#left-source").click();
    const popup=await popupWait; await popup.waitForLoadState();
    assert((await popup.locator("body").innerText()).includes("原创合成材料"));
    await popup.close();
    // 同步连点，以及保存后延迟到达的第二次点击，均不得标到下一任务。
    await page.locator('[data-decision="left"]').evaluate(button=>{button.click();button.click();});
    await page.locator("#next").waitFor({state:"visible"});
    await page.locator('[data-decision="left"]').evaluate(button=>button.click());
    assert.equal((await events()).length,1);
    assert.equal((await state()).completed,1);
    await page.locator("#next").click();
    await page.locator("#previous").click();
    assert.equal(await page.locator("#query").textContent(),`查询：${first.query}`);
    for (const decision of ["tie","neither"]) {
      await page.locator(`[data-decision="${decision}"]`).click();
      await page.locator("#next").waitFor({state:"visible"});
      const history=await events();
      const latest=history.reduce((a,b)=>a.revision>b.revision?a:b);
      assert.equal(latest.decision,decision); assert.equal(latest.winner_id,null);
      await page.locator("#previous").click();
    }
    await page.locator("#withdraw").click();
    await page.waitForFunction(()=>document.getElementById("status").textContent.includes("已撤销"));
    assert.equal((await state()).completed,0);
    assert.equal((await state()).current.presentation_id,first.presentation_id);
    assert.equal((await state()).current.revision,4);
    await page.locator('[data-decision="right"]').click();
    await page.locator("#next").waitFor({state:"visible"});
    await page.locator("#next").click();
    await page.locator('[data-decision="uncertain"]').click();
    await page.locator("#next").waitFor({state:"visible"});
    const restored=(await state()).current;
    await page.reload(); await page.locator("#task").waitFor({state:"visible"});
    assert.deepEqual((await state()).current,restored);
    assert.equal((await state()).completed,2);
    await page.setViewportSize({width:390,height:844});
    assert.equal((await events()).length,6);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    if (process.env.RELEVANCE_SCREENSHOT) await page.screenshot({path:process.env.RELEVANCE_SCREENSHOT,fullPage:true});
    assert.deepEqual(errors,[]);
    console.log("浏览器验收通过：来源、刷新恢复、连点保护、tie/neither、改判、最新修订撤销、移动端布局；6 个审计事件。");
  } finally {
    if(browser) await browser.close();
    if (server.exitCode === null) {
      const exited=new Promise(resolve=>server.once("exit",resolve));
      server.kill("SIGTERM");
      await exited;
    }
    await fs.rm(dir,{recursive:true,force:true});
  }
}
run().catch(error=>{console.error(error);process.exitCode=1;});
