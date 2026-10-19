export function initSubscriptions() {
    const manager = document.querySelector("[data-subscription-manager]");
    if (!manager || !window.fetch || !window.FormData) return;
    const find = (selector) => manager.querySelector(selector);
    const results = find("[data-subscription-results]");
    const form = find("[data-subscription-manager-form]");
    const submit = find("[data-create-submit]");
    const next = find("[data-create-next]");
    const feedback = find("[data-subscription-feedback]");
    const search = find("[data-subscription-search]");
    const statusFilter = find("[data-subscription-status-filter]");
    const refreshStatus = find("[data-subscription-refresh-status]");
    let timer = null;
    let refreshing = false;
    let stopped = false;
    let creating = false;
    let submitted = null;
    let trackedTask = null;
    let deferredData = null;
    let lastCounts = {};
    let retryDelay = 1800;

    const localTime = (value) => {
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(date);
    };
    const formatDates = (root) => {
      root.querySelectorAll("[data-subscription-time]").forEach((time) => { time.textContent = localTime(time.dateTime); });
    };
    const message = (text, type = "") => {
      feedback.textContent = text;
      feedback.hidden = !text;
      feedback.classList.toggle("is-error", type === "error");
      feedback.classList.toggle("is-success", type === "success");
      feedback.setAttribute("role", type === "error" ? "alert" : "status");
    };
    const lockFields = (locked) => {
      form.querySelectorAll('input:not([type="hidden"]), select').forEach((input) => { input.disabled = locked; });
    };
    const requestJSON = async (url, options = {}) => {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), options.timeout || 20000);
      try {
        const response = await fetch(url, {
          credentials: "same-origin", cache: "no-store", redirect: "error",
          ...options, headers: { Accept: "application/json", ...options.headers }, signal: controller.signal,
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          const error = new Error(typeof data.message === "string" ? data.message : "暂时无法完成操作，请刷新状态后再试。");
          error.status = response.status;
          throw error;
        }
        return data;
      } finally {
        window.clearTimeout(timeout);
      }
    };

    const changeFormat = (picker) => {
      const select = picker.querySelector("[data-subscription-format]");
      const input = picker.querySelector("[data-subscription-url]");
      const url = new URL(input.dataset.baseUrl, window.location.origin);
      if (select.value === "mihomo") url.searchParams.delete("format");
      else url.searchParams.set("format", select.value);
      input.value = url.href;
      picker.querySelector("[data-format-description]").textContent = select.selectedOptions[0]?.dataset.description || "";
      picker.querySelector("[data-copy-feedback]").textContent = "";
    };
    const prepareResults = () => {
      results.querySelectorAll("[data-format-apply], [data-revoke-cancel]").forEach((button) => {
        button.hidden = button.hasAttribute("data-format-apply");
      });
      formatDates(results);
    };
    const applyFilters = () => {
      const query = search.value.trim().toLocaleLowerCase();
      const chosen = statusFilter.value;
      const cards = results.querySelectorAll("[data-subscription-link]");
      let visible = 0;
      cards.forEach((card) => {
        const state = card.dataset.linkStatus;
        const pending = state === "queued" || state === "processing";
        const closed = !pending && state !== "active" && state !== "provision_failed";
        const matchesState = chosen === "all" || chosen === state || (chosen === "pending" && pending) || (chosen === "closed" && closed);
        const matchesLabel = card.dataset.linkLabel.toLocaleLowerCase().includes(query);
        card.hidden = !(matchesState && matchesLabel);
        if (!card.hidden) visible += 1;
      });
      find("[data-filter-count]").textContent = visible + " / " + cards.length + " 条";
      find("[data-subscription-no-match]").hidden = cards.length === 0 || visible !== 0;
    };
    const finishTask = (state) => {
      submit.hidden = true;
      next.hidden = false;
      const active = state === "active";
      message(active ? "订阅已创建，可在下方选择客户端格式并复制链接。" : "本次创建未签发可用链接。查看下方状态后，可开始新的创建。", active ? "success" : "error");
    };
    const applyStatus = (data, force = false) => {
      lastCounts = data.counts;
      manager.querySelectorAll("[data-sub-count]").forEach((counter) => { counter.textContent = data.counts[counter.dataset.subCount]; });
      const health = data.node_health;
      const badge = find("[data-node-badge]");
      badge.textContent = health.fresh ? "节点已确认" : "节点状态待更新";
      badge.className = "badge";
      badge.dataset.tone = health.fresh ? "ok" : "private";
      find("[data-node-targets]").textContent = health.target_count;
      find("[data-node-identities]").textContent = health.identity_count;
      find("[data-node-success]").textContent = health.last_success_at ? localTime(health.last_success_at) : "尚无记录";
      // Only the list is replaced; editable creation fields and their request key stay intact.
      if (!force && (results.contains(document.activeElement) || results.querySelector("[data-revoke-confirmation][open]"))) {
        deferredData = data;
      } else {
        const formats = new Map([...results.querySelectorAll("[data-subscription-link]")].map((card) => [
          card.dataset.subscriptionLink, card.querySelector("[data-subscription-format]")?.value,
        ]));
        results.innerHTML = data.html;
        results.querySelectorAll("[data-subscription-link]").forEach((card) => {
          const select = card.querySelector("[data-subscription-format]");
          const chosen = formats.get(card.dataset.subscriptionLink);
          if (select && chosen && [...select.options].some((option) => option.value === chosen)) {
            select.value = chosen;
            changeFormat(card.querySelector("[data-client-picker]"));
          }
        });
        prepareResults();
        applyFilters();
        deferredData = null;
      }
      const task = data.tasks.find((item) => item.id === trackedTask);
      if (task) {
        const pending = task.status === "queued" || task.status === "processing";
        find("[data-task-heading]").textContent = task.status === "queued" ? "请求已排队" : task.status === "processing" ? "正在同步节点" : task.status === "active" ? "订阅链接已可用" : "创建请求已结束";
        find("[data-task-message]").textContent = pending ? "所有地区确认前不会显示可用链接，请保留此页等待结果。" : task.status === "active" ? "在链接管理中选择对应客户端，再复制订阅链接。" : "本次请求不会再次签发；开始新请求前请先查看链接状态。";
        if (!pending && next.hidden) finishTask(task.status);
      } else if (trackedTask === null) {
        find("[data-task-heading]").textContent = data.counts.pending ? "有请求正在创建" : "当前没有等待创建的请求";
        find("[data-task-message]").textContent = data.counts.pending ? "下方列表分别显示排队和节点同步阶段。" : "填写创建表单，完成节点同步后即可复制订阅。";
      }
      const pending = data.tasks.some((task) => ["queued", "processing", "revoke_pending", "expired_pending"].includes(task.status));
      refreshStatus.textContent = pending ? "正在自动更新进度，填写表单不会被清空。" : "状态已更新；需要时可手动刷新。";
      return pending;
    };
    const schedule = (delay = retryDelay) => {
      window.clearTimeout(timer);
      if (!stopped) timer = window.setTimeout(() => refresh(), delay);
    };
    const refresh = async (force = false) => {
      if (refreshing) return;
      if (document.hidden && !force) { schedule(5000); return; }
      refreshing = true;
      window.clearTimeout(timer);
      try {
        const data = await requestJSON("/admin/subscriptions/status");
        retryDelay = 1800;
        if (applyStatus(data, force)) schedule();
      } catch (error) {
        if (error.status === 401 || error.status === 403) {
          stopped = true;
          message("登录状态需要更新，请重新打开订阅管理页。已提交的请求结果请在登录后查看。", "error");
        } else {
          refreshStatus.textContent = "暂时无法更新状态，表单已保留，稍后会重试。";
          retryDelay = Math.min(retryDelay * 2, 10000);
          schedule();
        }
      } finally {
        refreshing = false;
      }
    };

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (creating) return;
      creating = true;
      if (submitted === null) submitted = new URLSearchParams(new FormData(form));
      submit.disabled = true;
      submit.textContent = "正在提交…";
      lockFields(true);
      try {
        const data = await requestJSON(form.action, { method: "POST", body: submitted });
        trackedTask = data.request.id;
        submit.textContent = "请求已受理，等待结果…";
        message("请求已受理，正在等待节点同步结果。此时还没有可用链接。");
        await refresh(true);
        schedule();
      } catch (error) {
        if (error.status) {
          submitted = null;
          lockFields(false);
          message(error.message, "error");
          submit.textContent = "创建受保护链接";
          if (error.status === 409) { submit.hidden = true; next.hidden = false; }
        } else {
          // The response may have been lost after acceptance. Retry the exact same request.
          message("暂时无法确认提交结果。点击重试会使用同一请求，避免重复创建。", "error");
          submit.textContent = "重试确认结果";
        }
        submit.disabled = false;
      } finally {
        creating = false;
      }
    });
    next.addEventListener("click", async () => {
      next.disabled = true;
      try {
        const response = await fetch("/admin/subscriptions", { credentials: "same-origin", cache: "no-store", redirect: "error", headers: { Accept: "text/html" } });
        if (!response.ok) throw new Error("无法准备新请求");
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        const key = page.querySelector('[name="idempotency_key"]')?.value;
        if (!key) throw new Error("没有新的请求标识");
        form.elements.idempotency_key.value = key;
        submitted = null;
        trackedTask = null;
        form.reset();
        // reset() restores the hidden field's initial value as well.
        form.elements.idempotency_key.value = key;
        lockFields(false);
        submit.hidden = false;
        submit.disabled = false;
        submit.textContent = "创建受保护链接";
        next.hidden = true;
        message("");
        form.elements.label.focus();
        await refresh();
      } catch (_) {
        message("暂时无法准备新请求，请重试；原请求和链接状态已保留。", "error");
      } finally {
        next.disabled = false;
      }
    });

    manager.addEventListener("submit", async (event) => {
      const actionForm = event.target.closest("[data-subscription-action]");
      if (event.target.matches("[data-format-form]")) { event.preventDefault(); return; }
      if (!actionForm) return;
      event.preventDefault();
      const button = actionForm.querySelector('button[type="submit"]');
      if (button.disabled) return;
      button.disabled = true;
      try {
        const data = await requestJSON(actionForm.action, { method: "POST", timeout: 120000 });
        actionForm.closest("[data-revoke-confirmation]")?.removeAttribute("open");
        message(data.message || "操作已完成。", "success");
        await refresh(true);
      } catch (error) {
        message(error.status ? error.message : "暂时无法确认操作结果，请刷新状态后查看，避免重复操作。", "error");
      } finally {
        button.disabled = false;
      }
    });
    manager.addEventListener("change", (event) => {
      if (event.target.matches("[data-subscription-format]")) changeFormat(event.target.closest("[data-client-picker]"));
    });
    manager.addEventListener("click", async (event) => {
      const copy = event.target.closest("[data-subscription-copy]");
      const cancel = event.target.closest("[data-revoke-cancel]");
      if (cancel) cancel.closest("[data-revoke-confirmation]").open = false;
      if (event.target.closest("[data-subscription-refresh]")) { event.preventDefault(); stopped = false; await refresh(true); }
      if (!copy) return;
      const picker = copy.closest("[data-client-picker]");
      const input = picker.querySelector("[data-subscription-url]");
      const status = picker.querySelector("[data-copy-feedback]");
      let copied = false;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          try { await navigator.clipboard.writeText(input.value); copied = true; } catch (_) { /* Use selection below. */ }
        }
        if (!copied) {
          input.focus();
          input.select();
          copied = document.execCommand("copy");
        }
      } catch (_) { copied = false; }
      if (!copied) { input.focus(); input.select(); }
      status.textContent = copied ? "链接已复制。" : "已选中链接，请按 Ctrl+C 或长按复制。";
    });
    const applyDeferred = () => {
      if (deferredData && !results.contains(document.activeElement) && !results.querySelector("[data-revoke-confirmation][open]")) {
        applyStatus(deferredData, true);
      }
    };
    results.addEventListener("focusout", () => window.setTimeout(applyDeferred, 0));
    results.addEventListener("toggle", applyDeferred, true);
    search.addEventListener("input", applyFilters);
    statusFilter.addEventListener("change", applyFilters);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && (lastCounts.pending || trackedTask !== null)) refresh();
    });
    window.addEventListener("pageshow", (event) => { if (event.persisted) refresh(); });
    find("[data-subscription-filters]").hidden = false;
    form.querySelectorAll("[data-file-updated]").forEach((option) => {
      option.textContent = option.dataset.fileName + " · " + option.dataset.fileStorage + (option.dataset.fileUpdated ? " · " + localTime(option.dataset.fileUpdated) + " 更新" : "");
    });
    formatDates(manager);
    prepareResults();
    applyFilters();
    refresh();
}

