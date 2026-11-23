/* SWE-Cloud Q&A page: questions only, answers folded, "Expand all" per group. Content from data/faq-page.json. */
(function(){
"use strict";
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const LOGO='<span class="logo dk" role="img" aria-label="SWE-Cloud"><svg viewBox="0 0 64 46" aria-hidden="true"><path class="lc" d="M17 42 H49 A11 11 0 0 0 51.5 20.3 A16 16 0 0 0 21 14.5 A13.8 13.8 0 0 0 17 42 Z"/><path class="lp" d="M23 21 L30 26.5 L23 32"/><path class="lp" d="M34 33 H42"/></svg><span aria-hidden="true">SWE-Cloud</span></span>';
const site=document.getElementById("site");
function render(groups){
  const head=`<header class="shead"><a href="index.html" style="text-decoration:none">${LOGO}</a><nav aria-label="Site"><a href="index.html">Overview</a><a href="index.html#c-leaderboard">Leaderboard</a><a href="tasks.html#if">Tasks</a><a href="faq.html" aria-current="page">Q&amp;A</a></nav></header>`;
  const crumbs=`<div class="crumbs"><a href="index.html">SWE-Cloud</a> &nbsp;›&nbsp; Q&amp;A</div>`;
  const body=groups.map(g=>`<section class="comp faqg" id="${esc(g.id)}"><div class="faq-gh"><div><h2 class="comp-h">${esc(g.title)}</h2>${g.desc?`<p class="faq-gd">${esc(g.desc)}</p>`:""}</div><button class="faq-xall" type="button" data-xall="${esc(g.id)}">Expand all</button></div>
    <div class="faq-q">${g.items.map(it=>`<details id="${esc(it.id)}"><summary><span>${esc(it.q)}</span></summary><div class="faq-a">${it.a}</div></details>`).join("")}</div></section>`).join("");
  site.innerHTML=head+crumbs+`<main class="main faq-main"><h1 class="faq-h1">Q&amp;A</h1>${body}</main><footer class="sfoot">SWE-Cloud</footer>`;
  const id=location.hash.slice(1), d=id&&document.getElementById(id);
  if(d&&d.tagName==="DETAILS"){d.open=true;d.scrollIntoView()}
}
site.addEventListener("click",e=>{
  const b=e.target.closest("[data-xall]");if(!b)return;
  const ds=[...document.getElementById(b.dataset.xall).querySelectorAll("details")], open=ds.some(d=>!d.open);
  ds.forEach(d=>d.open=open); b.textContent=open?"Collapse all":"Expand all";
});
site.addEventListener("toggle",e=>{
  const g=e.target.closest&&e.target.closest(".faqg");if(!g)return;
  const ds=[...g.querySelectorAll("details")], b=g.querySelector("[data-xall]");
  if(b)b.textContent=ds.every(d=>d.open)?"Collapse all":"Expand all";
},true);
fetch("data/faq-page.json",{cache:"no-cache"}).then(r=>{if(!r.ok)throw new Error("HTTP "+r.status);return r.json()}).then(d=>render(d.groups))
  .catch(err=>{site.innerHTML=`<div class="loaderr"><h1>SWE-Cloud</h1><p>The page could not load data/faq-page.json (${esc(err.message)}). Serve the repository root, for example <code>python3 -m http.server 8000</code>, and open <code>http://localhost:8000/site/faq.html</code>.</p></div>`});
})();
