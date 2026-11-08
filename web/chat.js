const state = {
  conversations: [],
  activeId: null,
  messages: [],
  pendingConfirmation: null,
  sending: false,
  sendingId: null,
  loadToken: 0,
  skillId: "",
  editingSkillId: "",
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
    const item = document.createElement("div");
    item.className = `conversation-item${conversation.conversation_id === state.activeId ? " active" : ""}`;
    const open = document.createElement("button");
    open.type = "button";
    open.className = "conversation-open";
    open.title = conversation.title;
    open.setAttribute("aria-current", conversation.conversation_id === state.activeId ? "page" : "false");
    open.textContent = conversation.title;
    open.addEventListener("click", () => selectConversation(conversation.conversation_id));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "conversation-delete";
    remove.textContent = "删除";
    remove.setAttribute("aria-label", `删除${conversation.title}`);
    remove.addEventListener("click", () => removeConversation(conversation.conversation_id));
    item.append(open, remove);
    ui.list.append(item);
  }
}

async function removeConversation(id) {
  try {
    await api(`/api/conversations/${id}`, { method: "DELETE" });
    state.conversations = state.conversations.filter((item) => item.conversation_id !== id);
    if (state.activeId !== id) {
      renderConversations();
      return;
    }
    state.activeId = null;
    state.messages = [];
    state.pendingConfirmation = null;
    showDesk("");
    const next = state.conversations[0];
    if (next) {
      await selectConversation(next.conversation_id);
      return;
    }
    updateUrl("", true);
    ui.title.textContent = "新对话";
    renderConversations();
    renderMessages();
  } catch (error) {
    showError(error.message);
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
    else renderAnswer(content, message.content, message.role);
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

function renderAnswer(parent, value, role) {
  const normalized = role === "assistant" ? structureAnswer(value) : String(value || "").replace(/\r\n/g, "\n").trim();
  if (!normalized) return;
  for (const block of normalized.split(/\n{2,}/)) {
    const lines = block.split("\n").map((line) => line.trim()).filter(Boolean);
    let list = null;
    lines.forEach((line, index) => {
      if (role === "assistant" && (line.startsWith("§") || (index === 0 && lines.length > 1 && isAnswerHeading(line)) || (lines.length === 1 && isAnswerHeading(line)))) {
        list = null;
        const heading = document.createElement("h3");
        appendRichText(heading, line.replace(/^§/, ""));
        parent.append(heading);
        return;
      }
      if (line.startsWith("- ")) {
        if (!list) {
          list = document.createElement("ul");
          parent.append(list);
        }
        const item = document.createElement("li");
        appendRichText(item, line.slice(2));
        list.append(item);
        return;
      }
      list = null;
      for (const sentence of role === "assistant" ? answerSentences(line) : [line]) {
        const paragraph = document.createElement("p");
        appendRichText(paragraph, sentence);
        parent.append(paragraph);
      }
    });
  }
}

function answerSentences(text) {
  const parts = String(text || "").match(/[^。！？]+[。！？]|[^。！？]+$/g) || [];
  const merged = [];
  for (const raw of parts) {
    const part = raw.trim();
    if (!part) continue;
    const cited = part.match(/^((?:\[\d+\])+)([\s\S]*)$/);
    if (cited && merged.length) {
      merged[merged.length - 1] += cited[1];
      const rest = cited[2].trim();
      if (rest) merged.push(rest);
      continue;
    }
    if (merged.length && part.length < 8 && !"。！？".includes(part.slice(-1))) {
      merged[merged.length - 1] += part;
      continue;
    }
    merged.push(part);
  }
  return merged.length ? merged : [text];
}

function structureAnswer(value) {
  return String(value || "")
    .replace(/\r\n/g, "\n")
    .replace(/\*\*([^*\n]+)\*\*/g, "\n\n§$1\n")
    .replace(/^[ \t]*#{1,3}[ \t]+/gm, "§")
    .replace(/([。！？])\s*-\s+/g, "$1\n- ")
    .replace(/^先给结论[：:]\s*/, "§结论\n")
    .replace(/(以下按[^。\n]{0,24}。)/g, "\n\n$1\n\n")
    .replace(/(?<!^)(?<!\n)([一二三四五六七八九十]{1,3})、/g, "\n\n$1、")
    .replace(/^([一二三四五六七八九十]{1,3})、([^：:\n]{1,20})[：:]\s*/gm, "§$2\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function isAnswerHeading(line) {
  const text = line.replace(/^§/, "");
  return text.length > 0 && text.length <= 20 && !/[。！？]$/.test(text) && !text.startsWith("- ");
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
      body: JSON.stringify({ content: question, web_search: webSearchEnabled(), skill_id: state.skillId, ...extra }),
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

function showDesk(which) {
  const desks = {
    jobs: ["定时任务", "小灵 / 定时任务"],
    skills: ["技能", "小灵 / 技能"],
    memory: ["个性化", "小灵 / 个性化"],
    database: ["数据库", "小灵 / 数据库"],
  };
  for (const name of Object.keys(desks)) {
    document.querySelector(`#${name}View`).hidden = name !== which;
  }
  const open = Boolean(desks[which]);
  ui.area.hidden = open;
  document.querySelector(".composer-wrap").hidden = open;
  document.querySelector("#openJobs").classList.toggle("is-active", which === "jobs");
  document.querySelector("#openSkills").classList.toggle("is-active", which === "skills");
  document.querySelector("#newChat").classList.toggle("is-active", !open);
  document.querySelector("#userMenu").hidden = true;
  document.querySelector("#openUserMenu").setAttribute("aria-expanded", "false");
  if (open) {
    ui.title.textContent = desks[which][0];
    document.querySelector(".current-chat-label").textContent = desks[which][1];
    if (which === "jobs") loadJobs().catch((error) => showError(error.message));
    if (which === "skills") renderSkills();
    if (which === "memory") loadMemory().catch((error) => showError(error.message));
    if (which === "database") loadDatabase().catch((error) => showError(error.message));
  } else {
    document.querySelector(".current-chat-label").textContent = "小灵 / 金融知识";
    ui.title.textContent = state.conversations.find((item) => item.conversation_id === state.activeId)?.title || "新对话";
  }
}

function showJobs(open) {
  showDesk(open ? "jobs" : "");
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
        skill_id: document.querySelector("#jobSkill").value,
        run_at: document.querySelector("#jobRunAt").value.trim(),
        schedule_kind: document.querySelector("#jobCadence").value,
        schedule_day: document.querySelector("#jobCadence").value === "daily" ? null : Number(document.querySelector("#jobDay").value),
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
    const text = document.createElement("span");
    text.textContent = `${doc.source_file} · ${doc.status}`;
    label.append(input, text);
    docs.append(label);
  }
  fillDayOptions();
  const code = document.querySelector("#jobCode");
  code.replaceChildren();
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = "不指定，不会运行";
  code.append(empty);
  for (const item of jobCatalog.codes) {
    const option = document.createElement("option");
    option.value = item.code_id;
    option.textContent = item.name || item.code_id;
    code.append(option);
  }
  const skill = document.querySelector("#jobSkill");
  skill.replaceChildren();
  const noSkill = document.createElement("option");
  noSkill.value = "";
  noSkill.textContent = "不使用技能";
  skill.append(noSkill);
  for (const item of jobCatalog.skills || []) {
    const option = document.createElement("option");
    option.value = item.skill_id;
    option.textContent = item.name;
    skill.append(option);
  }
  if (state.skillId) skill.value = state.skillId;
}

function fillDayOptions() {
  const cadence = document.querySelector("#jobCadence").value;
  const label = document.querySelector("#jobDayLabel");
  const day = document.querySelector("#jobDay");
  label.hidden = cadence === "daily";
  day.replaceChildren();
  if (cadence === "weekly") {
    ["周一", "周二", "周三", "周四", "周五", "周六", "周日"].forEach((name, index) => {
      const option = document.createElement("option");
      option.value = String(index + 1);
      option.textContent = name;
      day.append(option);
    });
  }
  if (cadence === "monthly") {
    for (let number = 1; number <= 31; number += 1) {
      const option = document.createElement("option");
      option.value = String(number);
      option.textContent = `${number}日`;
      day.append(option);
    }
  }
}

function scheduleText(job) {
  if (job.schedule_kind === "weekly") {
    const names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
    return `每${names[(job.schedule_day || 1) - 1]} ${job.run_at}`;
  }
  if (job.schedule_kind === "monthly") return `每月${job.schedule_day}日 ${job.run_at}`;
  return `每天 ${job.run_at}`;
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
    meta.textContent = `${job.sop_ref} · ${scheduleText(job)} · 代码 ${job.code_id || "未指定"}`;
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
  const heading = document.createElement("div");
  heading.className = "detail-title";
  const title = document.createElement("h2");
  title.textContent = job.name;
  const pill = document.createElement("span");
  pill.className = "status-pill";
  pill.textContent = jobStatus[job.last_status] || job.last_status;
  heading.append(title, pill);
  const grid = document.createElement("div");
  grid.className = "detail-grid";
  const description = document.createElement("label");
  description.append(document.createTextNode("任务描述"));
  const descriptionInput = document.createElement("textarea");
  descriptionInput.maxLength = 200;
  descriptionInput.value = job.description || "";
  description.append(descriptionInput);
  const cadence = document.createElement("label");
  cadence.append(document.createTextNode("重复"));
  const cadenceInput = document.createElement("select");
  [["daily", "每天"], ["weekly", "每周"], ["monthly", "每月"]].forEach(([value, text]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = text;
    option.selected = (job.schedule_kind || "daily") === value;
    cadenceInput.append(option);
  });
  cadence.append(cadenceInput);
  const day = document.createElement("label");
  day.append(document.createTextNode("日期"));
  const dayInput = document.createElement("select");
  day.append(dayInput);
  const fillDetailDay = () => {
    day.hidden = cadenceInput.value === "daily";
    dayInput.replaceChildren();
    const selected = String(job.schedule_day || 1);
    if (cadenceInput.value === "weekly") {
      ["周一", "周二", "周三", "周四", "周五", "周六", "周日"].forEach((name, index) => {
        const option = document.createElement("option");
        option.value = String(index + 1);
        option.textContent = name;
        option.selected = option.value === selected;
        dayInput.append(option);
      });
    }
    if (cadenceInput.value === "monthly") {
      for (let number = 1; number <= 31; number += 1) {
        const option = document.createElement("option");
        option.value = String(number);
        option.textContent = `${number}日`;
        option.selected = option.value === selected;
        dayInput.append(option);
      }
    }
  };
  cadenceInput.addEventListener("change", fillDetailDay);
  fillDetailDay();
  const time = document.createElement("label");
  time.append(document.createTextNode("运行时刻"));
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
      body: JSON.stringify({
        description: descriptionInput.value.trim(),
        run_at: timeInput.value,
        schedule_kind: cadenceInput.value,
        schedule_day: cadenceInput.value === "daily" ? null : Number(dayInput.value),
      }),
    });
    await loadJobs();
  });
  grid.append(description, cadence, day, time);
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
  const tryRun = document.createElement("button");
  tryRun.type = "button";
  tryRun.className = "desk-solid";
  tryRun.textContent = "试运行";
  tryRun.addEventListener("click", async () => {
    tryRun.disabled = true;
    tryRun.textContent = "正在试运行";
    try {
      await api(`/api/data-jobs/${job.job_id}/run`, { method: "POST" });
      await renderJobDetail(jobId);
    } catch (error) {
      showError(error.message);
      tryRun.disabled = false;
      tryRun.textContent = "试运行";
    }
  });
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
  detail.append(back, heading, grid, save, files, codeWrap, tryRun, preview);
  if (job.last_error) {
    const error = document.createElement("p");
    error.className = "job-error";
    error.textContent = job.last_error;
    detail.append(error);
  }
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

document.querySelector("#brandHome").addEventListener("click", () => showDesk(""));
document.querySelector("#openUserMenu").addEventListener("click", () => {
  const menu = document.querySelector("#userMenu");
  menu.hidden = !menu.hidden;
  document.querySelector("#openUserMenu").setAttribute("aria-expanded", String(!menu.hidden));
});
document.querySelectorAll("#userMenu [data-view]").forEach((button) => {
  button.addEventListener("click", () => {
    showDesk(button.dataset.view);
    setSidebar(false);
  });
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

document.querySelector("#jobCadence").addEventListener("change", fillDayOptions);
document.querySelector("#openSkills").addEventListener("click", () => {
  const opening = document.querySelector("#skillsView").hidden;
  showDesk(opening ? "skills" : "");
  if (opening) setSidebar(false);
});
document.querySelector("#skillChip").addEventListener("click", () => {
  state.skillId = "";
  document.querySelector("#skillChip").hidden = true;
});
document.querySelector("#openSkillDialog").addEventListener("click", () => openSkillDialog());
document.querySelector("#cancelSkill").addEventListener("click", () => document.querySelector("#skillDialog").close());
document.querySelector("#skillForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = {
    name: document.querySelector("#skillName").value.trim(),
    summary: document.querySelector("#skillSummary").value.trim(),
    instructions: document.querySelector("#skillInstructions").value.trim(),
    code_id: document.querySelector("#skillCode").value,
  };
  try {
    const path = state.editingSkillId ? `/api/skills/${state.editingSkillId}` : "/api/skills";
    await api(path, { method: state.editingSkillId ? "PATCH" : "POST", body: JSON.stringify(body) });
    document.querySelector("#skillDialog").close();
    state.editingSkillId = "";
    renderSkills();
  } catch (error) {
    showError(error.message);
  }
});

function useSkill(skill) {
  state.skillId = skill.skill_id;
  const chip = document.querySelector("#skillChip");
  chip.hidden = false;
  chip.textContent = `技能：${skill.name} · 取消`;
  showDesk("");
}

async function openSkillDialog(skill) {
  state.editingSkillId = skill?.skill_id || "";
  document.querySelector("#skillDialogTitle").textContent = skill ? "修改技能" : "新建技能";
  document.querySelector("#skillName").value = skill?.name || "";
  document.querySelector("#skillSummary").value = skill?.summary || "";
  document.querySelector("#skillInstructions").value = skill?.instructions || "";
  const select = document.querySelector("#skillCode");
  select.replaceChildren();
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "不绑定代码";
  select.append(none);
  try {
    const data = await api("/api/data-jobs");
    for (const item of data.codes || []) {
      const option = document.createElement("option");
      option.value = item.code_id;
      option.textContent = item.name || item.code_id;
      option.selected = item.code_id === skill?.code_id;
      select.append(option);
    }
  } catch (error) {
    showError(error.message);
  }
  document.querySelector("#skillDialog").showModal();
}

function renderSkills() {
  api("/api/skills").then((data) => {
    const list = document.querySelector("#skillList");
    list.replaceChildren();
    const skills = data.skills || [];
    if (!skills.length) {
      const empty = document.createElement("p");
      empty.className = "empty-note";
      empty.textContent = "还没有自己的技能。新建一条，写下名称和做法。";
      list.append(empty);
      return;
    }
    for (const skill of skills) {
      const card = document.createElement("article");
      card.className = "plain-card static";
      const name = document.createElement("strong");
      name.textContent = skill.name;
      const summary = document.createElement("small");
      summary.textContent = skill.summary || skill.instructions;
      const actions = document.createElement("div");
      actions.className = "card-actions";
      const use = document.createElement("button");
      use.type = "button";
      use.textContent = "用于这次对话";
      use.addEventListener("click", () => useSkill(skill));
      const edit = document.createElement("button");
      edit.type = "button";
      edit.textContent = "修改";
      edit.addEventListener("click", () => openSkillDialog(skill));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "删除";
      remove.addEventListener("click", async () => {
        await api(`/api/skills/${skill.skill_id}`, { method: "DELETE" });
        if (state.skillId === skill.skill_id) {
          state.skillId = "";
          document.querySelector("#skillChip").hidden = true;
        }
        renderSkills();
      });
      actions.append(use, edit, remove);
      card.append(name, summary, actions);
      list.append(card);
    }
  }).catch((error) => showError(error.message));
}

async function loadDatabase() {
  const data = await api("/api/table-imports");
  const list = document.querySelector("#databaseList");
  list.replaceChildren();
  const tables = (data.tables || []).filter((item) => item.table_name);
  if (!tables.length) {
    const empty = document.createElement("p");
    empty.className = "empty-note";
    empty.textContent = "还没有已发布的表。";
    list.append(empty);
    return;
  }
  for (const table of tables) {
    const card = document.createElement("article");
    card.className = "plain-card static";
    const name = document.createElement("strong");
    name.textContent = table.source_file;
    const summary = document.createElement("small");
    summary.textContent = table.table_summary || "已发布";
    card.append(name, summary);
    list.append(card);
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
