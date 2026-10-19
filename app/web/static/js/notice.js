export function toast(text) {
 const host = document.querySelector("[data-toasts]"); if (!host) return;
 const item = document.createElement("div"); item.className = "toast"; item.textContent = text; host.append(item);
 while (host.children.length > 4) host.firstElementChild.remove();
 setTimeout(() => { item.classList.add("is-leaving"); setTimeout(() => item.remove(), 250); }, 4200);
}