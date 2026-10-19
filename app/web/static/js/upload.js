import {toast} from "./notice.js";
export function initUpload() {
 if(document.body.dataset.uploadEnabled!=="true")return;
 const overlay=document.querySelector("[data-dock-overlay]"), tray=document.querySelector("[data-tray]"), list=tray.querySelector("[data-tray-list]");
 const queue=[];let busy=false, depth=0;
 const isFileDrag=e=>[...(e.dataTransfer?.types||[])].includes("Files");
 const modalOpen=()=>Boolean(document.querySelector("dialog[open]"));
 const queuedForm=form=>["/upload","/images/upload","/temp"].includes(form?.getAttribute("action"));
 const forms=[...document.querySelectorAll("#upload-form,[data-quick-upload]")];
 const primaryPage=["/upload","/images/upload","/temp"].includes(location.pathname)||/^\/albums\/[^/]+\/contribute\/?$/.test(location.pathname);
 const primaryAction=location.pathname.replace(/\/contribute\/?$/,"/upload");
 const primary=primaryPage ? forms.find(form=>form.getAttribute("action")===primaryAction&&form.querySelector('input[type=file]')) : null;
 const zoneOf=form=>form?.querySelector("#upload-dropzone,.quick-dock")||form?.querySelector('input[type=file]');
 const isOverlay=target=>Boolean(target?.closest?.("[data-dock-overlay]"));
 function dropTarget(e){
  // A stale/retargeted overlay event must never decide business ownership.
  const hit=document.elementFromPoint(e.clientX,e.clientY);
  return hit&&!isOverlay(hit) ? hit : !isOverlay(e.target) ? e.target : document.body;
 }
 function owner(target){
  if(primary)return {form:primary};
  const form=target?.closest?.("form");
  if(form){
   const zone=zoneOf(form);
   return form.querySelector('input[type=file]')&&zone?.contains(target) ? {form} : {blocked:true};
  }
  return {};
 }
 function clearVisual(){overlay.classList.remove("is-active");overlay.setAttribute("aria-hidden","true");forms.forEach(form=>zoneOf(form)?.classList.remove("is-over"));}
 function hide(){depth=0;clearVisual();}
 function show(target){
  clearVisual();const {form,blocked}=owner(target);
  if(form)zoneOf(form)?.classList.add("is-over");
  else if(!blocked&&!primaryPage){overlay.classList.add("is-active");overlay.setAttribute("aria-hidden","false");}
 }
 document.addEventListener("dragenter",e=>{if(!isFileDrag(e))return;e.preventDefault();if(modalOpen()){hide();return;}depth++;show(e.target);});
 document.addEventListener("dragleave",e=>{if(!isFileDrag(e))return;depth=Math.max(0,depth-1);if(depth===0&&!document.documentElement.contains(e.relatedTarget))hide();});
 document.addEventListener("dragover",e=>{if(isFileDrag(e)){e.preventDefault();e.dataTransfer.dropEffect="copy";if(modalOpen()){hide();return;}show(e.target);}});
 document.addEventListener("drop",e=>{if(!isFileDrag(e))return;e.preventDefault();hide();if(modalOpen())return;
  const {form,blocked}=owner(dropTarget(e)), files=e.dataTransfer.files;
  if(blocked){toast("请在上传选择区拖入文件。");return;}
  if(form&&!queuedForm(form)){
   // Retain native contribution submission and consent validation.
   const input=form.querySelector('input[type=file]');
   try { const transfer=new DataTransfer();for(const file of files)transfer.items.add(file);input.files=transfer.files;input.dispatchEvent(new Event("change",{bubbles:true})); }
   catch {toast("请点击选择照片，再确认投稿授权。");}
   return;
  }
  enqueue(files,form);});
 window.addEventListener("blur",hide);
 document.addEventListener("dragend",hide);
 document.addEventListener("keydown",e=>{if(e.key==="Escape")hide();});
 document.addEventListener("paste",e=>{
  if(modalOpen()||e.target.closest("input,textarea,select,[contenteditable=true]"))return;
  const images=[...(e.clipboardData?.items||[])].filter(i=>i.kind==="file"&&i.type.startsWith("image/")).map(i=>i.getAsFile()).filter(Boolean);
  const {form,blocked}=owner(e.target);if(images.length&&!blocked&&(!form||queuedForm(form))){e.preventDefault();enqueue(images,form);}
 });
 tray.querySelector("[data-tray-toggle]").addEventListener("click",e=>{const collapsed=tray.classList.toggle("is-collapsed");e.target.textContent=collapsed?"展开":"收起";e.target.setAttribute("aria-expanded",String(!collapsed));});
 forms.forEach(form=>{
  const input=form.querySelector('input[type=file]');
  // Read current form options at submit/drop, never at file-selection time.
  if(queuedForm(form))form.addEventListener("submit",e=>{if(!input.files.length)return;e.preventDefault();enqueue(input.files,form,e.submitter);input.value="";});
  input.addEventListener("change",()=>{const selected=form.querySelector("#selected-files");if(selected)selected.textContent=[...input.files].map(f=>f.name).join("、");});
  const zone=form.querySelector("#upload-dropzone");
  zone?.addEventListener("click",e=>{if(e.target!==input)input.click();});
  zone?.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();input.click();}});
 });
 function enqueue(files,form,submitter) {
  const selected=[...files];if(selected.length>20){toast("每次最多 20 个文件，请重新选择。");return;}
  if(!selected.length)return;
  const defaults=form ? new FormData(form,submitter) : null;
  const allImages=selected.every(f=>f.type.startsWith("image/"));
  selected.forEach(file=>{
   const row=document.createElement("li");row.className="tray-item";row.dataset.state="queued";
   const kind=document.createElement("span");kind.className="tag-kind";kind.dataset.kind=file.type.startsWith("image/")?"image":"other";kind.textContent=(file.name.split(".").pop()||"FILE").slice(0,5);
   const name=document.createElement("span");name.className="name";name.textContent=file.name;
   const state=document.createElement("span");state.className="state";state.textContent="等待靠岸";
   const bar=document.createElement("span");bar.className="bar";bar.append(document.createElement("i"));row.append(kind,name,state,bar);list.append(row);
   const fields=new FormData();
   const fileField=form?.querySelector('input[type=file]').name||'file';
   if(defaults)for(const [key,value]of defaults)if(key!==fileField)fields.append(key,value);
   if(!form){fields.set("csrf_token",document.body.dataset.csrf);fields.set("purpose",allImages?"image":"other");}
   const task={file,fields,row,state,done:false,url:submitter?.getAttribute("formaction")||form?.getAttribute("action")||"/upload",fileField};queue.push(task);
  });
  tray.hidden=false;tray.classList.remove("is-collapsed");tray.querySelector("[data-tray-refresh]").hidden=true;run();
 }
 async function run(){
  if(busy)return;busy=true;
  for(const task of queue.filter(t=>!t.done&&t.row.dataset.state==="queued"))await send(task);
  busy=false;tray.querySelector("[data-tray-summary]").textContent=queue.filter(t=>t.row.dataset.state==="done").length+" / "+queue.length;
  if(queue.some(t=>!t.done&&t.row.dataset.state==="queued")){run();return;}
  tray.querySelector("[data-tray-refresh]").hidden=false;toast("装卸处理结束，请查看各项结果。");
 }
 function send(task){
  task.row.dataset.state="uploading";task.state.textContent="开始靠岸…";const started=performance.now();
  return new Promise(resolve=>{
   const xhr=new XMLHttpRequest();xhr.open("POST",task.url);xhr.setRequestHeader("Accept","application/json");xhr.timeout=600000;
   xhr.upload.onprogress=e=>{if(e.lengthComputable){const pct=Math.round(e.loaded*100/e.total);task.row.style.setProperty("--p",pct);task.state.textContent=pct+"% · "+(e.loaded/1024/Math.max(.1,(performance.now()-started)/1000)).toFixed(0)+" KB/s";}};
   let settled=false;
   const finish=(unknown=false)=>{
    if(settled)return;settled=true;
    let data;try{data=JSON.parse(xhr.responseText);}catch{}
    const saved=data?.file_id ? data : data?.uploaded?.[0]?.file;
    if(!unknown&&xhr.status>=200&&xhr.status<300&&saved?.file_id){
     task.done=true;task.row.dataset.state="done";task.row.style.setProperty("--p",100);task.state.replaceChildren("已靠岸 · ");
     const link=document.createElement("a");link.href=(task.fields.get("purpose")==="image"?"/images/":"/files/")+encodeURIComponent(saved.file_id);link.textContent="查看";task.state.append(link);task.file=null;task.fields=null;
    }else if(!unknown&&xhr.status>=400&&xhr.status<=599){
     task.row.dataset.state="error";task.state.textContent=data?.message||"上传未完成，请重试。";
     const retry=document.createElement("button");retry.className="btn btn-sm";retry.type="button";retry.textContent="重试";
     retry.addEventListener("click",()=>{task.row.dataset.state="queued";retry.remove();run();},{once:true});task.state.append(retry);
    }else{
     task.row.dataset.state="unknown";task.state.textContent="结果未知 · 请先到文件库/图片库核对 · ";
     const link=document.createElement("a");link.href=task.fields.get("purpose")==="image"?"/images":"/files";link.textContent="前往核对";task.state.append(link);
     task.file=null;task.fields=null;
    }resolve();
   };
   xhr.onload=()=>finish();xhr.onerror=()=>finish(true);xhr.ontimeout=()=>finish(true);xhr.onabort=()=>finish(true);
   const data=new FormData();for(const [k,v]of task.fields)data.append(k,v);data.append(task.fileField,task.file);xhr.send(data);
  });
 }
}
