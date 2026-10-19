export function initConfirm() {
 const dialog = document.querySelector("[data-confirm-dialog]"); if (!dialog?.showModal) return;
 let pending = null, approved = null, trigger = null;
 document.addEventListener("submit", e => {
  const form = e.target, submitter = e.submitter;
  if (approved === form) { approved = null; return; }
  const text = submitter?.dataset.confirm || form.dataset.confirm; if (!text) return;
  e.preventDefault(); e.stopImmediatePropagation(); if (dialog.open) return;
  pending = {form, submitter}; trigger = submitter || document.activeElement;
  dialog.querySelector("[data-confirm-message]").textContent = text; dialog.showModal();
  dialog.querySelector("[data-confirm-cancel]").focus();
 }, true);
 dialog.querySelector("[data-confirm-cancel]").addEventListener("click", () => dialog.close());
 dialog.addEventListener("close", () => { pending = null; trigger?.focus(); });
 dialog.querySelector("[data-confirm-accept]").addEventListener("click", () => {
  const action = pending; dialog.close(); if (!action) return;
  approved = action.form; action.form.requestSubmit(action.submitter || undefined);
  // An invalid native form never dispatches submit; don't exempt its next attempt.
  approved = null;
 });
}
