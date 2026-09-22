document.addEventListener("click", async (event) => {
  const review = event.target.closest(".advisor-review");
  if (review) {
    const action = review.dataset.action;
    const label = action === "approve" ? "批准采用这条外部 AI 建议？" : "拒绝这条外部 AI 建议？";
    if (!window.confirm(label)) return;
    markBusy(review);
    try {
      // The same review button serves the advisor inbox and the condensation
      // proposals; the element says which endpoint it belongs to.
      const endpoint = review.dataset.endpoint || "/api/advisor/submissions";
      const response = await fetch(`${endpoint}/${review.dataset.id}/${action}`, {method: "POST", credentials: "same-origin"});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "操作失败");
      window.location.reload();
    } catch (error) {
      window.alert(error.message);
      clearBusy(review);
    }
    return;
  }
  const archive = event.target.closest(".guidance-archive");
  if (archive) {
    if (!window.confirm("停止采用这条建议？历史记录仍会保留。")) return;
    markBusy(archive);
    try {
      const response = await fetch(`/api/guidance/${archive.dataset.id}/archive`, {method: "POST", credentials: "same-origin"});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "操作失败");
      window.location.reload();
    } catch (error) {
      window.alert(error.message);
      clearBusy(archive);
    }
    return;
  }
  const regenerate = event.target.closest(".regenerate-report");
  if (regenerate) {
    const scope = regenerate.dataset.scope;
    const key = regenerate.dataset.key;
    const row = regenerate.closest(".report-actions");
    const picker = row?.querySelector(".regenerate-model");
    const status = row?.querySelector(".form-status");
    const model = picker ? picker.value : null;
    if (!window.confirm(`用 ${model || "当前模型"} 重新生成「${key}」？`)) return;
    const idleLabel = regenerate.textContent;
    // React immediately: the request can take a moment and the run itself takes
    // minutes, so the button must say what it is doing and refuse a second click.
    regenerate.disabled = true;
    regenerate.textContent = "正在提交…";
    if (picker) picker.disabled = true;
    if (status) status.textContent = "正在提交重新生成请求…";
    try {
      const response = await fetch("/api/reports/regenerate", {
        method: "POST",
        headers: {"Content-Type": "application/json", "Accept": "application/json"},
        credentials: "same-origin",
        body: JSON.stringify({scope, key, model})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "操作失败");
      regenerate.textContent = "正在重新生成…";
      if (status) status.textContent = "已开始，页面会自动刷新；完成后这里显示新报告。";
      // Reload so the running state comes from the server and stays visible.
      window.setTimeout(() => window.location.reload(), 1200);
    } catch (error) {
      window.alert(error.message);
      regenerate.disabled = false;
      regenerate.textContent = idleLabel;
      if (picker) picker.disabled = false;
      if (status) status.textContent = "";
    }
    return;
  }
  const distill = event.target.closest(".distill-report");
  if (distill) {
    const scope = distill.dataset.scope || "period";
    const key = distill.dataset.key;
    if (!window.confirm("让本地模型对照其他模型的报告，另存一份蒸馏版？原报告不会被改动。")) return;
    const row = distill.closest(".report-actions");
    const status = row?.querySelector(".form-status");
    const idleLabel = distill.textContent;
    distill.disabled = true;
    distill.textContent = "正在提交…";
    if (status) status.textContent = "正在提交蒸馏请求…";
    try {
      const response = await fetch("/api/reports/distill", {
        method: "POST",
        headers: {"Content-Type": "application/json", "Accept": "application/json"},
        credentials: "same-origin",
        body: JSON.stringify({scope, key})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "操作失败");
      distill.textContent = "正在蒸馏…";
      if (status) status.textContent = `正在学习 ${data.teacher} 的报告；完成后这里会出现「蒸馏版」。`;
      window.setTimeout(() => window.location.reload(), 1200);
    } catch (error) {
      window.alert(error.message);
      distill.disabled = false;
      distill.textContent = idleLabel;
      if (status) status.textContent = "";
    }
    return;
  }
  const button = event.target.closest(".feedback-action");
  if (!button) return;
  const action = button.dataset.action;
  const id = button.dataset.id;
  let note = null;
  if (action === "confirm") {
    if (!window.confirm("确认这个结论是正确的吗？")) return;
  } else if (action === "reject") {
    note = window.prompt("可选：这个规律不准确，因为……", "");
    if (note === null) return;
  } else {
    return;
  }
  markBusy(button);
  try {
    const response = await fetch(`/api/patterns/${id}/${action}`, {
      method: "POST",
      headers: {"Content-Type": "application/json", "Accept": "application/json"},
      credentials: "same-origin",
      body: JSON.stringify({note})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "操作失败");
    window.alert(action === "confirm" ? "✓ 已确认为长期习惯" : "已否定。AI 后续分析时会将此反馈视为负面证据。");
    window.location.reload();
  } catch (error) {
    window.alert(error.message);
    clearBusy(button);
  }
});

for (const picker of document.querySelectorAll(".regenerate-model[data-third-party]")) {
  const external = (picker.dataset.thirdParty || "").split(",").filter(Boolean);
  const warning = picker.closest(".report-actions")?.querySelector(".provider-warning");
  if (!warning || !external.length) continue;
  // Say it at the point of choice: selecting an external model sends household data out.
  const sync = () => { warning.hidden = !external.includes(picker.value); };
  picker.addEventListener("change", sync);
  sync();
}

const autoModels = document.querySelector(".auto-models");
if (autoModels) {
  const warning = autoModels.querySelector("#auto-warning");
  const thirdParty = (autoModels.dataset.thirdParty || "").split(",").filter(Boolean);
  const sync = () => {
    const chosen = autoModels.querySelector('input[name="automatic_model"]:checked')?.value;
    if (warning) warning.hidden = !thirdParty.includes(chosen);
  };
  autoModels.addEventListener("change", sync);
  sync();
}

// One busy state for every action: a disabled button with a spinner and a label that
// says what is happening. Clearing it restores the original label, so a failure
// leaves the button usable.
function markBusy(button) {
  if (!button) return;
  if (button.dataset.idleLabel === undefined) button.dataset.idleLabel = button.textContent;
  button.disabled = true;
  button.classList.add("is-busy");
  button.textContent = button.dataset.busyLabel || "处理中…";
}

function clearBusy(button) {
  if (!button) return;
  button.disabled = false;
  button.classList.remove("is-busy");
  if (button.dataset.idleLabel !== undefined) button.textContent = button.dataset.idleLabel;
}

// Any form without a dedicated handler still gets feedback. A button that conveys its
// intent through its own name/value is left to its own handler, so disabling it can
// never drop that value from the request.
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (form.classList.contains("guidance-form") || form.classList.contains("settings-form")) return;
  const button = event.submitter || form.querySelector('button[type="submit"]');
  if (button && !button.name) markBusy(button);
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!form.classList.contains("settings-form")) return;
  const button = event.submitter || form.querySelector('button[type="submit"]');
  markBusy(button);
  const status = form.querySelector(".form-status");
  if (status && button?.dataset.busyLabel) status.textContent = button.dataset.busyLabel;
});

const clearKeyButton = document.querySelector("[data-clear-key]");
if (clearKeyButton) {
  clearKeyButton.addEventListener("click", () => {
    const form = clearKeyButton.closest("form");
    if (!window.confirm("清除已保存的密钥？清除后需要重新填写才能使用第三方模型。")) return;
    form.querySelector('input[name="clear_key"]').value = "1";
    markBusy(clearKeyButton);
    form.requestSubmit();
  });
}

// Live views (busy queue / running report) used to hard-reload every 20s even
// while the owner was mid-click, which felt like the page froze. Poll only when
// the tab is visible, skip if a form is focused, and wait a bit longer.
function scheduleLiveReload(ms) {
  window.setTimeout(() => {
    if (document.visibilityState !== "visible") {
      scheduleLiveReload(ms);
      return;
    }
    const active = document.activeElement;
    if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT" || active.isContentEditable)) {
      scheduleLiveReload(ms);
      return;
    }
    window.location.reload();
  }, ms);
}
if (document.querySelector("[data-queue]")) {
  scheduleLiveReload(30000);
}

// A group menu opened by click should close when the click lands elsewhere.
document.addEventListener("click", (event) => {
  for (const group of document.querySelectorAll(".nav-group[open]")) {
    if (!group.contains(event.target)) group.open = false;
  }
});

const runningReport = document.querySelector('.regenerate-report[data-running="1"]');
if (runningReport) {
  scheduleLiveReload(30000);
}

document.addEventListener("submit", async (event) => {
  const form = event.target.closest(".guidance-form");
  // The settings form carries .guidance-form for its styling only and must reach
  // the server as an ordinary POST. Without this exclusion the handler below
  // called preventDefault() on it, so the browser never submitted: the button sat
  // at "正在保存并测试连接…" forever and nothing was saved (2026-09-14).
  if (!form || form.classList.contains("settings-form")) return;
  event.preventDefault();
  const content = new FormData(form).get("content")?.trim();
  const scope = new FormData(form).get("scope") || "all";
  const button = form.querySelector("button[type=submit]");
  const status = form.querySelector(".form-status");
  if (!content) {
    // Say so rather than doing nothing: a form that silently ignores a click is
    // indistinguishable from a broken page.
    if (status) status.textContent = "请先填写内容。";
    return;
  }
  markBusy(button);
  try {
    const response = await fetch("/api/guidance", {method: "POST", headers: {"Content-Type": "application/json", "Accept": "application/json"}, credentials: "same-origin", body: JSON.stringify({content, scope})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "提交失败");
    status.textContent = "已保存，将从下一次分析开始采用。";
    form.reset();
    window.setTimeout(() => window.location.reload(), 700);
  } catch (error) {
    status.textContent = error.message;
    clearBusy(button);
  }
});

// Light/dark toggle in the header. The pre-paint script in base.html has already
// set html[data-theme]; this only flips it, persists the choice, and keeps the
// switch UI (thumb, aria-pressed, accessible name) in sync.
(function () {
  const button = document.querySelector("[data-theme-toggle]");
  if (!button) return;
  const root = document.documentElement;

  function currentTheme() {
    return root.getAttribute("data-theme") === "dark" ? "dark" : "light";
  }

  function syncToggle(theme) {
    const dark = theme === "dark";
    button.setAttribute("aria-pressed", dark ? "true" : "false");
    button.setAttribute("aria-label", dark ? "切换浅色模式" : "切换深色模式");
    button.setAttribute("title", dark ? "切换到浅色" : "切换到深色");
  }

  syncToggle(currentTheme());

  button.addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    root.style.colorScheme = next;
    try { localStorage.setItem("home-ai-theme", next); } catch (e) {}
    syncToggle(next);
    // Brief press feedback beyond :active so a quick click still reads as a switch.
    button.classList.add("is-switching");
    window.setTimeout(() => button.classList.remove("is-switching"), 280);
  });
})();
