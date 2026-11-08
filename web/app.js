const state = {
  config: {
    allowed_extensions: [".md", ".pdf", ".doc", ".docx"],
    max_upload_mb: 200,
    stages: [],
  },
  files: [],
  tasks: [],
  selectedTaskId: null,
  uploading: false,
  published: [],
};

const elements = {
  activeFile: document.querySelector("#activeFile"),
  dropZone: document.querySelector("#dropZone"),
  fileInput: document.querySelector("#fileInput"),
  fileQueue: document.querySelector("#fileQueue"),
  historyCount: document.querySelector("#historyCount"),
  historyList: document.querySelector("#historyList"),
  limitHint: document.querySelector("#limitHint"),
  pipelineList: document.querySelector("#pipelineList"),
  progressBar: document.querySelector("#progressBar"),
  progressLabel: document.querySelector("#progressLabel"),
  progressValue: document.querySelector("#progressValue"),
  resultPanel: document.querySelector("#resultPanel"),
  startButton: document.querySelector("#startButton"),
  systemState: document.querySelector("#systemState"),
  taskStatus: document.querySelector("#taskStatus"),
  toastRegion: document.querySelector("#toastRegion"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function fileKind(filename) {
  const lower = filename.toLowerCase();
  if (lower.endsWith(".pdf")) return "PDF";
  if (lower.endsWith(".doc") || lower.endsWith(".docx")) return "WORD";
  return "MD";
}

function toast(message, isError = false) {
  const item = document.createElement("div");
  item.className = `toast${isError ? " error" : ""}`;
  item.textContent = message;
  elements.toastRegion.append(item);
  window.setTimeout(() => item.remove(), 4200);
}

function renderQueue() {
  if (!state.files.length) {
    elements.fileQueue.innerHTML = '<div class="queue-empty">尚未选择文件</div>';
  } else {
    elements.fileQueue.innerHTML = state.files
      .map(
        (file, index) => `
          <div class="queued-file">
            <span class="file-type">${fileKind(file.name)}</span>
            <div class="file-copy">
              <strong title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</strong>
              <span>${formatBytes(file.size)}</span>
            </div>
            <button class="remove-file" type="button" data-remove-index="${index}" aria-label="移除 ${escapeHtml(file.name)}">×</button>
          </div>`,
      )
      .join("");
  }
  elements.startButton.disabled = !state.files.length || state.uploading;
  elements.startButton.querySelector("span").textContent = state.uploading
    ? "正在上传…"
    : state.files.length > 1
      ? `开始入库（${state.files.length}）`
      : "开始入库";
}

function addFiles(fileList) {
  const maxBytes = state.config.max_upload_mb * 1024 * 1024;
  const allowed = new Set([
    ...(state.config.allowed_extensions || []),
    ...(state.config.table_extensions || []),
  ]);
  for (const file of fileList) {
    const extension = file.name.includes(".") ? `.${file.name.split(".").pop().toLowerCase()}` : "";
    if (!allowed.has(extension)) {
      toast(`${file.name}：仅支持 PDF、Markdown 或 Word`, true);
      continue;
    }
    if (file.size > maxBytes) {
      toast(`${file.name}：超过 ${state.config.max_upload_mb} MB`, true);
      continue;
    }
    const duplicate = state.files.some(
      (item) => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified,
    );
    if (!duplicate) state.files.push(file);
  }
  renderQueue();
}

function renderPipeline(task) {
  const stages = state.config.stages.filter((stage) => !["queued", "completed"].includes(stage.key));
  const currentIndex = stages.findIndex((stage) => stage.key === task?.stage);
  const completed = task?.status === "completed";

  elements.pipelineList.innerHTML = stages
    .map((stage, index) => {
      let className = "";
      if (completed || (currentIndex >= 0 && index < currentIndex)) className = "done";
      if (task?.status === "running" && index === currentIndex) className = "active";
      if (task?.status === "failed" && index === currentIndex) className = "failed";
      const marker = className === "done" ? "✓" : String(index + 1).padStart(2, "0");
      return `
        <li class="pipeline-step ${className}">
          <i class="step-index">${marker}</i>
          <strong>${escapeHtml(stage.label)}</strong>
        </li>`;
    })
    .join("");
}

function renderSelectedTask() {
  const task = state.tasks.find((item) => item.id === state.selectedTaskId) ?? state.tasks[0] ?? null;
  if (task && task.id !== state.selectedTaskId) state.selectedTaskId = task.id;

  if (!task) {
    elements.activeFile.innerHTML = `
      <div class="file-emblem">—</div>
      <div><strong>还没有处理任务</strong><span>上传后可在这里查看每一步进度</span></div>`;
    elements.taskStatus.className = "task-status idle";
    elements.taskStatus.textContent = "等待文件";
    elements.progressLabel.textContent = "等待开始";
    elements.progressValue.textContent = "0%";
    elements.progressBar.style.width = "0%";
    if (!state.tableReview) elements.resultPanel.hidden = true;
    renderPipeline(null);
    return;
  }

  elements.activeFile.innerHTML = `
    <div class="file-emblem">${fileKind(task.filename)}</div>
    <div>
      <strong title="${escapeHtml(task.filename)}">${escapeHtml(task.filename)}</strong>
      <span>${formatBytes(task.size)} · 任务 ${escapeHtml(task.id)}</span>
    </div>`;
  elements.taskStatus.className = `task-status ${task.status}`;
  elements.taskStatus.textContent = {
    queued: "排队中",
    running: "处理中",
    completed: "已完成",
    failed: "失败",
  }[task.status] ?? task.status;
  elements.progressLabel.textContent = task.message || task.stage_label;
  elements.progressValue.textContent = `${task.progress}%`;
  elements.progressBar.style.width = `${task.progress}%`;
  renderPipeline(task);
  if (state.tableReview) return;

  if (task.status === "completed" && task.result) {
    const result = task.result;
    elements.resultPanel.hidden = false;
    elements.resultPanel.innerHTML = `
      <h3>入库结果</h3>
      <div class="result-grid">
        <div><span>识别商品</span><strong title="${escapeHtml(result.item_name || "未识别")}">${escapeHtml(result.item_name || "未识别")}</strong></div>
        <div><span>切片数量</span><strong>${result.chunks_count} chunks</strong></div>
        <div><span>向量维度</span><strong>${result.vector_dimension} dim</strong></div>
        <div><span>Milvus</span><strong>${result.milvus_written ? "写入成功" : "未确认"}</strong></div>
        <div><span>总耗时</span><strong>${result.duration_seconds}s</strong></div>
        <div><span>MinIO 对象</span><strong title="${escapeHtml(result.minio_object)}">${escapeHtml(result.minio_object || "—")}</strong></div>
      </div>`;
  } else if (task.status === "failed") {
    elements.resultPanel.hidden = false;
    elements.resultPanel.innerHTML = `<p class="error-copy"><strong>失败原因：</strong>${escapeHtml(task.error || "未知错误")}</p>`;
  } else {
    elements.resultPanel.hidden = true;
  }
}

function renderHistory() {
  const published = state.published || [];
  elements.historyCount.textContent = `${state.tasks.length + published.length} 个任务`;
  const documents = state.tasks.map((task) => {
    const statusText = task.status === "failed" ? "失败" : `${task.progress}%`;
    return `
      <button class="history-row ${task.id === state.selectedTaskId ? "selected" : ""}" type="button" data-task-id="${task.id}">
        <span class="history-name" title="${escapeHtml(task.filename)}">${escapeHtml(task.filename)}</span>
        <span class="history-stage">${escapeHtml(task.stage_label)}</span>
        <span class="history-progress ${task.status === "failed" ? "failed" : ""}">${statusText}</span>
        <span class="history-time">${formatTime(task.updated_at)}</span>
      </button>`;
  });
  const tables = published.map((item) => `
    <button class="history-row" type="button" data-published="${escapeHtml(item.doc_id)}">
      <span class="history-name" title="${escapeHtml(item.source_file)}">${escapeHtml(item.source_file)}</span>
      <span class="history-stage">表格已发布</span>
      <span class="history-progress">完成</span>
      <span class="history-time">${escapeHtml(item.format || "")}</span>
    </button>`);
  if (!documents.length && !tables.length) {
    elements.historyList.innerHTML = '<div class="history-empty">完成第一次上传后，任务会出现在这里。</div>';
    return;
  }
  elements.historyList.innerHTML = documents.concat(tables).join("");
}

function renderTasks() {
  renderSelectedTask();
  renderHistory();
}

async function loadTasks() {
  try {
    const response = await fetch("/api/tasks", { cache: "no-store" });
    if (!response.ok) throw new Error("无法读取任务状态");
    const data = await response.json();
    if (!Array.isArray(data.tasks)) throw new Error("任务状态格式无效");
    state.tasks = data.tasks;
    elements.systemState.classList.add("ready");
    elements.systemState.querySelector("span:last-child").textContent = "入库服务已连接";
    await loadPublished();
    renderTasks();
  } catch (error) {
    elements.systemState.classList.remove("ready");
    elements.systemState.querySelector("span:last-child").textContent = "入库服务未连接";
  }
}

let taskPollTimer;
let taskPollRunning = false;

async function pollTasks() {
  if (taskPollRunning) return;
  window.clearTimeout(taskPollTimer);
  taskPollRunning = true;
  try {
    await loadTasks();
  } finally {
    taskPollRunning = false;
    const hasActiveTask = state.tasks.some((task) => task.status === "queued" || task.status === "running");
    taskPollTimer = window.setTimeout(pollTasks, document.hidden ? 15000 : hasActiveTask ? 1600 : 8000);
  }
}

function uploadFile(file, sheetName) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const extension = file.name.includes(".") ? `.${file.name.split(".").pop().toLowerCase()}` : "";
    const table = new Set(state.config.table_extensions || []).has(extension);
    const target = table ? "/api/table-imports" : "/api/tasks";
    const sheet = sheetName ? `&sheet_name=${encodeURIComponent(sheetName)}` : "";
    xhr.open("POST", `${target}?filename=${encodeURIComponent(file.name)}${sheet}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.responseType = "json";
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.response);
      else reject(new Error(xhr.response?.error || `上传失败（HTTP ${xhr.status}）`));
    };
    xhr.onerror = () => reject(new Error("网络连接中断"));
    xhr.send(file);
  });
}

async function startUpload() {
  if (!state.files.length || state.uploading) return;
  state.uploading = true;
  renderQueue();
  const pending = [...state.files];
  let accepted = 0;

  for (const file of pending) {
    try {
      const task = await uploadFile(file);
      if (task.review || task.need_sheet || task.duplicate) {
        showTableReview(task, file);
        accepted += 1;
        continue;
      }
      accepted += 1;
      state.tableReview = null;
      state.selectedTaskId = task.id;
      state.tasks = [task, ...state.tasks.filter((item) => item.id !== task.id)];
      renderTasks();
    } catch (error) {
      toast(`${file.name}：${error.message}`, true);
    }
  }

  state.files = [];
  state.uploading = false;
  renderQueue();
  if (accepted) toast(`${accepted} 个文件已进入入库队列`);
  await pollTasks();
}

function showTableReview(draft, file) {
  state.tableReview = draft;
  state.tableFile = file || state.tableFile;
  elements.resultPanel.hidden = false;
  if (draft.need_sheet) {
    const buttons = (draft.sheets || []).map((name) => `<button type="button" data-sheet="${escapeHtml(name)}">${escapeHtml(name)}</button>`).join("");
    elements.resultPanel.innerHTML = `<h3>选择工作表</h3><p class="review-note">这份表格有多张表。选定一张后才会登记，一文件只建一张业务表。</p><div class="chip-row">${buttons}</div>`;
    elements.resultPanel.querySelectorAll("[data-sheet]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          const next = await uploadFile(state.tableFile, button.dataset.sheet);
          showTableReview(next, state.tableFile);
        } catch (error) {
          toast(error.message, true);
        }
      });
    });
    return;
  }
  if (draft.duplicate) {
    elements.resultPanel.innerHTML = "<p>原件、工作表和导入配置完全相同，已沿用现有版本。</p>";
    return;
  }
  const roles = ["维度", "度量", "时间", "主键", "属性"];
  const types = ["text", "bigint", "numeric", "boolean", "date", "timestamptz"];
  const fields = (draft.columns || []).map((column, index) => {
    const label = column.aliases?.[0] || column.column_name;
    return `
      <tr>
        <th>${escapeHtml(label)}</th>
        <td><input data-field="description" data-index="${index}" value="${escapeHtml(column.description)}" /></td>
        <td><select data-field="role" data-index="${index}">${roles.map((role) => `<option ${role === column.role ? "selected" : ""}>${role}</option>`).join("")}</select></td>
        <td><select data-field="data_type" data-index="${index}">${types.map((type) => `<option ${type === column.data_type ? "selected" : ""}>${type}</option>`).join("")}</select></td>
        <td><label><input data-field="value_index" data-index="${index}" type="checkbox" /> 值索引</label></td>
      </tr>`;
  }).join("");
  elements.resultPanel.innerHTML = `
    <h3>确认字段后才发布</h3>
    <p class="review-note">${escapeHtml(draft.source_file)} · ${draft.row_count ?? 0} 行。说明里的数字不会变成表中记录。数字列不会自动当成度量，值索引默认关闭。</p>
    <form id="tableConfirm" class="table-review">
      <label>文件简介 <input name="summary" required /></label>
      <label>表简介 <input name="table_summary" required /></label>
      <label>表角色 <select name="table_role"><option>事实表</option><option>维度表</option></select></label>
      <div class="field-scroll">
        <table class="field-table">
          <thead><tr><th>原列名</th><th>说明</th><th>角色</th><th>类型</th><th>索引</th></tr></thead>
          <tbody>${fields}</tbody>
        </table>
      </div>
      <button class="primary-action review-submit" type="submit">确认发布</button>
    </form>`;
  document.querySelector("#tableConfirm").addEventListener("submit", submitTableConfirm);
}

async function loadPublished() {
  try {
    const response = await fetch("/api/table-imports", { cache: "no-store" });
    if (!response.ok) return;
    const data = await response.json();
    state.published = Array.isArray(data.tables) ? data.tables : [];
  } catch {
    state.published = state.published || [];
  }
}

async function submitTableConfirm(event) {
  event.preventDefault();
  const draft = state.tableReview;
  const form = event.currentTarget;
  const button = form.querySelector("button");
  button.disabled = true;
  button.textContent = "正在发布";
  const columns = (draft.columns || []).map((column, index) => ({
    column_id: column.column_id,
    description: form.querySelector(`[data-field="description"][data-index="${index}"]`).value,
    role: form.querySelector(`[data-field="role"][data-index="${index}"]`).value,
    data_type: form.querySelector(`[data-field="data_type"][data-index="${index}"]`).value,
    aliases: column.aliases || [],
    value_index: form.querySelector(`[data-field="value_index"][data-index="${index}"]`).checked,
  }));
  const payload = {
    summary: form.summary.value,
    table_summary: form.table_summary.value,
    table_role: form.table_role.value,
    columns,
    metrics: [],
  };
  elements.resultPanel.insertAdjacentHTML("afterbegin", `<p class="review-note" id="publishProgress">1/3 已提交确认，正在写入数据库和索引。</p>`);
  const response = await fetch(`/api/table-imports/${draft.import_version_id}/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) {
    button.disabled = false;
    button.textContent = "确认发布";
    toast(result.error || "发布失败", true);
    return;
  }
  state.tableReview = { published: true };
  await loadPublished();
  renderHistory();
  toast(`已发布 ${result.source_file}`);
  elements.resultPanel.innerHTML = `
    <h3>发布完成</h3>
    <ol class="publish-steps">
      <li>已接收确认</li>
      <li>已写入 PostgreSQL</li>
      <li>索引已完成，当前版本可以查询</li>
    </ol>
    <p>已发布 ${escapeHtml(result.source_file)}，表 ${escapeHtml(result.table_name)}，${result.row_count} 行。下方任务列表已显示这条表。</p>`;
}

async function initialize() {
  try {
    const response = await fetch("/api/config", { cache: "no-store" });
    if (!response.ok) throw new Error("config unavailable");
    state.config = await response.json();
    elements.limitHint.textContent = `单个文件最大 ${state.config.max_upload_mb} MB`;
    elements.systemState.classList.add("ready");
    elements.systemState.querySelector("span:last-child").textContent = "入库服务已连接";
  } catch {
    elements.systemState.querySelector("span:last-child").textContent = "入库服务未连接";
    state.config.stages = [
      "validate", "minio", "parse", "images", "split", "recognize", "embedding", "milvus",
    ].map((key) => ({ key, label: key }));
  }
  renderQueue();
  renderPipeline(null);
  await pollTasks();
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) pollTasks();
});

elements.dropZone.addEventListener("click", () => elements.fileInput.click());
elements.dropZone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    elements.fileInput.click();
  }
});
elements.fileInput.addEventListener("change", (event) => {
  addFiles(event.target.files);
  event.target.value = "";
});
for (const eventName of ["dragenter", "dragover"]) {
  elements.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    elements.dropZone.classList.add("dragging");
  });
}
for (const eventName of ["dragleave", "drop"]) {
  elements.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    elements.dropZone.classList.remove("dragging");
  });
}
elements.dropZone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
elements.fileQueue.addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove-index]");
  if (!button) return;
  state.files.splice(Number(button.dataset.removeIndex), 1);
  renderQueue();
});
elements.historyList.addEventListener("click", (event) => {
  const row = event.target.closest("[data-task-id]");
  if (!row) return;
  state.selectedTaskId = row.dataset.taskId;
  renderTasks();
});
elements.startButton.addEventListener("click", startUpload);
document.querySelector("#codeForm").addEventListener("submit", submitCode);
loadCodeCatalog().catch((error) => toast(error.message, true));

async function loadCodeCatalog() {
  const [versions, jobs] = await Promise.all([
    fetch("/api/code-imports", { cache: "no-store" }).then(readJson),
    fetch("/api/data-jobs", { cache: "no-store" }).then(readJson),
  ]);
  const target = document.querySelector("#codeTarget");
  target.replaceChildren();
  const fresh = document.createElement("option");
  fresh.value = "";
  fresh.textContent = "作为新代码";
  target.append(fresh);
  const seen = new Set();
  for (const item of versions.versions || []) {
    if (!item.code_id || seen.has(item.code_id)) continue;
    seen.add(item.code_id);
    const option = document.createElement("option");
    option.value = item.code_id;
    option.textContent = item.name || item.code_id;
    target.append(option);
  }
  const doc = document.querySelector("#codeDoc");
  doc.replaceChildren();
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "不绑定文件";
  doc.append(none);
  for (const item of jobs.documents || []) {
    const option = document.createElement("option");
    option.value = item.doc_id;
    option.textContent = `${item.source_file} · ${item.status}`;
    doc.append(option);
  }
  const list = document.querySelector("#codeVersions");
  list.replaceChildren();
  for (const item of versions.versions || []) {
    const row = document.createElement("p");
    row.textContent = `${item.name} · ${item.status} · ${item.version_id}`;
    list.append(row);
  }
}

async function readJson(response) {
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "读取失败");
  return data;
}

async function submitCode(event) {
  event.preventDefault();
  const file = document.querySelector("#codeFile").files[0];
  if (!file) return;
  const target = document.querySelector("#codeTarget").value;
  const params = new URLSearchParams({ filename: file.name });
  if (target) params.set("code_id", target);
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  try {
    const response = await fetch(`/api/code-imports?${params}`, { method: "POST", body: await file.arrayBuffer() });
    const staged = await readJson(response);
    await readJson(await fetch(`/api/code-imports/${staged.version_id}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: document.querySelector("#codeName").value.trim(),
        description: document.querySelector("#codeDescription").value.trim(),
        doc_id: document.querySelector("#codeDoc").value,
      }),
    }));
    event.target.reset();
    await loadCodeCatalog();
    toast("代码已保存，旧版本仍保留");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

initialize();
