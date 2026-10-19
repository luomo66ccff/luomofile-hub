export function initLightbox() {
 const box=document.querySelector("[data-lightbox]"); if(!box?.showModal)return;
 const items=[...document.querySelectorAll("[data-lightbox-item]")], image=box.querySelector("[data-lightbox-image]");
 let index=0, trigger, startX;
 function show(i) {
  index=(i+items.length)%items.length;const item=items[index];
  image.src=item.dataset.preview;image.alt=item.dataset.title||"图片";
  box.querySelector("[data-lightbox-counter]").textContent=(index+1)+" / "+items.length;
  box.querySelector("[data-lightbox-caption]").textContent=item.dataset.caption||"";
  const detail=box.querySelector("[data-lightbox-open]");detail.href=item.getAttribute("href");detail.textContent=item.dataset.detailLabel||"查看详情";
  [-1,1].forEach(d=>{const adjacent=new Image();adjacent.src=items[(index+d+items.length)%items.length].dataset.preview;});
 }
 items.forEach((item,i)=>item.addEventListener("click",e=>{e.preventDefault();trigger=item;show(i);box.showModal();box.querySelector("[data-lightbox-close]").focus();}));
 box.querySelector("[data-lightbox-close]").addEventListener("click",()=>box.close());
 box.querySelector("[data-lightbox-prev]").addEventListener("click",()=>show(index-1));
 box.querySelector("[data-lightbox-next]").addEventListener("click",()=>show(index+1));
 box.addEventListener("keydown",e=>{if(e.key==="ArrowRight"||e.key==="ArrowLeft"){e.preventDefault();show(index+(e.key==="ArrowRight"?1:-1));}});
 box.addEventListener("touchstart",e=>{startX=e.changedTouches[0].clientX;},{passive:true});
 box.addEventListener("touchend",e=>{const delta=e.changedTouches[0].clientX-startX;if(Math.abs(delta)>45)show(index+(delta<0?1:-1));},{passive:true});
 box.addEventListener("close",()=>{image.removeAttribute("src");trigger?.focus();});
}
