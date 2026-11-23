/* SWE-Cloud task gallery.
   Nothing about the tasks is bundled here: the list comes from tasks/SOURCES.md, and each card and page
   from the task's own README.md and task-diagram.svg, read at view time from the task folders.
   The site expects to sit next to them (repo/site and repo/tasks); data/site.json "tasks_base" can point elsewhere. */
(function(){
const SPLITS=[
  {id:"if",dir:"issue-fixing",name:"Issue fixing",heading:"Issue-fixing",lede:"Each task is a working AWS-backed service with a reported production failure. The agent finds the cause and ships a fix; hidden tests replay crashes, retries and failovers."},
  {id:"pc",dir:"program-construction",name:"Program construction",heading:"Program-construction",lede:"Each task is a program modeled on a real open-source project, which the agent can run but not read. The agent rebuilds it from interface guides and experiments; hidden tests compare both on fresh AWS state."}
];
const PROMPT='<path class="lp" d="M23 21 L30 26.5 L23 32"/><path class="lp" d="M34 33 H42"/>';
const logo=()=>`<a href="index.html" class="logo dk" aria-label="SWE-Cloud home" style="text-decoration:none;color:inherit"><svg viewBox="0 0 64 46" aria-hidden="true"><path class="lc" d="M17 42 H49 A11 11 0 0 0 51.5 20.3 A16 16 0 0 0 21 14.5 A13.8 13.8 0 0 0 17 42 Z"/>${PROMPT}</svg><span aria-hidden="true">SWE-Cloud</span></a>`;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const site=document.getElementById("site");
let BASE="../tasks", TASKS=[], q="";
const info={};   // slug -> {title, summary, services}

// Task images carry a per-visit tag, so a browser never shows a cached diagram after a task changes (READMEs are fetched with no-cache).
const VISIT=Date.now();
const getText=p=>fetch(p,{cache:"no-cache"}).then(r=>{if(!r.ok)throw new Error(p+": HTTP "+r.status);return r.text()});
const splitOf=slug=>SPLITS.find(s=>slug.startsWith(s.id+"-"));
const folder=t=>`${BASE}/${splitOf(t.slug).dir}/${t.slug}/`;

/* tasks/SOURCES.md: one table per split, rows "| `slug` | kind | [project](url) | `revision` |" */
function parseSources(md){
  const out=[];
  for(const line of md.split("\n")){
    const m=line.match(/^\|\s*`((?:if|pc)-[a-z0-9-]+)`\s*\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*`?([^|`]*)`?\s*\|\s*$/);
    if(!m)continue;
    const src=m[3].match(/\[([^\]]+)\]\(([^)]+)\)/);
    out.push({slug:m[1],kind:m[2],project:src?src[1]:m[3],url:src?src[2]:"",rev:m[4]});
  }
  return out;
}

/* A small Markdown renderer for the task READMEs: headings, paragraphs, lists, tables, fenced code,
   images and links (relative paths resolve against the task folder), inline code, bold and italics. */
function inline(s,base){
  const codes=[];
  s=s.replace(/`([^`]+)`/g,(_,c)=>{codes.push(c);return `\u0000${codes.length-1}\u0000`});
  s=esc(s);
  const url=u=>/^(https?:|mailto:|#|\/)/.test(u)?u:base+u;
  s=s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g,(_,a,u)=>`<img src="${esc(/^https?:/.test(u)?u:url(u)+"?v="+VISIT)}" alt="${a}" loading="lazy">`);
  s=s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,(_,t,u)=>`<a href="${esc(url(u))}"${/^https?:/.test(u)?' target="_blank" rel="noopener"':""}>${t}</a>`);
  s=s.replace(/\*\*([^*]+)\*\*/g,"<b>$1</b>").replace(/(^|[^*])\*([^*\s][^*]*)\*/g,"$1<i>$2</i>");
  return s.replace(/\u0000(\d+)\u0000/g,(_,i)=>`<code>${esc(codes[+i])}</code>`);
}
function markdown(md,base){
  const L=md.replace(/\r/g,"").split("\n");let h="",i=0;
  const cells=l=>l.trim().replace(/^\||\|$/g,"").split("|").map(c=>c.trim());
  while(i<L.length){
    const l=L[i];
    if(!l.trim()){i++;continue}
    let m;
    if(l.startsWith("```")){let c=[];i++;while(i<L.length&&!L[i].startsWith("```"))c.push(L[i++]);i++;h+=`<pre><code>${esc(c.join("\n"))}</code></pre>`;continue}
    if((m=l.match(/^(#{1,4})\s+(.*)/))){const n=m[1].length;h+=`<h${n}>${inline(m[2],base)}</h${n}>`;i++;continue}
    if(l.trim().startsWith("|")){
      const rows=[];while(i<L.length&&L[i].trim().startsWith("|"))rows.push(L[i++]);
      const head=rows.length>1&&/^[\s|:-]+$/.test(rows[1])?cells(rows[0]):null;
      const data=(head?rows.slice(2):rows).filter(r=>!/^[\s|:-]+$/.test(r));
      const emptyHead=head&&head.every(c=>!c);
      h+=`<table>${head&&!emptyHead?`<thead><tr>${head.map(c=>`<th>${inline(c,base)}</th>`).join("")}</tr></thead>`:""}<tbody>${data.map(r=>`<tr>${cells(r).map(c=>`<td>${inline(c,base)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
      continue}
    if(/^\s*([-*]|\d+\.)\s+/.test(l)){
      const ord=/^\s*\d+\./.test(l);const items=[];
      while(i<L.length&&(/^\s*([-*]|\d+\.)\s+/.test(L[i])||(/^\s{2,}\S/.test(L[i])&&items.length))){
        if(/^\s*([-*]|\d+\.)\s+/.test(L[i]))items.push(L[i].replace(/^\s*([-*]|\d+\.)\s+/,""));else items[items.length-1]+=" "+L[i].trim();i++}
      h+=`<${ord?"ol":"ul"}>${items.map(x=>`<li>${inline(x,base)}</li>`).join("")}</${ord?"ol":"ul"}>`;continue}
    const p=[];while(i<L.length&&L[i].trim()&&!/^(#{1,4}\s|```|\||\s*([-*]|\d+\.)\s+)/.test(L[i]))p.push(L[i++].trim());
    if(p.length)h+=`<p>${inline(p.join(" "),base)}</p>`;else i++;
  }
  return h;
}
/* Card facts from a README: the H1, the first paragraph, and the "AWS services" row of "Task facts". */
function summarize(md){
  const L=md.replace(/\r/g,"").split("\n");
  const title=(L.find(l=>/^#\s/.test(l))||"").replace(/^#\s+/,"");
  const start=L.findIndex(l=>/^#\s/.test(l));
  let summary="";for(let k=start+1;k<L.length;k++){const t=L[k].trim();if(!t)continue;if(/^(!\[|#|\|)/.test(t))break;summary=t;break}
  const row=L.find(l=>/^\|\s*AWS services\s*\|/i.test(l));
  const services=row?row.split("|")[2].split(",").map(s=>s.trim()).filter(Boolean):[];
  return {title,summary,services};
}
const plain=s=>s.replace(/`([^`]+)`/g,"$1").replace(/\*\*([^*]+)\*\*/g,"$1").replace(/\[([^\]]+)\]\([^)]+\)/g,"$1");

function shell(body){
  return `<header class="shead">${logo()}<nav aria-label="Site"><a href="index.html">Overview</a><a href="index.html">Leaderboard</a></nav></header>
  <div class="crumbs"><a href="index.html">SWE-Cloud</a> &nbsp;›&nbsp; Task Gallery</div><main class="gal">${body}</main><footer class="sfoot">SWE-Cloud</footer>`;
}
function card(t){
  const x=info[t.slug];
  return `<a class="tcard" href="#${t.slug}"><h3>${x?esc(plain(x.title)):esc(t.slug)}</h3><span class="slug">${esc(t.slug)}</span>${x?`<p>${esc(plain(x.summary))}</p>`:"<p>…</p>"}<div class="chips2"><span class="k">${esc(t.kind)}</span>${x?x.services.map(s=>`<span>${esc(s)}</span>`).join(""):""}</div></a>`;
}
function listView(sp){
  const all=TASKS.filter(t=>t.slug.startsWith(sp.id+"-"));
  const words=q.toLowerCase().split(/\s+/).filter(Boolean);
  const hit=t=>{const x=info[t.slug];const hay=(t.slug+" "+t.kind+" "+t.project+" "+(x?x.title+" "+x.summary+" "+x.services.join(" "):"")).toLowerCase();return words.every(w=>hay.includes(w))};
  const shown=all.filter(hit);
  site.innerHTML=shell(`<h1>Task Gallery</h1><p class="lede">${esc(sp.lede)}</p>
    <div class="gtop"><nav class="gtabs" aria-label="Task set">${SPLITS.map(s=>`<a href="#${s.id}" aria-current="${s.id===sp.id}">${s.name} (${TASKS.filter(t=>t.slug.startsWith(s.id+"-")).length})</a>`).join("")}</nav>
    <input class="gsearch" type="search" placeholder="Filter by title, service or source" value="${esc(q)}" aria-label="Filter tasks"><span class="gcount">${shown.length} of ${all.length}</span></div>
    <div class="ggrid">${shown.map(card).join("")}</div>`);
  const inp=site.querySelector(".gsearch");
  inp.addEventListener("input",e=>{q=e.target.value;const pos=e.target.selectionStart;listView(sp);const n=site.querySelector(".gsearch");n.focus();n.setSelectionRange(pos,pos)});
  /* fill card facts from each README as they arrive */
  all.filter(t=>!info[t.slug]).forEach(t=>getText(folder(t)+"README.md").then(md=>{info[t.slug]=summarize(md);const el=site.querySelector(`a.tcard[href="#${t.slug}"]`);if(el)el.outerHTML=card(t)}).catch(()=>{}));
}
function taskView(t){
  const sp=splitOf(t.slug),base=folder(t);
  site.innerHTML=shell(`<div class="tdet"><a class="back" href="#${sp.id}">← ${sp.name} tasks</a>
    <div class="meta"><span class="slug" style="font-family:var(--mono)">swe-cloud/${esc(t.slug)}</span>·<span>${esc(t.kind)}</span>${t.url?`·<span>source: <a href="${esc(t.url)}" target="_blank" rel="noopener">${esc(t.project)}</a></span>`:""}${t.rev?`·<code>${esc(t.rev)}</code>`:""}</div>
    <article class="md"><p class="loading">Loading README…</p></article></div>`);
  window.scrollTo(0,0);
  getText(base+"README.md").then(md=>{site.querySelector(".md").innerHTML=markdown(md,base);document.title=plain(summarize(md).title)+" · SWE-Cloud"})
    .catch(e=>{site.querySelector(".md").innerHTML=`<div class="gerr">Could not load <code>${esc(base)}README.md</code> (${esc(e.message)}).</div>`});
}
function route(){
  const h=decodeURIComponent(location.hash.slice(1));
  const t=TASKS.find(x=>x.slug===h);
  if(t)return taskView(t);
  document.title="SWE-Cloud: Task Gallery";
  listView(SPLITS.find(s=>s.id===h)||SPLITS[0]);
}
fetch("data/site.json",{cache:"no-cache"}).then(r=>r.ok?r.json():{}).catch(()=>({}))
  .then(cfg=>{if(cfg&&cfg.tasks_base)BASE=cfg.tasks_base.replace(/\/$/,"");return getText(BASE+"/SOURCES.md")})
  .then(md=>{TASKS=parseSources(md);if(!TASKS.length)throw new Error("no tasks found in SOURCES.md");window.addEventListener("hashchange",route);route()})
  .catch(err=>{site.innerHTML=shell(`<div class="gerr"><h1>Tasks</h1><p>The gallery reads the task folders at <code>${esc(BASE)}/</code> and could not load them (${esc(err.message)}).</p><p>Serve the repository root, not only <code>site/</code>: run <code>python3 -m http.server 8000</code> in the repository and open <code>http://localhost:8000/site/tasks.html</code>.</p></div>`)});
})();
