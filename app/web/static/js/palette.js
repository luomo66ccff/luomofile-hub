export function initPalette() {
 const dialog = document.querySelector("[data-palette]"); if (!dialog?.showModal) return;
 const input=dialog.querySelector("input"), list=dialog.querySelector("[role=listbox]");
 const commands=[...dialog.querySelector("template").content.querySelectorAll("a")].map(a=>({title:a.textContent,url:a.getAttribute("href"),group:a.dataset.group,icon:a.dataset.icon}));
 let active=0, results=[], timer, abort, returnFocus;
 function select(i) {
  active=results.length ? (i+results.length)%results.length : 0;
  [...list.querySelectorAll('[role="option"]')].forEach((el,j)=>el.setAttribute("aria-selected",String(j===active)));
  if (results.length) { input.setAttribute("aria-activedescendant","command-"+active); list.querySelectorAll('[role="option"]')[active]?.scrollIntoView({block:"nearest"}); }
  else input.removeAttribute("aria-activedescendant");
 }
 function render(items) {
  results=items; list.replaceChildren();
  let group;
  items.forEach((item,i)=> {
   if(item.group!==group){group=item.group;const heading=document.createElement("li");heading.className="palette-group";heading.setAttribute("role","presentation");heading.textContent=group;list.append(heading);}
   const el=document.createElement("li");el.className="palette-item";el.id="command-"+i;el.setAttribute("role","option");
   const svg=document.createElementNS("http://www.w3.org/2000/svg","svg"),use=document.createElementNS("http://www.w3.org/2000/svg","use");svg.setAttribute("aria-hidden","true");use.setAttribute("href","#i-"+(item.icon||"box"));svg.append(use);
   const title=document.createElement("span");title.textContent=item.title;el.append(svg,title);
   if(Number.isFinite(item.size_bytes)){const meta=document.createElement("small");meta.textContent=item.size_bytes>=1048576?(item.size_bytes/1048576).toFixed(1)+" MB":item.size_bytes>=1024?(item.size_bytes/1024).toFixed(1)+" KB":item.size_bytes+" B";el.append(meta);}
   el.addEventListener("click",()=>location.assign(item.url));list.append(el);
  });
  if (!items.length) { const empty=document.createElement("p"); empty.className="palette-group"; empty.textContent="没有匹配的结果"; list.append(empty); }
  select(0);
 }
 function open() { if(dialog.open)return; returnFocus=document.activeElement; input.value=""; render(commands); dialog.showModal(); input.setAttribute("aria-expanded","true"); input.focus(); }
 document.querySelectorAll("[data-palette-open]").forEach(b=>b.addEventListener("click",open));
 dialog.querySelector("[data-palette-close]").addEventListener("click",()=>dialog.close());
 dialog.addEventListener("close",()=>{ abort?.abort(); clearTimeout(timer); input.setAttribute("aria-expanded","false"); returnFocus?.focus(); });
 document.addEventListener("keydown",e=>{
  const typing=e.target.closest("input,textarea,select,[contenteditable=true]");
  if ((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="k" || e.key==="/"&&!typing) { e.preventDefault(); open(); }
 });
 input.addEventListener("keydown",e=>{
  if (e.key==="ArrowDown"||e.key==="ArrowUp") {e.preventDefault();select(active+(e.key==="ArrowDown"?1:-1));}
  if (e.key==="Enter" && results[active]) {e.preventDefault();location.assign(results[active].url);}
 });
 input.addEventListener("input",()=>{
  clearTimeout(timer);abort?.abort(); const q=input.value.trim();
  const local=commands.filter(c=>c.title.toLowerCase().includes(q.toLowerCase()));render(local);
  if (!q||document.body.dataset.signedIn!=="true")return;
  timer=setTimeout(async()=>{
   abort=new AbortController();
   try { const r=await fetch("/api/session/search?q="+encodeURIComponent(q),{signal:abort.signal,headers:{Accept:"application/json"}});
    if(!r.ok)return;const data=await r.json();
    if(dialog.open&&input.value.trim()===q)render([...local,...data.files.map(f=>({...f,group:"文件",icon:"box"})),...data.albums.map(a=>({...a,group:"相册",icon:"album"}))]);
   } catch {}
  },220);
 });
}