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
    const content = document.createElement("p");
    content.className = "message-content";
    const parts = String(message.content || "").split(/(https?:\/\/[^\s<>"']+)/g);
    for (const part of parts) {
      if (/^https?:\/\//.test(part)) {
        const link = document.createElement("a");
        link.href = part;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = part;
        content.append(link);
      } else {
        content.append(document.createTextNode(part));
      }
    }
    body.append(content);
    row.append(body);
    ui.messages.append(row);
  }
  renderConfirmation();
  ui.area.scrollTop = ui.area.scrollHeight;
}

function renderConfirmation() {
  const confirmation = state.pendingConfirmation;
  if (!confirmation || !confirmation.candidates?.length) return;
  const panel = document.createElement("div");
  panel.className = "confirmation-panel";
  const heading = document.createElement("p");
  heading.textContent = "请确认产品名称";
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
    state.messages.push({ role: "assistant", content: "正在思考…", pending: true });
    state.pendingConfirmation = null;
    renderMessages();
    await api(`/api/conversations/${id}/messages`, {
      method: "POST",
      body: JSON.stringify({ content: question, ...extra }),
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
    await createConversation();
  } catch (error) {
    showError(error.message);
  }
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
