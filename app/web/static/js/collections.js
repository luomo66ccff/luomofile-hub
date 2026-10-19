// Presentation of the current result page. No new query parameters or server sorting.
export function initCollections() {
 const collection=document.querySelector('[data-collection]');if(!collection)return;
 const original=[...collection.children];
 document.querySelectorAll('[data-collection-tools]').forEach(el=>{el.hidden=false;});
 document.querySelector('[data-collection-sort]')?.addEventListener('change',e=>{
  const rows=e.target.value==='name'?[...original].sort((a,b)=>{
   const title=el=>el.querySelector('h3 a,.image-info strong')?.textContent.trim()||'';
   return title(a).localeCompare(title(b),'zh-CN',{numeric:true});
  }):original;
  collection.append(...rows);
 });
 document.querySelectorAll('[data-collection-view]').forEach(button=>button.addEventListener('click',()=>{
  collection.classList.toggle('is-list',button.dataset.collectionView==='list');
  document.querySelectorAll('[data-collection-view]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
 }));
}
