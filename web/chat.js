const state = {
  conversations: [],
  activeId: null,
  messages: [],
  pendingConfirmation: null,
  sending: false,
  sendingId: null,
  loadToken: 0,
};

const ui = {
  list: document.querySelector("#conversationList"),
  count: document.querySelector("#conversationCount"),
  title: document.querySelector("#currentTitle"),
  welcome: document.querySelector("#welcome"),
  messages: document.querySelector("#messageList"),
  area: document.querySelector("#messagesArea"),
  form: document.querySelector("#messageForm"),
  input: document.querySelector("#messageInput"),
  send: document.querySelector("#sendButton"),
  error: document.querySelector("#inlineError"),
  sidebar: document.querySelector("#sidebar"),
  scrim: document.querySelector("#mobileScrim"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers },
  });
  const result = await response.json().catch(() => ({ error: `请求失败 (${response.status})` }));
  if (!response.ok) throw new Error(result.error || `请求失败 (${response.status})`);
  return result;
}

function showError(message) {
  ui.error.textContent = message;
  ui.error.hidden = !message;
}

function setSidebar(open) {
  ui.sidebar.classList.toggle("open", open);
  ui.scrim.hidden = !open;
}

function updateUrl(id, replace = false) {
  const url = id ? `/chat?id=${encodeURIComponent(id)}` : "/chat";
  history[replace ? "replaceState" : "pushState"]({}, "", url);
}

function renderConversations() {
  ui.list.replaceChildren();
  ui.count.textContent = String(state.conversations.length);
  if (!state.conversations.length) {
    const empty = document.createElement("p");
    empty.className = "sidebar-empty";
    empty.textContent = "还没有对话记录";
    ui.list.append(empty);
    return;
  }
  for (const conversation of state.conversations) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `conversation-item${conversation.conversation_id === state.activeId ? " active" : ""}`;
    item.setAttribute("aria-current", conversation.conversation_id === state.activeId ? "page" : "false");
    item.title = conversation.title;
    const label = document.createElement("span");
    label.textContent = conversation.title;
    item.append(label);
    item.addEventListener("click", () => selectConversation(conversation.conversation_id));
    ui.list.append(item);
  }
}

function renderMessages() {
  const hasMessages = state.messages.length > 0;
  ui.welcome.hidden = hasMessages;
  ui.messages.hidden = !hasMessages;
  ui.messages.replaceChildren();
  for (const message of state.messages) {
    const row = document.createElement("article");
    row.className = `message ${message.role}${message.pending ? " pending" : ""}`;
    if (message.role === "assistant") {
      const avatar = document.createElement("span");
      avatar.className = "message-avatar";
      avatar.setAttribute("aria-hidden", "true");
      avatar.textContent = "灵";
      row.append(avatar);
    }
    const body = document.createElement("div");
    body.className = "message-body";
    if (message.role === "assistant") {
      const label = document.createElement("p");
      label.className = "message-label";
      label.textContent = "小灵";
      body.append(label);
    }
    const content = document.createElement("div");
    content.className = "message-content";
    if (message.pending) renderProgress(content, message);
    else renderAnswer(content, message.content);
    body.append(content);
    renderCitations(body, message.citations);
    row.append(body);
    ui.messages.append(row);
  }
  renderConfirmation();
  ui.area.scrollTop = ui.area.scrollHeight;
}

function renderProgress(parent, message) {
  const steps = message.steps?.length ? message.steps : [message.statusText || "正在处理…"];
  const list = document.createElement("ol");
  list.className = "progress-list";
  steps.forEach((step, index) => {
    const item = document.createElement("li");
    item.className = index === steps.length - 1 ? "current" : "done";
    item.textContent = step;
    list.append(item);
  });
  parent.append(list);
}

function renderAnswer(parent, value) {
  const normalized = String(value || "")
    .replace(/\r\n/g, "\n")
    .replace(/\*\*([^*\n]+)\*\*/g, "\n\n§$1\n")
    .replace(/^[ \t]*#{1,3}[ \t]+/gm, "§")
    .replace(/([。！？])\s*-\s+/g, "$1\n- ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  if (!normalized) return;
  for (const block of normalized.split(/\n{2,}/)) {
    const lines = block.split("\n").map((line) => line.trim()).filter(Boolean);
    if (!lines.length) continue;
    if (lines.length === 1 && lines[0].startsWith("§")) {
      const heading = document.createElement("h3");
      appendRichText(heading, lines[0].slice(1));
      parent.append(heading);
      continue;
    }
    if (lines.every((line) => line.startsWith("- ") || line.startsWith("§"))) {
      let list = null;
      for (const line of lines) {
        if (line.startsWith("§")) {
          const heading = document.createElement("h3");
          appendRichText(heading, line.slice(1));
          parent.append(heading);
          list = null;
          continue;
        }
        if (!list) {
          list = document.createElement("ul");
          parent.append(list);
        }
        const item = document.createElement("li");
        appendRichText(item, line.slice(2));
        list.append(item);
      }
      continue;
    }
    const paragraph = document.createElement("p");
    appendRichText(paragraph, lines.join(""));
    parent.append(paragraph);
  }
}

function appendRichText(parent, value) {
  const pieces = String(value || "").split(/(\[\d+\]|https?:\/\/[^\s<>"']+)/g);
  for (const part of pieces) {
    const mark = part.match(/^\[(\d+)\]$/);
    if (mark) {
      const link = document.createElement("a");
      link.className = "cite-mark";
      link.href = `#cite-${mark[1]}`;
      link.textContent = part;
      parent.append(link);
    } else if (/^https?:\/\//.test(part)) {
      const link = document.createElement("a");
      link.href = part;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = part;
      parent.append(link);
    } else if (part) {
      parent.append(document.createTextNode(part));
    }
  }
}

function renderCitations(parent, citations) {
  if (!citations?.length) return;
  const list = document.createElement("ol");
  list.className = "citation-list";
  for (const item of citations) {
    const card = document.createElement("li");
    card.className = "citation-card";
    const index = item.index || citations.indexOf(item) + 1;
    card.id = `cite-${index}`;
    const title = document.createElement("strong");
    const rawName = item.download_name || item.title || item.source_file || item.document_title || item.file_name || "资料";
    const name = String(rawName).replace(/_new(?=\.[^.]+$)/, "").replace(/\.md$/i, "");
    title.textContent = `[${index}] ${name}`;
    card.append(title);
    if (item.product_name) {
      const product = document.createElement("p");
      product.textContent = item.product_name;
      card.append(product);
    }
    if (item.excerpt || item.source_span || item.content) {
      const excerpt = document.createElement("p");
      excerpt.textContent = item.excerpt || item.source_span || item.content;
      card.append(excerpt);
    }
    if (item.download_url) {
      const download = document.createElement("a");
      download.href = item.download_url;
      const saved = String(item.download_name || "").toLowerCase();
      download.textContent = saved.endsWith(".doc") || saved.endsWith(".docx") ? "下载 Word" : "下载 PDF";
      card.append(download);
    }
    if (item.url) {
      const page = document.createElement("a");
      page.href = item.url;
      page.target = "_blank";
      page.rel = "noopener noreferrer";
      page.textContent = item.url;
      card.append(document.createTextNode(" "));
      card.append(page);
    }
    list.append(card);
  }
  parent.append(list);
}

function renderConfirmation() {
  const confirmation = state.pendingConfirmation;
  if (!confirmation || !confirmation.candidates?.length) return;
  const panel = document.createElement("div");
  panel.className = "confirmation-panel";
  const heading = document.createElement("p");
  const business = confirmation.candidates.some((candidate) => candidate.entity_type);
  heading.textContent = business ? "请确认查询对象" : "请确认产品名称";
  panel.append(heading);
  if (confirmation.candidates.length === 1) {
    const name = confirmation.candidates[0].itemname_question;
    const actions = document.createElement("div");
    actions.className = "confirmation-actions";
    const yes = document.createElement("button");
    yes.type = "button";
    yes.textContent = `是，${confirmation.candidates[0].display_name || name}`;
    yes.disabled = state.sending;
    yes.addEventListener("click", () => submitMessage(`确认产品：${confirmation.candidates[0].display_name || name}`, { item_name_user_confirmed: true }));
    const no = document.createElement("button");
    no.type = "button";
    no.textContent = "不是这个产品";
    no.disabled = state.sending;
    no.addEventListener("click", () => submitMessage("不是这个产品", { item_name_user_confirmed: false }));
    actions.append(yes, no);
    panel.append(actions);
  } else {
    const options = document.createElement("div");
    options.className = "confirmation-options";
    for (const candidate of confirmation.candidates) {
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = candidate.itemname_question;
      checkbox.disabled = state.sending;
      label.append(checkbox, document.createTextNode(candidate.display_name || candidate.itemname_question));
      options.append(label);
    }
    panel.append(options);
    const actions = document.createElement("div");
    actions.className = "confirmation-actions";
    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.textContent = "确认所选产品";
    confirm.disabled = state.sending;
    confirm.addEventListener("click", () => {
      const selected = [...options.querySelectorAll("input:checked")].map((item) => item.value);
      if (!selected.length) return showError("请至少选择一个产品，或点击“都不是”");
      const labels = confirmation.candidates
        .filter((candidate) => selected.includes(candidate.itemname_question))
        .map((candidate) => candidate.display_name || candidate.itemname_question);
      submitMessage(`选择产品：${labels.join("、")}`, { item_name_user_selection: selected });
    });
    const none = document.createElement("button");
    none.type = "button";
    none.textContent = "都不是";
    none.disabled = state.sending;
    none.addEventListener("click", () => submitMessage("都不是这些产品", { item_name_user_selection: [] }));
    actions.append(confirm, none);
    panel.append(actions);
  }
  ui.messages.append(panel);
}

function updateComposer() {
  ui.send.disabled = !ui.input.value.trim() || state.sending;
  ui.input.disabled = state.sending;
  ui.input.style.height = "auto";
  ui.input.style.height = `${Math.min(ui.input.scrollHeight, 180)}px`;
}

async function refreshConversations() {
  const data = await api("/api/conversations");
  state.conversations = data.conversations;
  renderConversations();
}

async function loadConversation(id) {
  const token = ++state.loadToken;
  try {
    const data = await api(`/api/conversations/${id}`);
    if (token !== state.loadToken || state.activeId !== id) return;
    state.messages = data.messages;
    state.pendingConfirmation = data.pending_confirmation || null;
    ui.title.textContent = data.title;
    renderMessages();
    return true;
  } catch (error) {
    if (token === state.loadToken && state.activeId === id) showError(error.message);
    return false;
  }
}

function selectConversation(id, { replace = false, updateHistory = true } = {}) {
  state.activeId = id;
  state.messages = [];
  state.pendingConfirmation = null;
  ui.title.textContent = state.conversations.find((item) => item.conversation_id === id)?.title || "新对话";
  showError("");
  renderConversations();
  renderMessages();
  setSidebar(false);
  if (updateHistory) updateUrl(id, replace);
  if (id) loadConversation(id);
  ui.input.focus();
}

async function createConversation() {
  const conversation = await api("/api/conversations", { method: "POST" });
  state.conversations.unshift(conversation);
  selectConversation(conversation.conversation_id);
  return conversation.conversation_id;
}

function webSearchEnabled() {
  return document.querySelector("#webSearch")?.checked !== false;
}

async function readEvents(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() || "";
    for (const block of blocks) {
      if (!block.trim()) continue;
      let event = "message";
      const dataLines = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length) onEvent(event, JSON.parse(dataLines.join("\n")));
    }
  }
}

async function submitMessage(question, extra = {}) {
  if (!question || state.sending) return;
  showError("");
  state.sending = true;
  updateComposer();
  let id = state.activeId;
  try {
    if (!id) id = await createConversation();
    state.sendingId = id;
    state.loadToken += 1;
    ui.input.value = "";
    updateComposer();
    state.messages.push({ role: "user", content: question });
    state.messages.push({
      role: "assistant",
      content: "",
      statusText: "已收到问题，正在判断需要哪类资料。",
      steps: ["已收到问题，正在判断需要哪类资料。"],
      pending: true,
      citations: [],
    });
    state.pendingConfirmation = null;
    renderMessages();
    const response = await fetch(`/api/conversations/${id}/messages/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ content: question, web_search: webSearchEnabled(), ...extra }),
    });
    if (!response.ok) {
      const result = await response.json().catch(() => ({ error: `请求失败 (${response.status})` }));
      throw new Error(result.error || `请求失败 (${response.status})`);
    }
    const draft = state.messages[state.messages.length - 1];
    await readEvents(response, (event, data) => {
      if (state.activeId !== id) return;
      if (event === "status") {
        draft.steps = draft.steps || [];
        if (data.text && draft.steps.at(-1) !== data.text) draft.steps.push(data.text);
        draft.statusText = data.text || "正在检索资料…";
        draft.pending = !draft.content;
      } else if (event === "answer_delta") {
        draft.pending = false;
        draft.content += data.text || "";
      } else if (event === "citations") {
        draft.citations = data.citations || [];
      } else if (event === "error") {
        throw new Error(data.message || "回答失败");
      }
      renderMessages();
    });
    if (state.activeId === id) await loadConversation(id);
    await refreshConversations();
  } catch (error) {
    showError(error.message);
    if (id && state.activeId === id) await loadConversation(id);
  } finally {
    state.sending = false;
    state.sendingId = null;
    updateComposer();
    if (state.activeId === id) renderMessages();
    ui.input.focus();
  }
}

async function sendMessage(event) {
  event.preventDefault();
  await submitMessage(ui.input.value.trim());
}

ui.form.addEventListener("submit", sendMessage);
ui.input.addEventListener("input", updateComposer);
ui.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    ui.form.requestSubmit();
  }
});
document.querySelector("#newChat").addEventListener("click", async () => {
  try {
    showError("");
    showJobs(false);
    await createConversation();
  } catch (error) {
    showError(error.message);
  }
});
document.querySelector("#collapseSidebar").addEventListener("click", () => {
  const shell = document.querySelector(".chat-shell");
  const collapsed = shell.classList.toggle("sidebar-collapsed");
  const button = document.querySelector("#collapseSidebar");
  button.setAttribute("aria-expanded", String(!collapsed));
  button.setAttribute("aria-label", collapsed ? "展开对话列表" : "收起对话列表");
});
document.querySelector("#openSidebar").addEventListener("click", () => setSidebar(true));
document.querySelector("#closeSidebar").addEventListener("click", () => setSidebar(false));
ui.scrim.addEventListener("click", () => setSidebar(false));
document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    ui.input.value = button.dataset.prompt;
    updateComposer();
    ui.input.focus();
  });
});
window.addEventListener("popstate", () => {
  const id = new URLSearchParams(location.search).get("id");
  selectConversation(id, { updateHistory: false });
});

const jobStatus = { never: "尚未运行", running: "运行中", succeeded: "成功", failed: "失败" };
let jobCatalog = { jobs: [], codes: [], documents: [], openId: null, preview: "" };

function showJobs(open) {
  document.querySelector("#jobsView").hidden = !open;
  ui.area.hidden = open;
  document.querySelector(".composer-wrap").hidden = open;
  document.querySelector("#openJobs").classList.toggle("is-active", open);
  document.querySelector(".current-chat-label").textContent = open ? "小灵 / 每日任务" : "小灵 / 金融知识";
  if (open) {
    ui.title.textContent = "定时任务";
    loadJobs().catch((error) => showError(error.message));
  } else {
    ui.title.textContent = state.conversations.find((item) => item.conversation_id === state.activeId)?.title || "新对话";
  }
}

document.querySelector("#openJobs").addEventListener("click", () => {
  const opening = document.querySelector("#jobsView").hidden;
  showJobs(opening);
  if (opening) setSidebar(false);
});
document.querySelector("#openJobDialog").addEventListener("click", () => {
  fillJobDialog();
  document.querySelector("#jobDialog").showModal();
});
document.querySelector("#cancelJob").addEventListener("click", () => document.querySelector("#jobDialog").close());
document.querySelector("#jobForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const docIds = [...document.querySelectorAll("#jobDocs input:checked")].map((item) => item.value);
  try {
    await api("/api/data-jobs", {
      method: "POST",
      body: JSON.stringify({
        name: document.querySelector("#jobName").value.trim(),
        sop_ref: document.querySelector("#jobSop").value.trim(),
        description: document.querySelector("#jobDescription").value.trim(),
        code_id: document.querySelector("#jobCode").value,
        run_at: document.querySelector("#jobRunAt").value.trim(),
        doc_ids: docIds,
      }),
    });
    document.querySelector("#jobDialog").close();
    event.target.reset();
    await loadJobs();
  } catch (error) {
    showError(error.message);
  }
});

function fillJobDialog() {
  const docs = document.querySelector("#jobDocs");
  docs.replaceChildren();
  if (!jobCatalog.documents.length) {
    docs.textContent = "还没有已登记的文件。";
  }
  for (const doc of jobCatalog.documents) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = doc.doc_id;
    label.append(input, document.createTextNode(` ${doc.source_file} · ${doc.status}`));
    docs.append(label);
  }
  const code = document.querySelector("#jobCode");
  code.replaceChildren();
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = "不指定，不会运行";
  code.append(empty);
  for (const item of jobCatalog.codes) {
    const option = document.createElement("option");
    option.value = item.code_id;
    option.textContent = item.code_id;
    code.append(option);
  }
}

async function loadJobs() {
  jobCatalog = { ...jobCatalog, ...(await api("/api/data-jobs")) };
  const list = document.querySelector("#jobList");
  const detail = document.querySelector("#jobDetail");
  const opened = (jobCatalog.jobs || []).find((job) => job.job_id === jobCatalog.openId);
  list.hidden = Boolean(opened);
  detail.hidden = !opened;
  if (opened) {
    await renderJobDetail(opened.job_id);
    return;
  }
  list.replaceChildren();
  if (!(jobCatalog.jobs || []).length) {
    const empty = document.createElement("p");
    empty.className = "empty-note";
    empty.textContent = "还没有定时任务。";
    list.append(empty);
    return;
  }
  for (const job of jobCatalog.jobs) {
    const card = document.createElement("article");
    card.className = `job-card ${job.last_status}`;
    const title = document.createElement("h3");
    title.textContent = job.name;
    const pill = document.createElement("span");
    pill.className = "status-pill";
    pill.textContent = jobStatus[job.last_status] || job.last_status;
    title.append(pill);
    const meta = document.createElement("p");
    meta.className = "job-meta";
    meta.textContent = `${job.sop_ref} · 每天 ${job.run_at} · 代码 ${job.code_id || "未指定"}`;
    const actions = document.createElement("div");
    actions.className = "chip-row";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "desk-ghost";
    open.textContent = "打开";
    open.addEventListener("click", () => {
      jobCatalog.openId = job.job_id;
      jobCatalog.preview = "";
      loadJobs().catch((error) => showError(error.message));
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "desk-ghost";
    remove.textContent = "取消";
    remove.addEventListener("click", async () => {
      await api(`/api/data-jobs/${job.job_id}`, { method: "DELETE" });
      await loadJobs();
    });
    actions.append(open, remove);
    card.append(title, meta, actions);
    if (job.last_error) {
      const error = document.createElement("p");
      error.className = "job-error";
      error.textContent = job.last_error;
      card.append(error);
    }
    list.append(card);
  }
}

async function renderJobDetail(jobId) {
  const job = await api(`/api/data-jobs/${jobId}`);
  const detail = document.querySelector("#jobDetail");
  detail.replaceChildren();
  const back = document.createElement("button");
  back.type = "button";
  back.className = "desk-ghost";
  back.textContent = "返回全部任务";
  back.addEventListener("click", () => {
    jobCatalog.openId = null;
    jobCatalog.preview = "";
    loadJobs().catch((error) => showError(error.message));
  });
  const title = document.createElement("h2");
  title.textContent = job.name;
  const pill = document.createElement("span");
  pill.className = "status-pill";
  pill.textContent = jobStatus[job.last_status] || job.last_status;
  title.append(pill);
  const grid = document.createElement("div");
  grid.className = "detail-grid";
  const description = document.createElement("label");
  description.append(document.createTextNode("任务描述"));
  const descriptionInput = document.createElement("textarea");
  descriptionInput.maxLength = 200;
  descriptionInput.value = job.description || "";
  description.append(descriptionInput);
  const time = document.createElement("label");
  time.append(document.createTextNode("每天运行"));
  const timeInput = document.createElement("input");
  timeInput.type = "time";
  timeInput.value = job.run_at;
  time.append(timeInput);
  const save = document.createElement("button");
  save.type = "button";
  save.className = "desk-solid";
  save.textContent = "保存修改";
  save.addEventListener("click", async () => {
    await api(`/api/data-jobs/${job.job_id}`, {
      method: "PATCH",
      body: JSON.stringify({ description: descriptionInput.value.trim(), run_at: timeInput.value }),
    });
    await loadJobs();
  });
  grid.append(description, time);
  const files = document.createElement("div");
  const filesLabel = document.createElement("p");
  filesLabel.textContent = "涉及文件";
  const fileRow = document.createElement("div");
  fileRow.className = "chip-row";
  for (const file of job.files || []) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "desk-ghost";
    chip.textContent = file.source_file || file.error || file.doc_id;
    chip.addEventListener("click", () => {
      jobCatalog.preview = `file:${file.doc_id}`;
      renderJobDetail(jobId).catch((error) => showError(error.message));
    });
    fileRow.append(chip);
  }
  if (!fileRow.childElementCount) fileRow.textContent = "还没有选择文件。";
  files.append(filesLabel, fileRow);
  const codeWrap = document.createElement("div");
  const codeChip = document.createElement("button");
  codeChip.type = "button";
  codeChip.className = "desk-ghost";
  codeChip.textContent = job.code_id || "未指定代码";
  codeChip.addEventListener("click", () => {
    jobCatalog.preview = "code";
    renderJobDetail(jobId).catch((error) => showError(error.message));
  });
  codeWrap.append(codeChip);
  const preview = document.createElement("div");
  preview.className = "preview";
  if (jobCatalog.preview.startsWith("file:")) {
    const file = (job.files || []).find((item) => item.doc_id === jobCatalog.preview.slice(5));
    preview.append(renderFilePreview(file));
  } else if (jobCatalog.preview === "code") {
    const block = document.createElement("pre");
    block.className = "code-view";
    block.textContent = job.code_registered ? job.code_preview : "代码未登记，不会运行";
    preview.append(block);
  }
  detail.append(back, title, grid, save, files, codeWrap, preview);
}

function renderFilePreview(file) {
  const wrap = document.createElement("div");
  if (!file) return wrap;
  if (file.error) {
    wrap.textContent = file.error;
    return wrap;
  }
  const summary = document.createElement("p");
  summary.className = "job-meta";
  summary.textContent = `${file.source_file} · 表 ${file.table_name}`;
  wrap.append(summary);
  const rows = file.rows || [];
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.textContent = "这张表目前没有数据行。";
    wrap.append(empty);
    return wrap;
  }
  const table = document.createElement("table");
  const head = document.createElement("tr");
  for (const name of file.columns || Object.keys(rows[0])) {
    const cell = document.createElement("th");
    cell.textContent = name;
    head.append(cell);
  }
  table.append(head);
  for (const row of rows) {
    const line = document.createElement("tr");
    for (const name of file.columns || Object.keys(row)) {
      const cell = document.createElement("td");
      cell.textContent = row[name] ?? "";
      line.append(cell);
    }
    table.append(line);
  }
  wrap.append(table);
  return wrap;
}

document.querySelector("#openMemory").addEventListener("click", async () => {
  const panel = document.querySelector("#memoryPanel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) await loadMemory();
});
document.querySelector("#memoryForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.querySelector("#memoryInput");
  const text = input.value.trim();
  if (!text) return;
  try {
    await api("/api/memory", { method: "POST", body: JSON.stringify({ text }) });
    input.value = "";
    await loadMemory();
  } catch (error) {
    showError(error.message);
  }
});

async function loadMemory() {
  const data = await api("/api/memory");
  const list = document.querySelector("#memoryList");
  list.replaceChildren();
  for (const item of data.items || []) {
    const row = document.createElement("li");
    const text = document.createElement("span");
    text.textContent = item.text;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "删除";
    remove.addEventListener("click", async () => {
      await api("/api/memory", { method: "DELETE", body: JSON.stringify({ text: item.text }) });
      await loadMemory();
    });
    row.append(text, remove);
    list.append(row);
  }
}

(async () => {
  updateComposer();
  try {
    await refreshConversations();
    const id = new URLSearchParams(location.search).get("id");
    if (id) selectConversation(id, { updateHistory: false });
  } catch (error) {
    showError(error.message);
  }
})();
