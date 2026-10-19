import { toast } from "./notice.js";
export function initCopy() {
 document.addEventListener("click", async e => {
  const b = e.target.closest("[data-copy],[data-copy-target]"); if (!b) return;
  const target = b.dataset.copyTarget ? document.getElementById(b.dataset.copyTarget) : null;
  const text = b.dataset.copy ?? target?.value ?? target?.textContent ?? "";
  try {
   if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
   await navigator.clipboard.writeText(text);
   const label = b.textContent; b.textContent = "已复制"; b.classList.add("is-copied"); toast("已复制");
   setTimeout(() => { b.textContent = label; b.classList.remove("is-copied"); }, 1800);
  } catch {
   const input = target || document.createElement("textarea");
   if (!target) { input.value = text; input.className = "copy-fallback input"; b.after(input); }
   input.focus();
   if (typeof input.select === "function") input.select();
   else { const range = document.createRange(); range.selectNodeContents(input); const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); }
   toast("已选中文本，请按 Ctrl+C 或长按复制。");
  }
 });
}
