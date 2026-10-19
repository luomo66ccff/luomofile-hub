export function initSelect() {
 const form = document.querySelector("#files-bulk-form"); if (!form) return;
 const boxes = [...form.querySelectorAll('input[name="file_ids"]')], all = form.querySelector("#select-all-page"), bar = form.querySelector("[data-bulk-bar]");
 let last = -1;
 const update = () => {
  const count = boxes.filter(b => b.checked).length;
  if (bar) { bar.hidden = !count; bar.querySelector("[data-selected-count]").textContent = count; }
  if (all) { all.checked = boxes.length > 0 && count === boxes.length; all.indeterminate = count > 0 && count < boxes.length; }
  boxes.forEach(b => b.closest("tr,article")?.classList.toggle("is-selected", b.checked));
 };
 boxes.forEach((b, i) => b.addEventListener("click", e => {
  if (e.shiftKey && last >= 0) boxes.slice(Math.min(last,i),Math.max(last,i)+1).forEach(x => { x.checked = b.checked; });
  last = i; update();
 }));
 all?.addEventListener("change", () => { boxes.forEach(b => { b.checked = all.checked; }); update(); });
 form.querySelector("[data-selection-clear]")?.addEventListener("click", () => { boxes.forEach(b => { b.checked=false; }); update(); });
 update();
}