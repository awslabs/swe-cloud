/* SWE-Cloud front page. Renders the sections from the files in data/. */
(function(){
"use strict";
const THEMES=[
  {id:"aws",name:"AWS console",tag:"current",info:"The site's current default view (Cloudscape): dark top bar, white rounded containers, counters in headers."},
  {id:"classic",name:"Classic",tag:"current",info:"The site's current classic view: warm paper, light serif headlines, mono labels, centred sections."},
  {id:"docs",name:"Docs",info:"Documentation site: a sidebar that follows the component order, one content column, callouts."}
];
const FIXED_TOP="mast", FIXED_BOTTOM="cite";
const MOVABLE=["how","leaderboard","ground","dist","pipeline","qa"];
const LABEL={mast:"Name and slogan",how:"How the tasks work",leaderboard:"Leaderboard",ground:"How the tasks are grounded",dist:"What the tasks cover",pipeline:"How to run this benchmark",qa:"Q&A",cite:"Citation"};
const VARIANTS={
  mast:[["A","Centred"],["B","Split with stat tiles"],["C","Compact bar"]],
  how:[["A","Two storyboards"],["B","Tabbed storyboard"],["C","Side-by-side steps"]],
  leaderboard:[["A","Table"],["B","Compact rows"],["C","Table with cost bars"]],
  ground:[["A","Two flows"],["B","Source to task"],["C","Fact cards"]],
  dist:[["A","Formal taxonomy"],["B","Systems terms"],["C","Plain engineering"]],
  pipeline:[["A","Timeline, Harbor folded"],["B","Timeline, Harbor shown"],["C","Timeline, terminal blocks"]],
  qa:[["A","Accordion"],["B","Card grid"],["C","List and answer"]],
  cite:[["A","BibTeX"],["B","BibTeX or text"],["C","Compact"]]
};

/* Leaderboard rows come from data/leaderboard.json (see loadData). */
let MODELS=[], LB={};
const VIEWS=[{id:"ALL",name:"Overall (100)"},{id:"IF",name:"Issue fixing (60)"},{id:"PC",name:"Program construction (40)"}];
let QA=[], BIB="", PLAIN="";

/* ---------- State: layout comes from data/site.json ---------- */
let order=[], variants={aws:{}}, hidden=new Set();
const clean=true;
let view="ALL", howTab="IF", qaSel=0, citeTab="bib";
const theme="aws";
const V=c=>variants[theme][c]||"A";

/* ---------- Helpers ---------- */
const f3=v=>v.toFixed(3), pct=v=>Math.round(v*100)+"%";
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const PROMPT='<path class="lp" d="M23 21 L30 26.5 L23 32"/><path class="lp" d="M34 33 H42"/>';
function logo(dark){return `<span class="logo${dark?" dk":""}" role="img" aria-label="SWE-Cloud"><svg viewBox="0 0 64 46" aria-hidden="true"><path class="lc" d="M17 42 H49 A11 11 0 0 0 51.5 20.3 A16 16 0 0 0 21 14.5 A13.8 13.8 0 0 0 17 42 Z"/>${PROMPT}</svg><span aria-hidden="true">SWE-Cloud</span></span>`}
const agentIcon=`<span class="agent" aria-hidden="true"><svg viewBox="16 14 32 24">${PROMPT}</svg></span>`;
const icFolder='<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1.5 3.5h4l1.5 1.5h7.5v8h-13z" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>';
const icFile='<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 1.5h6l3 3v10h-9z M9.5 1.5v3h3" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>';
const icLock='<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4.5" y="10.5" width="15" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>';
const icPatch='<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="4" cy="3.5" r="1.8" fill="none" stroke="currentColor" stroke-width="1.3"/><circle cx="4" cy="12.5" r="1.8" fill="none" stroke="currentColor" stroke-width="1.3"/><circle cx="12" cy="6" r="1.8" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M4 5.3v5.4M12 7.8c0 3-4 2-7 3.4" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>';
const icTest='<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 1.5h4M6.8 1.5v4.2L2.8 13a1 1 0 0 0 .9 1.5h8.6a1 1 0 0 0 .9-1.5L9.2 5.7V1.5" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>';
const lines=arr=>arr.map(w=>`<i class="ln" style="width:${w}%"></i>`).join("");

/* IF box 1: the files in /app, the AWS services each one calls, and what cdk/app.py creates. */
function ifWire(){
  const files=["api.py","worker.py","store.py","cdk/app.py"], svcs=["SQS","DynamoDB","S3"], fy=i=>14+i*26, sy=i=>20+i*32;
  const calls=[[0,0],[1,0],[1,1],[2,1],[2,2]];
  return `<div class="win"><div class="win-h">${icFolder}/app <span style="font-weight:400;color:var(--muted)">and the AWS it calls</span></div><svg class="wire" viewBox="0 0 260 110" role="img" aria-label="api.py calls SQS; worker.py calls SQS and DynamoDB; store.py calls DynamoDB and S3; cdk/app.py creates all three">
    ${svcs.map((s,i)=>`<path class="cdk" d="M96 ${fy(3)} C 150 ${fy(3)}, 150 ${sy(i)}, 176 ${sy(i)}"/>`).join("")}
    ${calls.map(([f,s])=>`<path d="M96 ${fy(f)} C 140 ${fy(f)}, 140 ${sy(s)}, 176 ${sy(s)}"/>`).join("")}
    ${files.map((f,i)=>`<text class="f" x="8" y="${fy(i)+4}">${f}</text>`).join("")}
    ${svcs.map((s,i)=>`<g><rect class="s" x="176" y="${sy(i)-10}" width="76" height="20" rx="4"/><text class="st" x="214" y="${sy(i)+4}" text-anchor="middle">${s}</text></g>`).join("")}
  </svg><div class="wire-k"><span><i></i>calls</span><span><i class="d"></i>cdk/app.py creates</span></div></div>`;
}
/* ---------- Storyboards: each task type from the agent's side, not any one task ---------- */
const STORY={
  IF:{title:"Issue fixing",n:"60 tasks",budget:"1 h per attempt",
    blurb:"Repair a production incident in a service that runs on AWS.",
    graded:"Hidden tests replay crashes, retries and failovers, then read the AWS state.",
    steps:[
      {cap:"Given the code of an AWS-backed service",sub:"and a description of the incident",html:
       `${ifWire()}<div class="win imd"><div class="win-h">${icFile}instruction.md</div><div class="imd-b"><span class="h">## Incident</span>Some jobs run twice after a worker restarts. On-call saw it in production; there is no stack trace.<span class="h">## Task</span>Fix the service in /app so a restart never processes a job twice.</div></div>`},
      {cap:"the agent reproduces it and finds the cause",sub:"often by probing the AWS services the code uses",html:
       `<div class="term"><span class="p">$</span> python repro.py --kill-worker-after-ack
<span class="o">job-41 processed 2×</span>  <span class="tx">expected 1</span></div>
        <div class="files">
        <div class="fcard"><span class="fn">worker.py</span>${lines([80,55])}<i class="ln bugl" style="width:70%"></i><span class="bug">acks before the write lands</span></div>
        <div class="fcard"><span class="fn">store.py</span>${lines([65])}<i class="ln bugl" style="width:50%"></i><span class="bug">write is not conditional</span></div></div>
        <div class="seq"><span>lease expires</span><em>→</em><span class="x">worker killed</span><em>→</em><span>redelivered</span><em>→</em><span class="x">2nd write</span></div>`},
      {cap:"then submits a fix",sub:"hidden tests replay the failure and inject crashes",html:
       `<div class="win"><div class="win-h">${icPatch}Fix</div><div class="rows"><div><span>worker.py</span><span><span class="add">+38</span> <span class="del">−12</span></span></div><div><span>store.py</span><span><span class="add">+14</span> <span class="del">−3</span></span></div></div>
        <div class="diffl"><span class="d">− queue.ack(msg); table.put(job)</span><span class="a">+ table.put(job, if_absent=True)</span><span class="a">+ queue.ack(msg)</span></div></div>
        <div class="win"><div class="win-h">${icTest}Hidden tests</div><div class="rows"><div><span>kill worker mid-write</span><span class="okc">✓</span></div><div><span>redeliver after timeout</span><span class="okc">✓</span></div><div><span>stale writer after failover</span><span class="okc">✓</span></div><div><span>burst of duplicate deliveries</span><span class="okc">✓</span></div></div></div>
        <span class="pfoot">Pass only if every test passes</span>`}
    ]},
  PC:{title:"Program construction",n:"40 tasks",budget:"4 h per attempt",
    blurb:"Rebuild a real program that runs on AWS from a binary it can run but not read.",
    graded:"Hidden tests run the agent's program on fresh AWS resources and compare its behavior with the reference.",
    steps:[
      {cap:"Given a program it can run but not read",sub:"and asked to build a replica of it in /app",html:
       `<div class="win"><div class="lockbin">${icLock}<div><b>/reference/program</b><small>run it as often as needed; source withheld</small></div></div></div>
        <div class="win"><div class="win-h">${icFolder}/docs <span style="font-weight:400;color:var(--muted)">3 interface guides</span></div></div>
        <div class="win imd"><div class="win-h">${icFile}instruction.md</div><div class="imd-b"><span class="h">## Task</span>Build a replica of /reference/program in /app. Match its commands, output, exit codes and what it stores in AWS.<span class="h">## Deliverable</span><code>build.sh</code> must produce <code>/app/bin/program</code>.</div></div>`},
      {cap:"the agent runs it to learn its behavior",sub:"then reimplements it in a language of its choice",html:
       `<div class="term"><span class="p">$</span> /reference/program put jobs/7 --ttl 60
<span class="o">ok version=1</span>
<span class="p">$</span> aws dynamodb get-item --key jobs/7
<span class="o">{"v": 1, "ttl": 1795000060}</span>
<span class="p">$</span> /reference/program put jobs/7
<span class="o">error: conflict (exit 3)</span></div>
        <div class="win"><div class="win-h">${icFolder}/app <span style="font-weight:400;color:var(--muted)">(written by the agent)</span></div><div class="rows"><div><span>main.go</span><span class="add">+412</span></div><div><span>store.go</span><span class="add">+268</span></div><div><span>retry.go</span><span class="add">+96</span></div><div><span>build.sh</span><span class="add">+9</span></div></div></div>`},
      {cap:"build.sh builds its executable",sub:"then hidden tests compare it with the reference",html:
       `<ol class="gsteps3"><li><b>Build</b><span><code>build.sh</code> turns /app into <code>/app/bin/program</code></span></li><li><b>Run</b><span>each test runs <code>/app/bin/program</code> on fresh AWS</span></li><li><b>Compare</b><span>output, exit codes and AWS state must match the reference's behavior</span></li></ol>
        <div class="win"><div class="win-h">${icTest}Hidden tests</div><div class="rows tests"><div><span>test_cli</span><span>10/10 <span class="okc">✓</span></span></div><div><span>test_s3_objects</span><span>12/12 <span class="okc">✓</span></span></div><div class="bad"><span>test_dynamodb_items</span><span>12/13 <span class="badc">✕</span></span></div><div><span>test_sqs_messages</span><span>8/8 <span class="okc">✓</span></span></div></div></div>
        <span class="pfoot">A pass needs every test to pass</span>`}
    ]}
};
function storyRow(k,numbered){
  const s=STORY[k];
  return `<div class="sb">${s.steps.map((st,i)=>`${i?'<div class="sb-arrow" aria-hidden="true">→</div>':""}<div class="sb-step"><div class="pn">${st.html}</div><div class="cap">${numbered?`<span class="sbnum">${i+1}</span>`:""}${st.cap}<small>${st.sub}</small></div></div>`).join("")}</div>`;
}
function storyStack(k){
  const s=STORY[k];
  return `<div class="vstack">${s.steps.map((st,i)=>`${i?'<div class="sb-arrow" aria-hidden="true">↓</div>':""}<div class="cap"><span class="sbnum">${i+1}</span>${st.cap}<small>${st.sub}</small></div><div class="pn">${st.html}</div>`).join("")}</div>`;
}
const dLegend=`<div class="dlegend"><span>File names and values are illustrative, not from a specific task.</span></div>`;
const dLegendPC=`<div class="dlegend"><span style="display:block">File names and values are illustrative, not from a specific task. The task format follows earlier rebuild-from-binary benchmarks such as <a href="https://arxiv.org/abs/2605.03546">ProgramBench</a>.</span></div>`;

/* ---------- Emulated AWS lane under each storyboard: what is in AWS at each step ---------- */
const icCloud='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 18.5h10.5a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.6 9.1 4.7 4.7 0 0 0 7 18.5z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>';
const awsChips=a=>a.map(s=>`<span class="aws">${s}</span>`).join("");
const LANE={
  IF:[`<b>Deployed from cdk/app.py</b><div class="lc">${awsChips(["SQS jobs","DynamoDB jobs","S3 results"])}</div><small>the service runs against these, as in production</small>`,
      `<b>What the agent can inspect</b><div class="kv"><span>SQS jobs · job-41</span><span class="x">received 2×</span></div><div class="kv"><span>DynamoDB jobs · job-41</span><span class="x">2 items</span></div>`,
      `<b>What the tests validate</b><div class="kv"><span>SQS · in flight</span><span class="okc">0 ✓</span></div><div class="kv"><span>DynamoDB · job-41</span><span class="okc">1 item ✓</span></div>`],
  PC:[`<b>Fresh resources for the reference</b><div class="lc">${awsChips(["S3 bucket","DynamoDB table","SQS queue"])}</div><small>created per run, test credentials</small>`,
      `<b>What the reference left behind</b><div class="kv"><span>S3 · data/</span><span>4 objects</span></div><div class="kv"><span>DynamoDB · jobs/7</span><span>v=1, ttl set</span></div>`,
      `<b>Compared after each test</b><div class="kv"><span>S3 objects</span><span class="okc">4 = 4 ✓</span></div><div class="kv"><span>DynamoDB items</span><span class="x">ttl missing ✕</span></div>`]};
/* One grid for boxes, captions and the AWS lane, so every row lines up across the three steps. */
function storyGrid(k){
  const s=STORY[k], gap=i=>i?'<div class="sb-arrow" aria-hidden="true">→</div>':"", sp=i=>i?"<div></div>":"";
  return `<div class="sbg sbg-${k}">${s.steps.map((st,i)=>gap(i)+`<div class="pn">${st.html}</div>`).join("")}${s.steps.map((st,i)=>sp(i)+`<div class="cap">${st.cap}<small>${st.sub}</small></div>`).join("")}
    <div class="lane-h">${icCloud}<b>Emulated AWS</b><span>at each step, inside the sandbox</span></div>${LANE[k].map((x,i)=>sp(i)+`<div class="lane-c">${x}</div>`).join("")}</div>`;
}

/* ---------- Component renderers ---------- */
function head(id,sub){
  let h="";
  if(theme==="classic") h+=`<span class="comp-pre">SWE-Cloud · ${LABEL[id]}</span>`;
  let title=LABEL[id];
  if(theme==="aws"&&id==="leaderboard") title+=` <span class="cnt">(${MODELS.length})</span>`;
  h+=`<h2 class="comp-h">${title}${theme==="docs"?'<a class="anchor" href="#docs" aria-hidden="true" tabindex="-1">#</a>':""}</h2>`;
  if(sub) h+=`<p class="comp-sub">${sub}</p>`;
  return h;
}

function rMast(){
  const v=V("mast");
  const slogan="Can coding agents fix and build programs that run on AWS?";
  const lede="Agents resolve production incidents in AWS-backed services, inspired by real incident reports, or rebuild programs modeled on real open-source projects. Each task runs in a sandbox with emulated AWS services and no internet access.";
  const stats=[["100","Tasks"],["60","Issue Fixing"],["40","Program Construction"],["23","AWS Services"]];
  const st=`<div class="mh-stats">${stats.map(s=>`<div class="mh-stat"><b class="num">${s[0]}</b><span>${s[1]}</span></div>`).join("")}</div>`;
  const ctas=`<div class="ctas"><a class="cta pri" href="tasks.html#if">Browse Task Gallery</a><button class="cta" data-go="leaderboard">Leaderboard</button><a class="cta" href="faq.html">Q&amp;A</a><span class="cta">Paper <span class="soon">soon</span></span><a class="cta" href="https://github.com/awslabs/swe-cloud">Code</a></div>`;
  if(v==="B") return `<div class="mh-B"><div>${logo()}<p class="mh-slogan">${slogan}</p><p class="mh-lede">${lede}</p>${ctas}</div>${st}</div>`;
  if(v==="C") return `<div class="mh-C"><div class="top">${logo()}<p class="mh-slogan">${slogan}</p></div><div class="strip"><span><b>100</b> tasks</span><span><b>60</b> issue fixing</span><span><b>40</b> program construction</span><span><b>22</b> AWS services</span><span><b>pass@3</b>, one harness</span></div>${theme==="docs"?`<p class="callout">Results use the reference protocol: one agent harness, three attempts per task, 1 h and 4 h budgets.</p>`:""}${ctas}</div>`;
  return `<div class="mh-A">${logo()}<p class="mh-slogan">${slogan}</p><p class="mh-lede">${lede}</p>${st}${ctas}</div>`;
}

function rHow(){
  const v=V("how");
  const sub="Two kinds of task. Each starts from what the agent is given and ends with how the result is judged.";
  if(v==="B"){
    const s=STORY[howTab];
    return head("how",sub)+`<div class="seg" role="group" aria-label="Task type">${["IF","PC"].map(k=>`<button data-how="${k}" aria-pressed="${k===howTab}">${STORY[k].title}</button>`).join("")}</div>
      <div class="in-B"><div>${storyRow(howTab,true)}</div><aside class="side-sum"><h3>${s.title}</h3><p style="margin:0;color:var(--muted)">${s.blurb}</p><dl><dt>Tasks</dt><dd>${s.n}</dd><dt>Budget</dt><dd>${s.budget}</dd><dt>Attempts</dt><dd>3 per model</dd></dl><p style="margin:12px 0 0;font-size:13px;color:var(--muted)">${s.graded}</p></aside></div>${dLegend}`;
  }
  if(v==="C"){
    return head("how",sub)+`<div class="in-C">${["IF","PC"].map(k=>`<div><div class="colh"><h3>${STORY[k].title}</h3><p>${STORY[k].blurb} ${STORY[k].n}, ${STORY[k].budget}.</p></div>${storyStack(k)}</div>`).join("")}</div>${dLegend}`;
  }
  const k=howTab;
  const seg=`<div class="seg how-seg" role="group" aria-label="Task type">${["IF","PC"].map(t=>`<button data-how="${t}" aria-pressed="${t===k}">${STORY[t].title} (${STORY[t].n.split(" ")[0]})</button>`).join("")}</div>`;
  const body=`<div class="hv-wrap">${storyGrid(k)}</div>`;
  return head("how",sub)+seg+`<div class="sbrow"><div class="sblab"><h3>${STORY[k].title}</h3><p>${STORY[k].blurb}</p><div class="meta"><span>${STORY[k].n}</span><span>${STORY[k].budget}</span></div></div>${body}</div>`+(k==="PC"?dLegendPC:dLegend);
}

function sorted(k){k=k||view;const key=k==="ALL"?"SC":k;return MODELS.slice().sort((a,b)=>(b[key]??-1)-(a[key]??-1))}
const segLB=()=>`<div class="seg" role="group" aria-label="Leaderboard view">${VIEWS.map(v=>`<button data-view="${v.id}" aria-pressed="${v.id===view}">${v.name}</button>`).join("")}</div>`;
const PHNOTE=()=>`<span class="phnote">${LB.placeholder?"<b>Placeholder scores and costs.</b> ":""}${LB.attempts} attempts per task. <a href="#c-pipeline" data-go="pipeline">How to run it</a></span>`;
const HARNESS="Terminus 2";
const TBDC=`<td class="r num tbd">TBD</td>`;
const p1=v=>(v*100).toFixed(1)+"%", usd=v=>"$"+v.toFixed(2);
function lbFoot(){const parts=[];
  if(view==="ALL")parts.push(`* <b>SWE-Cloud Score</b> = w<sub>IF</sub> × issue-fixing pass@1 + w<sub>PC</sub> × program-construction pass@1, where w is each task type's proportion of all tasks.`);
  if(view==="PC")parts.push(`* <b>almost@1</b>: share of attempts that pass at least ${Math.round(LB.score.almost_threshold*100)}% of the hidden tests.`);
  if(view!=="ALL")parts.push("<b>pass@1</b> (dark bar): mean success rate over three attempts per task. <b>pass@3</b> (light bar): share of tasks solved in at least one of the three.");
  parts.push("<b>Cost</b>: estimated API cost per attempt in USD, at list prices from a pinned LiteLLM price table.");
  parts.push(`† <b>Terminus 2</b>: Harbor's Terminus 2 plus our bug fixes, in <a href="https://github.com/awslabs/swe-cloud/blob/main/src/swecloud/agents/terminus.py"><code>src/swecloud/agents/terminus.py</code></a>.`);
  return `<p class="lbfoot">${parts.join(" ")}</p>`}
function wtBadge(r){return r.w==="Open"?(r.hf?`<a class="wt open" href="${r.hf}" target="_blank" rel="noopener" title="Hugging Face page">Open weights <span aria-hidden="true">↗</span></a>`:`<span class="wt open">Open weights</span>`):`<span class="wt">Closed</span>`}
const mName=r=>`${esc(r.m)} <span class="eff">(${r.eff})</span>`;
function scoreCell(r,wide){const max=1;if(r.SC==null)return TBDC;return `<td class="r"><div class="cellbar${wide?" wide":""}"><span class="num">${p1(r.SC)}</span><span class="bar"><i style="width:${(r.SC/max*100).toFixed(1)}%"></i></span></div></td>`}
function duoCell(r,wide){const k=view,max=k==="PC"?.6:1;if(r[k]==null)return TBDC;return `<td class="r"><div class="cellbar duo${wide?" wide":""}"><span class="num">${p1(r[k])} <small>/ ${p1(r[k+"3"])}</small></span><span class="bar"><i class="l" style="width:${Math.min(100,r[k+"3"]/max*100).toFixed(1)}%"></i><i style="width:${(r[k]/max*100).toFixed(1)}%"></i></span></div></td>`}
function costCell(r,bars){const k="c"+view,v=r[k];if(v==null)return TBDC;if(!bars)return `<td class="r num">${usd(v)}</td>`;const mx=Math.max(...MODELS.map(x=>x[k]));return `<td class="r"><div class="cellbar cost"><span class="num">${usd(v)}</span><span class="bar"><i style="width:${(v/mx*100).toFixed(1)}%"></i></span></div></td>`}
function rLeaderboard(){
  const v=V("leaderboard");
  const fn=(t,s="*")=>`<span class="fn" tabindex="0" data-tip="${t}" aria-label="${t}">${s}</span>`;
  const HH=`<th>Harness${fn("Harbor's Terminus 2 plus our bug fixes (src/swecloud/agents/terminus.py)","†")}</th>`;
  const mainH=view==="ALL"?"SWE-Cloud Score"+fn(`SWE-Cloud Score = w_IF × issue-fixing pass@1 + w_PC × program-construction pass@1, where w is each task type's proportion of all tasks`):"pass@1 / pass@3";
  const almostH=view==="PC"?`<th class="r">almost@1${fn(`almost@1: share of attempts that pass at least ${Math.round(LB.score.almost_threshold*100)}% of the hidden tests`)}</th>`:"";
  const almostC=r=>view==="PC"?(r.AL==null?TBDC:`<td class="r num">${p1(r.AL)}</td>`):"";
  const main=(r,wide)=>view==="ALL"?scoreCell(r,wide):duoCell(r,wide);
  const top=`<div class="lb-top">${segLB()}<div class="lb-tabs">${PHNOTE()}${modeSeg()}</div></div>`;
  if(lbMode==="chart") return head("leaderboard","")+top+lbChart()+chartFoot();
  if(v==="B"){
    return head("leaderboard","")+top+`<div class="tblwrap"><table class="lb lbB"><thead><tr><th>#</th><th>Model (effort)</th><th>Weights</th>${HH}<th class="r sel">${mainH}</th>${almostH}<th class="r">Cost / attempt</th></tr></thead><tbody>${sorted().map((r,i)=>`<tr${i<3?` class="podium p${i+1}"`:""}><td class="rk"><span class="rkb num">${i+1}</span></td><td class="md2"><div class="nm">${mName(r)}</div><div class="sub">${esc(r.lab)}</div></td><td>${wtBadge(r)}</td><td class="lab">${HARNESS}</td>${main(r,true)}${almostC(r)}${r["c"+view]==null?TBDC:`<td class="r num">${usd(r["c"+view])}</td>`}</tr>`).join("")}</tbody></table></div>`+lbFoot();
  }
  const bars=v==="C";
  return head("leaderboard","")+top+`<div class="tblwrap"><table class="lb"><thead><tr><th>#</th><th>Model (effort)</th><th>Lab</th><th>Weights</th>${HH}<th class="r sel">${mainH}</th>${almostH}<th class="r">Cost / attempt</th></tr></thead><tbody>${sorted().map((r,i)=>`<tr><td class="rk num">${i+1}</td><td class="md">${mName(r)}</td><td class="lab">${esc(r.lab)}</td><td>${wtBadge(r)}</td><td class="lab">${HARNESS}</td>${main(r)}${almostC(r)}${costCell(r,bars)}</tr>`).join("")}</tbody></table></div>`+lbFoot();
}

/* Categories regrouped from the 100 release tasks (tasks/SOURCES.md): every IF and PC task is assigned to exactly one subcategory.
   Each label has three wordings (variants A, B, C); counts are identical across variants. Services from the preview site's task cards. */
let COV={};
const tsum=t=>t[1].reduce((a,x)=>a+x[1],0);
const OP=[1,.72,.5,.32];
const TINT=[0,30,50,66], shade=(c,j)=>j?`color-mix(in srgb,var(--${c}) ${100-TINT[j]}%,#fff)`:`var(--${c})`;
function covSet(v){}
function arc(cx,cy,r0,r1,a0,a1){const L=a1-a0>Math.PI?1:0,p=(r,a)=>[(cx+r*Math.sin(a)).toFixed(2),(cy-r*Math.cos(a)).toFixed(2)];const [x0,y0]=p(r1,a0),[x1,y1]=p(r1,a1),[x2,y2]=p(r0,a1),[x3,y3]=p(r0,a0);return `M${x0} ${y0}A${r1} ${r1} 0 ${L} 1 ${x1} ${y1}L${x2} ${y2}A${r0} ${r0} 0 ${L} 0 ${x3} ${y3}Z`}
function donut(inner,outer,total,label,aria){
  const T=inner.reduce((a,x)=>a+x.v,0);let a=0,h="";const g=.012;
  inner.forEach(x=>{const d=x.v/T*Math.PI*2;h+=`<path d="${arc(100,100,outer?50:54,outer?74:90,a+g,a+d-g)}" style="fill:var(--${x.c});opacity:${x.o||1}"><title>${x.l}: ${x.v}</title></path>`;a+=d});
  if(outer){a=0;outer.forEach(x=>{const d=x.v/T*Math.PI*2;h+=`<path d="${arc(100,100,77,92,a+g,a+d-g)}" style="fill:var(--${x.c});opacity:${x.o}"><title>${x.l}: ${x.v}</title></path>`;a+=d})}
  return `<svg class="donut" viewBox="0 0 200 200" role="img" aria-label="${aria}">${h}<text x="100" y="100" text-anchor="middle" class="dn">${total}</text><text x="100" y="118" text-anchor="middle" class="dl">${label}</text></svg>`;
}
/* Rounded ring (design P5-G): rounded annular sectors with parallel-sided gaps. */
function rArc(cx,cy,r0,r1,a0,a1,c,gap){
  const P=(r,a)=>(cx+r*Math.sin(a)).toFixed(2)+" "+(cy-r*Math.cos(a)).toFixed(2);
  const h=gap/2,b0=a0+h/r1,b1=a1-h/r1,i0=a0+h/r0,i1=a1-h/r0;
  if(b1<=b0||i1<=i0)return "";
  c=Math.max(0,Math.min(c,(r1-r0)/2-.01,(i1-i0)*r0/2-.01));
  const oc=c/r1,ic=c/r0,Lo=(b1-b0-2*oc)>Math.PI?1:0,Li=(i1-i0-2*ic)>Math.PI?1:0;
  return `M${P(r1,b0+oc)}A${r1} ${r1} 0 ${Lo} 1 ${P(r1,b1-oc)}Q${P(r1,b1)} ${P(r1-c,b1)}L${P(r0+c,i1)}Q${P(r0,i1)} ${P(r0,i1-ic)}A${r0} ${r0} 0 ${Li} 0 ${P(r0,i0+ic)}Q${P(r0,i0)} ${P(r0+c,i0)}L${P(r1-c,b0)}Q${P(r1,b0)} ${P(r1,b0+oc)}Z`;
}
/* items: [{l,v,f,gap}] — f is the fill, gap (px) the gap before the item */
function ring(items,total,label,aria,base){
  const r0=80,r1=94,T=items.reduce((a,x)=>a+x.v,0);let a=0,h=`<circle class="trk" cx="100" cy="100" r="${(r0+r1)/2}" stroke-width="${r1-r0}"/>`;
  items.forEach((x,k)=>{const d=x.v/T*Math.PI*2,nx=items[(k+1)%items.length],g0=(x.gap??base)/2,g1=(nx.gap??base)/2;
    h+=`<path d="${rArc(100,100,r0,r1,a+(g0-base/2)/r1,a+d-(g1-base/2)/r1,5,base)}" style="fill:${x.f}"><title>${esc(x.l)}: ${x.v}</title></path>`;a+=d});
  return `<svg class="donut ring" viewBox="0 0 200 200" role="img" aria-label="${aria}">${h}<text x="100" y="100" text-anchor="middle" class="dn">${total}</text><text x="100" y="118" text-anchor="middle" class="dl">${label}</text></svg>`;
}
function themeDonut(k){const items=[];COV[k].forEach((t,i)=>t[1].forEach((x,j)=>items.push({l:t[0]+" · "+x[0],v:x[1],f:shade("c"+(i+1),j),gap:j===0?7:2.5})));return ring(items,k==="IF"?60:40,"tasks",(k==="IF"?"Issue-fixing":"Program-construction")+" tasks by category and subcategory",2.5)}
function themeLegend(k){return `<ul class="clg">${COV[k].map((t,i)=>`<li><span class="sw" style="background:var(--c${i+1})"></span><b>${t[0]}</b><span class="num cn">${tsum(t)}</span><small>${t[1].map((x,j)=>`<span title="${x[2]||""}"><span class="sw s" style="background:${shade("c"+(i+1),j)}"></span>${x[0]} ${x[1]}</span>`).join("<br>")}</small></li>`).join("")}</ul>`}
const flatLegend=arr=>`<ul class="clg flat">${arr.map((x,i)=>`<li><span class="sw" style="background:${x[2]||`var(--c${i+1})`}"></span>${x[0]}<span class="num cn">${x[1]}</span></li>`).join("")}</ul>`;
const COVTXT={
  IF:["Issue fixing: what breaks","60 tasks, grouped by the kind of production failure the agent has to fix."],
  PC:["Program construction: what gets rebuilt","40 tasks, grouped by the kind of software the agent rebuilds."],
  SVC:["AWS services covered","23 services across all 100 tasks, counted from each task's tags and emulated locally."]
};
function covCell(k,body){return `<div class="covc"><h3>${COVTXT[k][0]}</h3><p>${COVTXT[k][1]}</p>${body}</div>`}
function rDist(){
  const v=V("dist"), sub="What kinds of failures and programs the 100 tasks are about, and which AWS services they use.";
  covSet(v);
  const top=6, svcTop=COV.SVC.slice(0,top), svcRest=COV.SVC.slice(top), restN=svcRest.reduce((a,x)=>a+x[1],0), smax=28;
  let cells;
  if(false){
    const bars=k=>{const T=COV[k],max=Math.max(...T.map(tsum));return `<div class="cbars">${T.map((t,i)=>`<div class="cbr"><div class="cbh"><b>${t[0]}</b><span class="num">${tsum(t)}</span></div><div class="cbt" style="width:${tsum(t)/max*100}%">${t[1].map((x,j)=>`<i style="flex:${x[1]};background:var(--c${i+1});opacity:${OP[j]}" title="${x[0]}: ${x[1]}"></i>`).join("")}</div><small>${t[1].map(x=>`${x[0]} ${x[1]}`).join(" · ")}</small></div>`).join("")}</div>`};
    cells=[covCell("IF",bars("IF")),covCell("PC",bars("PC")),
      covCell("SVC",`<div class="hbl svc2">${COV.SVC.slice(0,10).map(x=>`<div><span>${x[0]}</span><i style="width:${x[1]/smax*100}%;background:var(--c1)"></i><b class="num">${x[1]}</b></div>`).join("")}</div><p class="more">Also: ${COV.SVC.slice(10).map(x=>x[0]).join(", ")}.</p>`)];
  } else if(false){
    cells=[covCell("IF",themeLegend("IF")),covCell("PC",themeLegend("PC")),
      covCell("SVC",`<div class="stiles">${COV.SVC.map(x=>`<span class="${x[1]/smax>.5?"dk":""}" style="flex-grow:${x[1]};--w:${(.12+.88*x[1]/smax).toFixed(2)}" title="${x[0]}: ${x[1]} tasks"><b>${x[0]}</b><em class="num">${x[1]}</em></span>`).join("")}</div><p class="more">Tile size and shade follow the number of tasks.</p>`)];
  } else {
    cells=[covCell("IF",`<div class="dwrap">${themeDonut("IF")}${themeLegend("IF")}</div>`),covCell("PC",`<div class="dwrap">${themeDonut("PC")}${themeLegend("PC")}</div>`),
      covCell("SVC",`<div class="dwrap">${ring(svcTop.map((x,i)=>({l:x[0],v:x[1],f:i<5?`var(--c${i+1})`:shade("c1",3)})).concat([{l:svcRest.length+" more services",v:restN,f:"var(--cg)"}]),23,"services","Task uses per AWS service",4)}${flatLegend(svcTop.map((x,i)=>[x[0],x[1],i<5?`var(--c${i+1})`:shade("c1",3)]).concat([[svcRest.length+" more",restN,"var(--cg)"]]))}<div class="svcmore">${svcRest.map(x=>`<span>${esc(x[0])} <b class="num">${x[1]}</b></span>`).join("")}</div><p class="more">A task counts once for each service it uses.</p></div>`)];
  }
  return head("dist",sub)+`<div class="cov">${cells.join("")}</div>`;
}

/* How the tasks are grounded: grounding of IF and PC tasks (facts from the public FAQ). */
const icDoc='<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 1.5h6l3 3v10h-9z M9.5 1.5v3h3 M5.5 8h5 M5.5 10.5h5" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>';
const icRepo='<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 2.5h9.5v9H4.5a1.5 1.5 0 0 0 0 3h8M3 2.5v10.5" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>';
const icGear='<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="2.2" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4" stroke="currentColor" stroke-width="1.3"/></svg>';
const GROUND={
  IF:{title:"Issue fixing",lead:"Every task is modelled on a real failure that was reported, fixed or analysed in public.",
    steps:[
      {h:"Seed: a real incident report",ic:icDoc,p:"An issue, pull request or fix in an open-source project on AWS, or a published design doc, postmortem or paper.",chips:["issue","pull request","postmortem","paper"]},
      {h:"Keep the mechanism, not the name",ic:icGear,p:"Borrow the failure mechanism and how it connects to the AWS services, while anonymizing the actual project to avoid memorization.",chips:["kept: mechanism","kept: AWS services"]},
      {h:"A compact service to repair",ic:icFolder,p:"Rewritten into a self-contained Python service with an incident report and hidden tests that require agents to fix the issue properly.",chips:["/app","incident report","hidden tests"]}
    ]},
  PC:{title:"Program construction",lead:"Every reference is a real open-source program that people run against AWS.",
    steps:[
      {h:"Seed: a real open-source program",ic:icRepo,p:"A pinned release of an open-source program that talks to AWS through public APIs, one per project.",chips:["pinned release","recorded commit","one per project"]},
      {h:"Built from source, offline",ic:icGear,p:"Built by us from upstream source, offline, into a single binary (most statically linked) that keeps its license.",chips:["source build","single binary","license kept"]},
      {h:"A reference the agent can run, not read",ic:icLock,p:"The agent gets an execute-only binary and interface guides, never the source. Hidden tests come from experiments on the reference.",chips:["execute-only","interface guides","hidden tests"]}
    ]}
};
function gMock(k,i){
  if(k==="IF"){
    if(i===0)return `<div class="win"><div class="win-h">${icDoc}Issue · open-source project</div><div class="gm"><b>Jobs run twice after a worker restart</b><div class="glabels"><span>bug</span><span>aws</span><span class="ok">fix merged</span></div>${lines([90,70,55])}</div></div>`;
    if(i===1)return `<div class="win"><div class="win-h">${icGear}Failure mechanism</div><div class="rows"><div><span>ack before write</span><span class="okc">kept</span></div><div><span>lease expiry, redelivery</span><span class="okc">kept</span></div><div><span>project name</span><span class="badc">anonymized</span></div></div></div>`;
    return `<div class="win"><div class="win-h">${icFolder}Task</div><ul class="tree"><li>${icFile}app/worker.py</li><li>${icFile}app/store.py</li><li>${icFile}incident.md</li><li>${icLock}tests/ <span style="color:var(--muted)">(hidden)</span></li></ul></div>`;
  }
  if(i===0)return `<div class="win"><div class="win-h">${icRepo}Upstream project</div><div class="gm"><b>A key-value store CLI for DynamoDB</b><div class="glabels"><span>tag v2.3.1</span><span>commit 9f3c2e1</span><span>Go</span></div>${lines([85,60])}</div></div>`;
  if(i===1)return `<div class="term"><span class="p">$</span> build --offline --commit 9f3c2e1
<span class="o">→ program  linux/x86-64</span>
<span class="o">→ LICENSE, NOTICE</span></div>`;
  return `<div class="win"><div class="lockbin">${icLock}<div><b>/reference/program</b><small>run, not read</small></div></div><ul class="tree" style="border-top:1px solid var(--line)"><li>${icFile}docs/interface.md</li><li>${icLock}tests/ <span style="color:var(--muted)">(hidden)</span></li></ul></div>`;
}
function rGround(){
  const v=V("ground"), sub="Every task is built from a real, public source: an issue or pull request in an open-source project, a published design, an engineering article or postmortem, or a released open-source program. The sources are all listed in <a href=\"https://github.com/awslabs/swe-cloud/blob/main/tasks/SOURCES.md\"><code>tasks/SOURCES.md</code></a>.";
  const note=`<p class="more">Example titles, tags and commits in the panels are illustrative.</p>`;
  if(v==="B"){
    return head("ground",sub)+`<div class="gwrap">`+["IF","PC"].map(k=>`<div class="grow"><div class="glab"><h3>${GROUND[k].title}</h3><p>${GROUND[k].lead}</p></div><div class="sb">${GROUND[k].steps.map((st,i)=>`${i?'<div class="sb-arrow" aria-hidden="true">→</div>':""}<div class="sb-step"><div class="pn" style="min-height:0">${gMock(k,i)}</div><div class="cap">${st.h}<small>${st.chips.join(" · ")}</small></div></div>`).join("")}</div></div>`).join("")+`</div>`+note;
  }
  if(v==="C"){
    const big={IF:["51 of 60","modelled on a failure reported or fixed in an open-source project that runs on AWS"],PC:["40 of 40","rebuilt from a released open-source program, one program per project"]};
    const rowsC={IF:[["Kept","The failure mechanism and the AWS behaviour behind it"],["Changed","Code rewritten as a compact Python service"],["Not named","The project, in the task's own files (its source is listed in tasks/SOURCES.md)"]],PC:[["Kept","The program's full observable behaviour, outputs, errors and stored state"],["Changed","Built by us from source, offline, into one binary"],["Hidden","The source code; the agent can only run the binary"]]};
    return head("ground",sub)+`<div class="gcards">${["IF","PC"].map(k=>`<div class="gcard"><span class="eyebrow">${GROUND[k].title}</span><div class="gbig"><b class="num">${big[k][0]}</b><span>${big[k][1]}</span></div><dl class="gkch">${rowsC[k].map(r=>`<dt>${r[0]}</dt><dd>${r[1]}</dd>`).join("")}</dl></div>`).join("")}</div>`;
  }
  return head("ground",sub)+`<div class="gflows">${["IF","PC"].map(k=>`<div class="gflow"><h3>${GROUND[k].title}</h3><ol class="gsteps">${GROUND[k].steps.map((st,i)=>`<li><span class="gic">${st.ic}</span><div><h4>${st.h}</h4><p>${st.p}</p><div class="gchips">${st.chips.map(c=>`<span>${c}</span>`).join("")}</div></div></li>`).join("")}</ol></div>`).join("")}</div>`;
}

/* "How to run this benchmark": one setup we use. Other models, providers and sandboxes go through the swe-cloud-run skill. */
const RUN={tab:"swecloud"};
const EX={model:"deepseek/deepseek-v4.1-flash",name:"DeepSeek V4.1 Flash"};
const RUNCMD={
  setup:["git clone https://github.com/awslabs/swe-cloud && cd swe-cloud","git lfs install && git lfs pull","uv sync --locked && source .venv/bin/activate","cp .env.example .env   # set OPENROUTER_API_KEY"],
  run:[`swecloud check --profile openrouter --model ${EX.model} --effort high --egress`,`swecloud run --profile openrouter --split issue-fixing --model ${EX.model} --effort high --n-concurrent 4 --job-name my-run`],
  harbor:["set -a && source .env && set +a","export AIOHTTP_SO_KEEPALIVE=true AIOHTTP_TCP_KEEPIDLE=30 AIOHTTP_TCP_KEEPINTVL=30 AIOHTTP_TCP_KEEPCNT=5 HARBOR_TELEMETRY=0",`harbor run --path \"$PWD/tasks/issue-fixing\" --job-name my-run --jobs-dir \"$PWD/jobs\" --env docker --agent swecloud.agents.terminus:Terminus2 --model openrouter/${EX.model} --n-attempts 3 --max-retries 1 --timeout-multiplier 1.0 --n-concurrent 4 --ak record_terminal_session=false --ak reasoning_effort=high --ak 'llm_kwargs={"timeout":1200,"max_tokens":128000}' --yes`],
  score:["swecloud score jobs/my-run"]
};
const cmdBlock=(lines,cls)=>`<div class="cmd${cls?" "+cls:""}"><pre>${lines.map(l=>`<span class="cl">${l}</span>`).join("")}</pre><button class="cpy" data-cmdcopy type="button" aria-label="Copy command">Copy</button></div>`;
function rPipeline(){
  const v=V("pipeline");
  const hb=`<details class="hbx"${v==="B"?" open":""}><summary>Same run with <code>harbor run</code></summary>${cmdBlock(RUNCMD.harbor)}<p class="cnote"><code>swecloud run</code> builds the same <code>harbor run</code> command; add <code>--dry-run</code> to print it for your choices.</p></details>`;
  const S=[["Set up","Once per machine.",cmdBlock(RUNCMD.setup)],
           ["Run","Check the route and the sealed sandbox, then run every task 3 times with the Terminus 2 harness.",cmdBlock(RUNCMD.run)+hb],
           ["Grading","Automatic: hidden tests run in a fresh container and write a reward per attempt: 0 or 1 for issue fixing, the fraction of tests passed for program construction.",""],
           ["Score","pass@1 and pass@3.",cmdBlock(RUNCMD.score)]];
  const intro=`<p class="skillnote">Every task follows the standard <a href="https://docs.harborframework.com/tasks/overview">Harbor task format</a>, so the benchmark runs with a plain <code>harbor run</code> with Docker. For advanced use cases involving external sandbox or API providers, use the <a href="https://github.com/awslabs/swe-cloud/tree/main/.agent/skills/swe-cloud-run"><b>swe-cloud-run</b> skill</a> with your coding agent. The steps below are one particular setup we use.</p>`;
  return head("pipeline","")+intro+`<ol class="pB pB${v} pShort">${S.map((s,i)=>`<li><span class="dotn">${i+1}</span><h3>${s[0]}</h3><div class="pbody"><p>${s[1]}</p>${s[2]}</div></li>`).join("")}</ol>`;
}

function rQA(){
  const v=V("qa"), more=`<p class="qamore"><a href="faq.html">All questions and answers →</a></p>`;
  if(v==="B") return head("qa","")+`<div class="qaB">${QA.slice(0,4).map(q=>`<div><h3>${esc(q[0])}</h3>${q[1]}</div>`).join("")}</div>`+more;
  if(v==="C") return head("qa","")+`<div class="qaC"><div class="ql" role="group" aria-label="Questions">${QA.map((q,i)=>`<button data-qa="${i}" aria-pressed="${i===qaSel}">${esc(q[0])}</button>`).join("")}</div><div class="qd"><h3>${esc(QA[qaSel][0])}</h3>${QA[qaSel][1]}</div></div>`+more;
  return head("qa","")+`<div class="qa">${QA.map((q,i)=>`<details><summary>${esc(q[0])}</summary>${q[1]}</details>`).join("")}</div>`+more;
}

function rCite(){
  const v=V("cite");
  if(v==="B") return head("cite","If you use SWE-Cloud, please cite:")+`<div class="citeB"><div class="tabs"><div class="seg" role="group" aria-label="Citation format"><button data-cite="bib" aria-pressed="${citeTab==="bib"}">BibTeX</button><button data-cite="txt" aria-pressed="${citeTab==="txt"}">Plain text</button></div></div><div class="cite">${citeTab==="bib"?`<pre>${esc(BIB)}</pre>`:`<p class="plain">${esc(PLAIN)}</p>`}<button class="copy" data-copy="${citeTab}">Copy</button></div></div>`;
  if(v==="C") return head("cite","")+`<div class="citeC"><p>${esc(PLAIN)}</p><div class="btns"><button class="pri" data-copy="bib">Copy BibTeX</button><button data-copy="txt">Copy text</button></div></div>`;
  return head("cite","If you use SWE-Cloud, please cite:")+`<div class="cite"><pre>${esc(BIB)}</pre><button class="copy" data-copy="bib">Copy</button></div>`;
}
const R={mast:rMast,how:rHow,leaderboard:rLeaderboard,ground:rGround,dist:rDist,pipeline:rPipeline,qa:rQA,cite:rCite};

/* ---------- Page ---------- */
/* ===== Leaderboard: score vs cost chart (wave 2, from the accepted design) ===== */
const ICONS=window.__ICONS||{};
const iconKey=n=>/^Claude/.test(n)?"claude":/^GPT/.test(n)?"openai":/^Kimi/.test(n)?"kimi":/^GLM/.test(n)?"zai":/^Grok/.test(n)?"grok":/^Qwen/.test(n)?"qwen":/^DeepSeek/.test(n)?"deepseek":"";
const CV=[{id:"ALL",name:"Overall (100)",short:"Overall",y:"SWE-Cloud Score",sk:"SC",ck:"cALL"},{id:"IF",name:"Issue fixing (60)",short:"Issue fixing",y:"pass@1",sk:"IF",ck:"cIF"},{id:"PC",name:"Program construction (40)",short:"Program construction",y:"pass@1",sk:"PC",ck:"cPC"}];
const cvOf=id=>CV.find(v=>v.id===id);
function pts(id){const v=cvOf(id);return MODELS.filter(r=>r[v.sk]!=null&&r[v.ck]!=null).map(r=>({r,s:r[v.sk],c:r[v.ck]}))}
function frontier(P){const s=P.slice().sort((a,b)=>a.c-b.c||b.s-a.s),out=[];let best=-1;for(const p of s){if(p.s>best+1e-9){out.push(p);best=p.s}}return out}
function niceY(mx){for(const t of [.2,.3,.4,.5,.6,.7,.8,.9,1])if(mx<=t-.02)return t;return 1}
const TICKS=[.1,.2,.5,1,2,5,10,20,50];
function xDom(P){const lo=Math.min(...P.map(p=>p.c)),hi=Math.max(...P.map(p=>p.c));let a=TICKS.filter(t=>t<=lo/1.15).pop()||.1,b=TICKS.find(t=>t>=hi*1.15)||50;return [a,b]}
const usdT=v=>v<1?"$"+v.toFixed(2):"$"+v;
let _ctx;function tw(t,f){_ctx=_ctx||document.createElement("canvas").getContext("2d");_ctx.font=f;return _ctx.measureText(t).width}
const ov=(a,b)=>a.x<b.x+b.w&&b.x<a.x+a.w&&a.y<b.y+b.h&&b.y<a.y+a.h;
function icon(r,x,y,R,front){const k=iconKey(r.m),s=R*1.25;return `<g class="pt${front?" fr":""}" data-m="${esc(r.m)}" transform="translate(${x.toFixed(1)},${y.toFixed(1)})"><circle r="${R}" class="pt-c"/><svg x="${-s/2}" y="${-s/2}" width="${s}" height="${s}" viewBox="0 0 24 24" class="ic">${ICONS[k]||""}</svg></g>`}
/* opts: {w,h,R,labels:"all"|"frontier", fr:"line"|"step"|"shade", compact} */
function chart(id,o){
  const v=cvOf(id),P=pts(id),F=frontier(P),FS=new Set(F.map(p=>p.r.m));
  const W=o.w,H=o.h,m={l:o.compact?44:56,r:o.compact?14:24,t:16,b:o.compact?40:48};
  const [x0,x1]=xDom(P),yM=niceY(Math.max(...P.map(p=>p.s)));
  const X=c=>m.l+(Math.log(c)-Math.log(x0))/(Math.log(x1)-Math.log(x0))*(W-m.l-m.r),Y=s=>H-m.b-s/yM*(H-m.t-m.b);
  let g="";
  const yt=[];for(let t=0;t<=yM+1e-9;t+=yM>.5?.1:yM>.25?.05:.05)yt.push(+t.toFixed(2));
  yt.forEach(t=>{g+=`<line class="gd" x1="${m.l}" x2="${W-m.r}" y1="${Y(t)}" y2="${Y(t)}"/><text class="tk" x="${m.l-8}" y="${Y(t)+4}" text-anchor="end">${Math.round(t*100)}%</text>`});
  TICKS.filter(t=>t>=x0&&t<=x1).forEach(t=>{g+=`<line class="gd v" x1="${X(t)}" x2="${X(t)}" y1="${m.t}" y2="${H-m.b}"/><text class="tk" x="${X(t)}" y="${H-m.b+18}" text-anchor="middle">${usdT(t)}</text>`});
  g+=`<line class="ax" x1="${m.l}" x2="${W-m.r}" y1="${H-m.b}" y2="${H-m.b}"/>`;
  g+=`<text class="al" x="${(m.l+W-m.r)/2}" y="${H-6}" text-anchor="middle">Cost per attempt, USD (log scale)</text>`;
  g+=`<text class="al" transform="translate(${o.compact?11:14},${(m.t+H-m.b)/2}) rotate(-90)" text-anchor="middle">${v.y}</text>`;
  /* frontier */
  const fp=F.map(p=>[X(p.c),Y(p.s)]);
  const vt=[fp[0]];if(o.fr==="line")vt.push(...fp.slice(1));else{for(let i=1;i<fp.length;i++){vt.push([fp[i][0],fp[i-1][1]],fp[i])}vt.push([W-m.r,fp[fp.length-1][1]])}
  const obs=[];for(let i=1;i<vt.length;i++){const[ax,ay]=vt[i-1],[bx,by]=vt[i],n=Math.ceil(Math.hypot(bx-ax,by-ay)/5);for(let j=0;j<=n;j++){const x=ax+(bx-ax)*j/n,y=ay+(by-ay)*j/n;obs.push({x:x-2,y:y-2,w:4,h:4})}}
  if(o.fr==="step"||o.fr==="shade"){let d=`M${fp[0][0]} ${fp[0][1]}`;for(let i=1;i<fp.length;i++)d+=`H${fp[i][0]}V${fp[i][1]}`;d+=`H${W-m.r}`;
    if(o.fr==="shade"){g+=`<path class="dom" d="${d}V${H-m.b}H${fp[0][0]}Z"/>`}
    g+=`<path class="fl${o.fr==="shade"?" solid":""}" d="${d}"/>`}
  else{g+=`<path class="fl" d="M${fp.map(p=>p.join(" ")).join("L")}"/>`}
  /* labels */
  const R=o.R,placed=[],mk=P.map(p=>({x:X(p.c)-R,y:Y(p.s)-R,w:2*R,h:2*R})).concat(obs);
  const font=`700 ${o.compact?11:12}px "Open Sans"`,lh=o.compact?13:15;let lab="";
  const order=P.slice().sort((a,b)=>(FS.has(b.r.m)-FS.has(a.r.m))||b.s-a.s);
  order.forEach(p=>{if(o.labels==="frontier"&&!FS.has(p.r.m))return;const x=X(p.c),y=Y(p.s),t=p.r.m,w=tw(t,font)+2,h=lh,gap=R+5;
    const C=[[x+gap,y-h/2,"start"],[x-gap-w,y-h/2,"end"],[x-w/2,y-R-4-h,"middle"],[x-w/2,y+R+3,"middle"],[x+gap-2,y-R-h+2,"start"],[x+gap-2,y+R-3,"start"],[x-gap-w+2,y-R-h+2,"end"],[x-gap-w+2,y+R-3,"end"]];
    let best=null;for(const c of C){const b={x:c[0],y:c[1],w,h};if(b.x<m.l||b.x+w>W-2||b.y<0||b.y+h>H-m.b)continue;if(placed.some(q=>ov(b,q))||mk.some(q=>ov(b,q)))continue;best=[b,c[2]];break}
    if(!best){for(const dy of [-26,26,-40,40]){const b={x:x+gap,y:y+dy-h/2,w,h};if(b.x+w>W-2)b.x=x-gap-w;if(b.y<0||b.y+h>H-m.b)continue;if(placed.some(q=>ov(b,q))||mk.some(q=>ov(b,q)))continue;best=[b,"lead"];break}}
    if(!best){const cand=[[x-gap-w-6,y-R-h-8],[x+gap+6,y-R-h-8],[x-w/2,y-R-h-14],[x-w/2,y-R-h-28]];for(const c of cand){const b={x:Math.max(m.l,Math.min(W-2-w,c[0])),y:c[1],w,h};if(b.y<0||placed.some(q=>ov(b,q)))continue;best=[b,"lead"];break}}
    if(!best)return;const [b,a]=best;placed.push(b);
    if(a==="lead")lab+=`<line class="ld" x1="${x}" y1="${y}" x2="${Math.max(b.x,Math.min(b.x+w,x))}" y2="${b.y+h>y?b.y:b.y+h}"/>`;
    lab+=`<text class="pl${FS.has(t)?" f":""}" data-m="${esc(t)}" x="${b.x+1}" y="${b.y+h-3.5}">${esc(t)}</text>`});
  let ic="";P.slice().sort((a,b)=>FS.has(a.r.m)-FS.has(b.r.m)).forEach(p=>{ic+=icon(p.r,X(p.c),Y(p.s),R,FS.has(p.r.m))});
  return `<svg class="sc-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${v.short}: ${v.y} against cost per attempt, with the Pareto frontier">${g}${lab}${ic}</svg>`;
}
function iconHTML(r){const k=iconKey(r.m);return `<svg viewBox="0 0 24 24" class="ic">${ICONS[k]||""}</svg>`}
let lbMode="table";
const SCKEY=`<div class="sc-key"><span><i class="k-fl"></i>Pareto frontier</span><span><i class="k-pt"></i>on the frontier</span><span><i class="k-pt off"></i>off the frontier</span></div>`;
const modeSeg=()=>`<div class="seg seg-mode" role="group" aria-label="Leaderboard display"><button data-lbmode="table" aria-pressed="${lbMode==="table"}">▤ Table</button><button data-lbmode="chart" aria-pressed="${lbMode==="chart"}">◩ Score vs cost</button></div>`;
function lbChart(){return `<div class="sc-host" data-chart="${view}"></div>${SCKEY}`}
function chartFoot(){const parts=[];
  if(view==="ALL")parts.push(`* <b>SWE-Cloud Score</b> = w<sub>IF</sub> × issue-fixing pass@1 + w<sub>PC</sub> × program-construction pass@1, where w is each task type's proportion of all tasks.`);
  else parts.push("<b>pass@1</b>: mean success rate over three attempts per task.");
  parts.push(`<b>Cost</b>: estimated API cost per attempt in USD, at list prices from a pinned LiteLLM price table, on a log scale${view==="ALL"?"; overall averages the 100 tasks (60 issue fixing, 40 program construction)":""}.`);
  parts.push("<b>Pareto frontier</b>: the models that no other model beats on both score and cost. Hover a model for its numbers.");
  return `<p class="lbfoot">${parts.join(" ")}</p>`}
function drawCharts(){document.querySelectorAll(".sc-host").forEach(h=>{const w=Math.max(280,Math.floor(h.clientWidth)),id=h.dataset.chart;
  h.innerHTML=chart(id,{w,h:LBH||Math.round(Math.min(520,Math.max(380,w*.42))),R:15,labels:"all",fr:"line"})+`<div class="sc-tip" hidden></div>`;})}
function tip(host,m,ev){const t=host.querySelector(".sc-tip");if(!t)return;if(!m){t.hidden=true;return}const r=MODELS.find(x=>x.m===m),id=host.dataset.chart,v=cvOf(id),F=new Set(frontier(pts(id)).map(p=>p.r.m));
  t.innerHTML=`<div class="tt-h">${iconHTML(r)}<b>${esc(r.m)}</b> <span class="eff">(${r.eff})</span></div><div class="tt-s">${esc(r.lab)} · ${r.w==="Open"?"Open weights":"Closed"}</div><table><tr><td>${v.y}</td><td class="num">${p1(r[v.sk])}</td></tr><tr><td>Cost per attempt</td><td class="num">${usd(r[v.ck])}</td></tr>${id==="ALL"?`<tr class="sm"><td>Issue fixing pass@1</td><td class="num">${p1(r.IF)}</td></tr><tr class="sm"><td>Program construction pass@1</td><td class="num">${p1(r.PC)}</td></tr>`:""}</table>${F.has(m)?`<div class="tt-f">On the Pareto frontier</div>`:""}`;
  t.hidden=false;const hb=host.getBoundingClientRect();let x=ev.clientX-hb.left+14,y=ev.clientY-hb.top+14;if(x+t.offsetWidth>hb.width)x=ev.clientX-hb.left-t.offsetWidth-14;if(y+t.offsetHeight>hb.height)y=ev.clientY-hb.top-t.offsetHeight-14;t.style.left=x+"px";t.style.top=y+"px"}
const SITE=document.getElementById("site");let LBH=0;
SITE.addEventListener("click",e=>{const b=e.target.closest("[data-lbmode]");if(b){e.stopPropagation();const tw=document.querySelector("#c-leaderboard .tblwrap");if(tw)LBH=tw.offsetHeight;lbMode=b.dataset.lbmode;rerenderKeepFocus(`[data-lbmode="${lbMode}"]`)}},true);
SITE.addEventListener("mousemove",e=>{const host=e.target.closest(".sc-host");document.querySelectorAll(".sc-tip").forEach(t=>{if(!host||!t.parentNode.contains(e.target.closest(".pt,.pl")))t.hidden=true});if(!host)return;const el=e.target.closest(".pt,.pl");tip(host,el?el.dataset.m:null,e);document.querySelectorAll(".sc-svg").forEach(s=>{s.classList.toggle("dim",!!el);s.querySelectorAll("[data-m]").forEach(x=>x.classList.toggle("hl",!!el&&x.dataset.m===el.dataset.m))})});
if(document.fonts)document.fonts.ready.then(()=>{if(document.querySelector(".sc-host"))drawCharts()});
window.addEventListener("resize",()=>{clearTimeout(window.__rz);window.__rz=setTimeout(drawCharts,120)});
new MutationObserver(()=>{if(document.querySelector(".sc-host:empty"))drawCharts()}).observe(SITE,{childList:true});

function picker(id){return "";}
function pickerUnused(id){return `<div class="vpick" role="group" aria-label="${LABEL[id]} variant"><em>${LABEL[id]}</em>${VARIANTS[id].map(v=>`<button data-vc="${id}" data-v="${v[0]}" aria-pressed="${V(id)===v[0]}" title="${v[1]}">${v[0]}</button>`).join("")}<span class="vsep"></span><button class="vhide" data-hide="${id}" title="Hide this component" aria-label="Hide ${LABEL[id]}">Hide</button></div>`}
function ghost(id){return clean?"":`<div class="ghost"><span>${LABEL[id]} is hidden</span><button data-show="${id}">Show</button></div>`}
function section(id){return `<section class="comp" id="c-${id}" data-comp="${id}" aria-label="${LABEL[id]}">${picker(id)}${R[id]()}</section>`}
function render(){
  const site=document.getElementById("site");
  site.className="site t-"+theme+(clean?" clean":"");
  const full=[FIXED_TOP,...order,FIXED_BOTTOM];
  const all=full.filter(id=>!hidden.has(id));
  const nav=[["how","Tasks"],["leaderboard","Leaderboard"],["qa","Q&A"],["cite","Cite"]].filter(x=>all.includes(x[0])).map(x=>`<button data-go="${x[0]}">${x[1]}</button>`).join("")+`<a href="tasks.html#if">Task Gallery</a>`;
  let h=`<header class="shead">${logo(theme==="aws")}${theme==="docs"?'<span class="ver">v1.0</span>':""}<nav aria-label="Site">${nav}</nav></header>`;
  if(theme==="aws") h+=`<div class="crumbs"><a href="#aws">SWE-Cloud</a> &nbsp;›&nbsp; Overview</div>`;
  h+=`<div class="layout"><aside class="side"><h5>On this page</h5>${all.filter(id=>id!==FIXED_TOP).map(id=>`<button data-go="${id}">${LABEL[id]}</button>`).join("")}<div class="ext"><a href="tasks.html#if">Issue-fixing tasks</a><a href="tasks.html#pc">Program-construction tasks</a><a href="faq.html">Full Q&amp;A</a></div></aside><main class="main">`;
  h+=full.map(id=>hidden.has(id)?ghost(id):section(id)).join("");
  h+=`</main></div><footer class="sfoot">SWE-Cloud</footer>`;
  site.innerHTML=h;
}
/* site interactions */
function rerenderKeepFocus(q){render();const nb=document.querySelector(q);if(nb)nb.focus()}
function copyText(t){return Promise.resolve().then(()=>navigator.clipboard.writeText(t)).then(()=>true).catch(()=>{try{const ta=document.createElement("textarea");ta.value=t;ta.style.position="fixed";ta.style.opacity="0";document.body.appendChild(ta);ta.select();const ok=document.execCommand("copy");ta.remove();return ok}catch(e){return false}})}
document.getElementById("site").addEventListener("change",e=>{const t=e.target;if(t.dataset&&t.dataset.run){RUN[t.dataset.run]=t.value.trim();rerenderKeepFocus(`[data-run="${t.dataset.run}"]`)}});
document.getElementById("site").addEventListener("click",e=>{
  const hd=e.target.closest("[data-hide]");if(hd){hidden.add(hd.dataset.hide);refresh();return}
  const sh=e.target.closest("[data-show]");if(sh){hidden.delete(sh.dataset.show);refresh();const el=document.getElementById("c-"+sh.dataset.show);if(el)el.scrollIntoView({block:"center"});return}
  const vc=e.target.closest("[data-vc]");if(vc){variants[theme][vc.dataset.vc]=vc.dataset.v;save();renderPanel();rerenderKeepFocus(`[data-vc="${vc.dataset.vc}"][data-v="${vc.dataset.v}"]`);return}
  const cc=e.target.closest("[data-cmdcopy]");if(cc){const txt=[...cc.parentElement.querySelectorAll(".cl")].map(x=>x.textContent).join("\n");copyText(txt).then(ok=>{cc.textContent=ok?"Copied":"Copy failed";cc.classList.toggle("ok",ok);setTimeout(()=>{cc.textContent="Copy";cc.classList.remove("ok")},1600)});return}
  const rt=e.target.closest("[data-runtab]");if(rt){RUN.tab=rt.dataset.runtab;rerenderKeepFocus(`[data-runtab="${RUN.tab}"]`);return}
  const v=e.target.closest("[data-view]");if(v){view=v.dataset.view;rerenderKeepFocus(`[data-view="${view}"]`);return}
  const hw=e.target.closest("[data-how]");if(hw){howTab=hw.dataset.how;rerenderKeepFocus(`[data-how="${howTab}"]`);return}
  const q=e.target.closest("[data-qa]");if(q){qaSel=+q.dataset.qa;rerenderKeepFocus(`[data-qa="${qaSel}"]`);return}
  const ct=e.target.closest("[data-cite]");if(ct){citeTab=ct.dataset.cite;rerenderKeepFocus(`[data-cite="${citeTab}"]`);return}
  const g=e.target.closest("[data-go]");if(g){const el=document.getElementById("c-"+g.dataset.go);if(el)el.scrollIntoView({behavior:"smooth",block:"start"});return}
  const cp=e.target.closest("[data-copy]");if(cp){const txt=cp.dataset.copy==="bib"?BIB:PLAIN,orig=cp.textContent;Promise.resolve().then(()=>navigator.clipboard.writeText(txt)).then(()=>{cp.textContent="Copied"},()=>{cp.textContent="Select and copy"}).finally(()=>setTimeout(()=>{cp.textContent=orig},1600))}
});


/* export selection */

/* ---------- Data ---------- */
const getJSON=p=>fetch(p,{cache:"no-cache"}).then(r=>{if(!r.ok)throw new Error(p+": "+r.status);return r.json()});
function fromLeaderboard(lb){
  const W=lb.score;
  return lb.models.map(m=>{const I=m.issue_fixing,P=m.program_construction,r={
    m:m.name,lab:m.lab,w:m.weights==="open"?"Open":"Closed",hf:m.weights_url||"",eff:m.effort||"",note:m.note||"",
    IF:I["pass@1"],IF3:I["pass@3"],cIF:I.cost_usd,PC:P["pass@1"],PC3:P["pass@3"],AL:P["almost@1"],cPC:P.cost_usd};
    const nI=lb.tasks.issue_fixing,nP=lb.tasks.program_construction;
    const ok=(...a)=>a.every(x=>x!=null);
    r.SC=ok(r.IF,r.PC)?W.w_if*r.IF+W.w_pc*r.PC:null; r.cALL=m.cost_usd!=null?m.cost_usd:ok(r.cIF,r.cPC)?(nI*r.cIF+nP*r.cPC)/(nI+nP):null; r.ALL=ok(r.IF,r.PC)?(nI*r.IF+nP*r.PC)/(nI+nP):null; return r});
}
Promise.all(["data/site.json","data/leaderboard.json","data/faq.json","data/citation.json","data/coverage.json"].map(getJSON)).then(([site,lb,faq,cite,cov])=>{
  order=site.order.filter(id=>MOVABLE.includes(id)); hidden=new Set(site.hidden||[]); variants.aws=site.variants||{};
  LB=lb; MODELS=fromLeaderboard(lb);
  QA=faq.items.map(x=>[x.q,x.a]); BIB=cite.bibtex; PLAIN=cite.text;
  const tup=T=>T.map(c=>[c.category,c.subcategories.map(x=>[x.name,x.count,(x.tasks||[]).join(", ")])]);
  COV={IF:tup(cov.issue_fixing),PC:tup(cov.program_construction),SVC:cov.aws_services};
  render();
  // A link such as index.html#leaderboard (or #c-leaderboard) opens at that section once it is built.
  const h=decodeURIComponent(location.hash.slice(1)), at=h&&(document.getElementById(h)||document.getElementById("c-"+h));
  if(at)requestAnimationFrame(()=>at.scrollIntoView({block:"start"}));
}).catch(err=>{
  document.getElementById("site").innerHTML=`<div class="loaderr"><h1>SWE-Cloud</h1><p>The page could not load its data files (${esc(err.message)}).</p><p>Open it through a web server, not as a file: run <code>python3 -m http.server 8000</code> in this folder and visit <code>http://localhost:8000</code>.</p></div>`;
});

})();
