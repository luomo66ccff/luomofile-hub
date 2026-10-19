import {initCollections} from "./collections.js";
import {initUpload} from "./upload.js";
import {initPalette} from "./palette.js";
import {initSelect} from "./select.js";
import {initLightbox} from "./lightbox.js";
import {initCopy} from "./copy.js";
import {initConfirm} from "./confirm.js";
import {toast} from "./notice.js";
import {initSubscriptions} from "./subscriptions.js";
initCollections();initUpload();initPalette();initSelect();initLightbox();initCopy();initConfirm();initSubscriptions();
document.querySelector("[data-theme-toggle]")?.addEventListener("click",()=>{
 const root=document.documentElement;const dark=root.dataset.theme?root.dataset.theme==="dark":matchMedia("(prefers-color-scheme: dark)").matches;
 root.dataset.theme=dark?"light":"dark";try{localStorage.setItem("harbor-theme",root.dataset.theme);}catch{}
});
document.querySelectorAll("[data-view]").forEach(a=>a.addEventListener("click",()=>{try{localStorage.setItem("harbor-view",a.dataset.view);}catch{}}));
if(location.pathname==="/files"&&!new URLSearchParams(location.search).has("view")){
 try{if(localStorage.getItem("harbor-view")==="grid"){const u=new URL(location);u.searchParams.set("view","grid");location.replace(u);}}catch{}
}
document.querySelectorAll("[data-password-toggle]").forEach(b=>b.addEventListener("click",()=>{const input=document.getElementById(b.dataset.passwordToggle);const show=input.type==="password";input.type=show?"text":"password";b.textContent=show?"隐藏":"显示";b.setAttribute("aria-pressed",String(show));}));
const greeting=document.querySelector("[data-greeting]");if(greeting){const h=new Date().getHours();greeting.textContent=h<6?"夜深了":h<12?"早上好":h<18?"下午好":"晚上好";}
document.querySelectorAll(".notice").forEach(n=>{if(n.textContent.trim())toast(n.textContent.trim());});
document.querySelectorAll("[data-media-image]").forEach(img=>img.addEventListener("error",()=>{img.alt="预览暂不可用";img.classList.add("media-unavailable");}));