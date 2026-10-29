/* 原文标注的请求重试、刷新恢复与连点保护。不把原文或标签写入浏览器持久存储。 */
"use strict";
const element = id => document.getElementById(id);
const token = document.querySelector('meta[name="relevance-csrf"]').content;
const labels = {left:"A 更相关", right:"B 更相关", tie:"相当", neither:"都不相关", skip:"跳过", uncertain:"无法判断", withdraw:"已撤销"};
let state = null, current = null, editing = false, saved = false, busy = false, pending = null;
function status(text, error=false) { element("status").textContent = text; element("status").className = error ? "error" : ""; }
function lock(value) { busy=value; document.querySelectorAll("button").forEach(button => {button.disabled=value;}); }
async function api(path, body) {
  const response = await fetch(path, {method:body ? "POST" : "GET", cache:"no-store", headers:body ? {"Content-Type":"application/json", "X-Relevance-CSRF":token} : {}, body:body ? JSON.stringify(body) : undefined});
  const data = await response.json();
  if (!response.ok) {const error = new Error(typeof data.detail === "string" ? data.detail : "请求校验失败"); error.code=response.status; throw error;}
  return data;
}
function render() {
  current=(editing || saved) ? state.previous : state.current;
  element("progress").textContent=`已标 ${state.completed} / ${state.total} 条`;
  element("coverage").textContent=state.coverage.warnings.map(w=>w.message).join(" ");
  element("previous").hidden=editing || !state.previous;
  element("back").hidden=!editing;
  element("next").hidden=!saved;
  element("decisions").hidden=saved;
  element("task").hidden=!current;
  element("empty").hidden=Boolean(current);
  if (!current) return;
  element("query").textContent=`查询：${current.query}`;
  element("scope").textContent=Object.keys(current.scope).length ? `范围：${JSON.stringify(current.scope)}` : "范围：未限定";
  element("material").textContent=current.material === "synthetic" ? "合成演示：原创虚构材料，不是你的真实资料库。" : "实际语料：由显式配置的接口或导入文件提供。";
  for (const side of ["left", "right"]) {
    const c=current[side];
    for (const [id,key] of [["title","article_title"],["heading","heading"],["before","context_before"],["text","text"],["after","context_after"]]) element(`${side}-${id}`).textContent=c[key];
    element(`${side}-source`).href=c.source_url;
  }
  element("prior").textContent=current.decision ? `上次判定：${labels[current.decision]}。修订 ${current.revision}` : "";
  element("withdraw").hidden=!editing || !current.decision || current.decision === "withdraw";
}
async function reload() {state=await api("/api/relevance/state"); render();}
async function decide(decision) {
  if (busy || saved || !current) return;
  // 网络结果不确定时保留原请求 ID；再次点击只能重试原操作，不能误标下一条。
  const body=pending || {presentation_id:current.presentation_id,decision,expected_revision:current.revision,request_id:crypto.randomUUID()};
  pending=body; lock(true);
  try {
    await api("/api/relevance/labels", body); pending=null; editing=false; saved=body.decision !== "withdraw";
    await reload(); status(saved ? "已保存。核对后点“下一条”继续，也可纠正上一条。" : "已撤销，可以重新标注。");
  } catch(error) {
    if (error.code && error.code < 500) {pending=null; await reload().catch(()=>{});}
    status(`${error.message}。${pending ? "再次点任一判定将重试刚才的操作；刷新也可从服务端恢复。" : "请核对当前条目后重试。"}`, true);
  } finally {lock(false);}
}
document.querySelectorAll("[data-decision]").forEach(button => button.addEventListener("click",()=>decide(button.dataset.decision)));
element("previous").addEventListener("click",()=>{if(!busy){pending=null;saved=false;editing=true;render();}});
element("back").addEventListener("click",()=>{if(!busy){pending=null;saved=false;editing=false;render();}});
element("next").addEventListener("click",()=>{if(!busy){pending=null;saved=false;editing=false;render();}});
const generate=element("generate");
if (generate) generate.addEventListener("submit",async event=>{
  event.preventDefault(); if(busy) return; lock(true);
  try {await api("/api/relevance/generate",{query:element("query-input").value,scope:{}});editing=false;saved=false;await reload();status("候选已进入冻结任务队列。");}
  catch(error){status(error.message,true);}finally{lock(false);}
});
reload().catch(error=>status(error.message,true));
