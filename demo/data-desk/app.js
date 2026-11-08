/* 灵库数据台演示。数据只存在本页内存中，不连接 PostgreSQL，也不代表正式查询实现。 */

const codes = {
  update_qinglan_flow: "更新青岚日申赎",
  sync_chengjiang_terms: "同步澄江条款",
  refresh_nanqiao_roster: "核对南桥名册",
  missing_updater: "未登记的标识",
};

const codeBodies = {
  update_qinglan_flow: "def update_qinglan_flow(rows):\n    \"\"\"按 SOP-FLOW-01 追加青岚当日申赎。\"\"\"\n    rows.append(today_flow())\n    return len(rows)\n",
  sync_chengjiang_terms: "def sync_chengjiang_terms(rows, incoming):\n    \"\"\"按 SOP-TERMS-02 覆盖澄江条款。字段不一致就拒绝。\"\"\"\n    if extra_fields(incoming):\n        raise ValueError(\"phone\")\n    return rows\n",
  refresh_nanqiao_roster: "def refresh_nanqiao_roster(catalog):\n    \"\"\"按 SOP-ROSTER-03 核对南桥网点名册。\"\"\"\n    catalog.touch()\n    return catalog\n",
};

const catalogs = [
  {
    id: "qinglan",
    sourceFile: "青岚日申赎.xlsx",
    tableName: "qinglan_daily_flow",
    summary: "青岚理财各产品每个工作日的申购金额和赎回笔数，按办理网点分行。",
    updatedAt: "2026-11-07 18:10",
    fields: [
      { name: "biz_date", description: "业务日期，格式为年月日。" },
      { name: "product_name", description: "理财产品名称。" },
      { name: "subscribe_amt", description: "当日申购金额，单位是元。" },
      { name: "redeem_count", description: "当日赎回笔数。" },
      { name: "branch_name", description: "办理网点的名称。" },
    ],
  },
  {
    id: "chengjiang",
    sourceFile: "澄江产品条款.csv",
    tableName: "chengjiang_terms",
    summary: "澄江系列产品已登记的风险等级、最短持有天数和赎回到账时间。",
    updatedAt: "2026-11-01 09:00",
    fields: [
      { name: "product_name", description: "产品名称。" },
      { name: "risk_level", description: "销售文件中的风险等级，例如 R1、R2。等级说明不是收益。" },
      { name: "min_hold_days", description: "最短持有天数。" },
      { name: "redeem_arrival", description: "赎回资金到账时间的文字说明。" },
    ],
  },
  {
    id: "nanqiao",
    sourceFile: "南桥网点名册.md",
    tableName: "nanqiao_branches",
    summary: "南桥片区网点名称、所在城市和负责人。元数据没有把它和申赎流水关联。",
    updatedAt: "2026-10-20 11:20",
    fields: [
      { name: "branch_name", description: "网点名称。" },
      { name: "city", description: "网点所在城市。" },
      { name: "manager", description: "网点负责人姓名。" },
    ],
  },
  {
    id: "lanqiao",
    sourceFile: "岚桥备付金说明.xlsx",
    tableName: "lanqiao_reserve",
    summary: "岚桥备付金台账。字段已经登记，当前没有数据行。",
    updatedAt: "2026-11-06 15:40",
    fields: [
      { name: "as_of", description: "备付金观察日。" },
      { name: "reserve_amt", description: "备付金余额，单位元。说明里的 500000 只是口径示例，不是余额。" },
    ],
  },
];

const rows = {
  qinglan: [],
  chengjiang: [
    { product_name: "澄江稳盈90", risk_level: "R2", min_hold_days: 90, redeem_arrival: "T+2" },
    { product_name: "澄江货币B", risk_level: "R1", min_hold_days: 0, redeem_arrival: "T+1" },
  ],
  nanqiao: [
    { branch_name: "南桥支行", city: "澄江市", manager: "周宁" },
    { branch_name: "西岭支行", city: "澄江市", manager: "何曼" },
  ],
  lanqiao: [],
};

function dateOffset(days) {
  const date = new Date();
  date.setDate(date.getDate() + days);
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function nowLabel() {
  const date = new Date();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hour = String(date.getHours()).padStart(2, "0");
  const minute = String(date.getMinutes()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day} ${hour}:${minute}`;
}

function todayKey() {
  return dateOffset(0);
}

rows.qinglan.push(
  { biz_date: dateOffset(-1), product_name: "青岚稳健180", subscribe_amt: 1280000, redeem_count: 6, branch_name: "南桥支行" },
  { biz_date: dateOffset(-1), product_name: "青岚货币A", subscribe_amt: 860000, redeem_count: 2, branch_name: "南桥支行" },
  { biz_date: dateOffset(-1), product_name: "青岚稳健180", subscribe_amt: 430000, redeem_count: 1, branch_name: "西岭支行" },
  { biz_date: dateOffset(-2), product_name: "青岚稳健180", subscribe_amt: 510000, redeem_count: 3, branch_name: "西岭支行" },
  { biz_date: dateOffset(-3), product_name: "青岚货币A", subscribe_amt: 240000, redeem_count: 1, branch_name: "南桥支行" },
);

const jobs = [
  {
    id: "job-flow",
    name: "青岚日申赎更新",
    sop: "SOP-FLOW-01",
    sopText: "每个交易日追加青岚当日申赎流水。",
    runAt: "18:10",
    fileIds: ["qinglan"],
    codeId: "update_qinglan_flow",
    status: "succeeded",
    lastRun: `${dateOffset(-1)} 18:10`,
    successDay: dateOffset(-1),
    error: "",
  },
  {
    id: "job-terms",
    name: "澄江条款同步",
    sop: "SOP-TERMS-02",
    sopText: "按条款字段覆盖澄江产品表。多出的字段整次不写入。",
    runAt: "09:20",
    fileIds: ["chengjiang"],
    codeId: "sync_chengjiang_terms",
    status: "failed",
    lastRun: `${dateOffset(-1)} 09:20`,
    successDay: "",
    error: "字段与元数据不一致：多出 phone。本次没有写入。",
  },
  {
    id: "job-roster",
    name: "南桥名册刷新",
    sop: "SOP-ROSTER-03",
    sopText: "核对南桥网点名册。这条任务还没有指定代码。",
    runAt: "08:00",
    fileIds: ["nanqiao"],
    codeId: "",
    status: "never",
    lastRun: "",
    successDay: "",
    error: "",
  },
];

const suggestions = [
  "青岚稳健180昨天申购了多少",
  "按产品汇总青岚的申购金额",
  "岚桥备付金是多少",
  "每个网点的负责人和赎回笔数",
  "现在更新青岚的数据",
  "澄江稳盈近一年收益是多少",
];

const state = {
  view: "ask",
  selectedId: "qinglan",
  pending: null,
  archiveNote: null,
  recent: [],
  openJobId: null,
  preview: "",
  memories: [],
  uploads: [],
};

const ui = {
  sidebar: document.querySelector("#sidebar"),
  scrim: document.querySelector("#scrim"),
  jobList: document.querySelector("#jobList"),
  messages: document.querySelector("#messages"),
  welcome: document.querySelector("#welcome"),
  suggestions: document.querySelector("#suggestions"),
  app: document.querySelector("#app"),
  form: document.querySelector("#composer"),
  question: document.querySelector("#question"),
  webSearch: document.querySelector("#webSearch"),
  formError: document.querySelector("#formError"),
  askView: document.querySelector("#askView"),
  jobsView: document.querySelector("#jobsView"),
  archiveView: document.querySelector("#archiveView"),
  skillsView: document.querySelector("#skillsView"),
  pluginsView: document.querySelector("#pluginsView"),
  memoryView: document.querySelector("#memoryView"),
  uploadView: document.querySelector("#uploadView"),
  databaseView: document.querySelector("#databaseView"),
  recentList: document.querySelector("#recentList"),
  brandHome: document.querySelector("#brandHome"),
  catalogList: document.querySelector("#catalogList"),
  catalogDetail: document.querySelector("#catalogDetail"),
  viewKicker: document.querySelector("#viewKicker"),
  viewTitle: document.querySelector("#viewTitle"),
  openAsk: document.querySelector("#openAsk"),
  openJobs: document.querySelector("#openJobs"),
  openSkills: document.querySelector("#openSkills"),
  openPlugins: document.querySelector("#openPlugins"),
  jobDetail: document.querySelector("#jobDetail"),
  userMenu: document.querySelector("#userMenu"),
  openUserMenu: document.querySelector("#openUserMenu"),
  memoryList: document.querySelector("#memoryList"),
  memoryInput: document.querySelector("#memoryInput"),
  uploadList: document.querySelector("#uploadList"),
  databaseList: document.querySelector("#databaseList"),
  pluginWebState: document.querySelector("#pluginWebState"),
  openSidebar: document.querySelector("#openSidebar"),
  closeSidebar: document.querySelector("#closeSidebar"),
  dialog: document.querySelector("#jobDialog"),
  jobForm: document.querySelector("#jobForm"),
  codeSelect: document.querySelector("#codeSelect"),
};

const statusText = { succeeded: "成功", failed: "失败", never: "尚未运行", running: "运行中" };
const fieldLabels = {
  biz_date: "业务日期",
  product_name: "产品",
  subscribe_amt: "申购金额（元）",
  redeem_count: "赎回笔数",
  branch_name: "网点",
  risk_level: "风险等级",
  min_hold_days: "最短持有天数",
  redeem_arrival: "赎回到账",
  city: "城市",
  manager: "负责人",
  as_of: "观察日",
  reserve_amt: "备付金余额（元）",
};

function catalogById(id) {
  return catalogs.find((item) => item.id === id);
}

function money(value) {
  return Number(value).toLocaleString("zh-CN");
}

function cloneRows(id) {
  return rows[id].map((row) => ({ ...row }));
}

function setSidebar(open) {
  ui.sidebar.classList.toggle("is-open", open);
  ui.scrim.hidden = !open;
}

function toggleSidebar() {
  if (window.matchMedia("(max-width: 860px)").matches) {
    setSidebar(!ui.sidebar.classList.contains("is-open"));
    return;
  }
  ui.app.classList.toggle("sidebar-collapsed");
}

function showView(view) {
  state.view = view;
  const nodes = {
    ask: ui.askView,
    jobs: ui.jobsView,
    archive: ui.archiveView,
    skills: ui.skillsView,
    plugins: ui.pluginsView,
    memory: ui.memoryView,
    upload: ui.uploadView,
    database: ui.databaseView,
  };
  for (const [name, node] of Object.entries(nodes)) node.hidden = name !== view;
  ui.openAsk.classList.toggle("is-active", view === "ask");
  ui.openJobs.classList.toggle("is-active", view === "jobs");
  ui.openSkills.classList.toggle("is-active", view === "skills");
  ui.openPlugins.classList.toggle("is-active", view === "plugins");
  const titles = {
    ask: ["对话", "灵库"],
    jobs: ["定时任务", "每日更新"],
    archive: ["维护", "文件档案"],
    skills: ["技能", "可用技能"],
    plugins: ["插件", "已安装"],
    memory: ["个性化", "长期记忆"],
    upload: ["上传", "文件"],
    database: ["数据库", "已登记的表"],
  };
  ui.viewKicker.textContent = titles[view][0];
  ui.viewTitle.textContent = titles[view][1];
  if (view === "archive") renderArchive();
  if (view === "jobs") renderJobs();
  if (view === "memory") renderMemories();
  if (view === "upload") renderUploads();
  if (view === "database") renderDatabase();
  if (view === "plugins") renderPluginState();
  ui.userMenu.hidden = true;
  ui.openUserMenu.setAttribute("aria-expanded", "false");
  setSidebar(false);
}

function newChat() {
  state.pending = null;
  ui.formError.hidden = true;
  ui.question.value = "";
  ui.messages.querySelectorAll(".message").forEach((node) => node.remove());
  ui.welcome.hidden = false;
  showView("ask");
}

function remember(text) {
  state.recent = [text, ...state.recent.filter((item) => item !== text)].slice(0, 8);
  renderRecent();
}

function renderRecent() {
  ui.recentList.replaceChildren();
  if (!state.recent.length) {
    const empty = document.createElement("p");
    empty.className = "recent-empty";
    empty.textContent = "还没有提问";
    ui.recentList.append(empty);
    return;
  }
  for (const text of state.recent) {
    const button = document.createElement("button");
    button.className = "recent-item";
    button.type = "button";
    const label = document.createElement("span");
    label.textContent = text;
    button.append(label);
    button.addEventListener("click", () => submitQuestion(text));
    ui.recentList.append(button);
  }
}

function renderJobs() {
  const opened = jobs.find((item) => item.id === state.openJobId);
  ui.jobList.hidden = Boolean(opened);
  ui.jobDetail.hidden = !opened;
  if (opened) {
    renderJobDetail(opened);
    return;
  }
  ui.jobList.replaceChildren();
  for (const job of jobs) {
    const card = document.createElement("article");
    card.className = `job-card ${job.status}`;
    const title = document.createElement("h3");
    title.textContent = job.name;
    const pill = document.createElement("span");
    pill.className = "status-pill";
    pill.textContent = statusText[job.status];
    title.append(pill);
    const meta = document.createElement("p");
    meta.className = "job-meta";
    const code = job.codeId ? codes[job.codeId] || job.codeId : "未指定";
    meta.append(document.createTextNode(`${job.sop} · 每天 ${job.runAt || "未设置"}`), document.createElement("br"));
    meta.append(document.createTextNode(job.sopText), document.createElement("br"));
    meta.append(document.createTextNode("代码 "));
    const codeNode = document.createElement("code");
    codeNode.textContent = job.codeId || "无";
    meta.append(codeNode, document.createTextNode(` · ${code}`));
    if (job.lastRun) meta.append(document.createElement("br"), document.createTextNode(`最近运行 ${job.lastRun}`));
    card.append(title, meta);
    if (job.error) {
      const error = document.createElement("p");
      error.className = "job-error";
      error.textContent = job.error;
      card.append(error);
    }
    const actions = document.createElement("div");
    actions.className = "chip-row";
    const open = actionButton("查看", () => {
      state.openJobId = job.id;
      state.preview = "";
      renderJobs();
    });
    const button = actionButton(job.status === "running" ? "运行中" : "演示运行", (event) => {
      event.stopPropagation();
      runJob(job.id);
    });
    button.disabled = job.status === "running";
    actions.append(open, button);
    card.append(actions);
    ui.jobList.append(card);
  }
}

function renderJobDetail(job) {
  const detail = ui.jobDetail;
  detail.replaceChildren();
  const back = actionButton("返回全部任务", () => {
    state.openJobId = null;
    state.preview = "";
    renderJobs();
  });
  const title = document.createElement("h2");
  title.textContent = job.name;
  const pill = document.createElement("span");
  pill.className = "status-pill";
  pill.textContent = statusText[job.status];
  title.append(pill);
  detail.append(back, title);
  if (job.error) {
    const error = document.createElement("p");
    error.className = "job-error";
    error.textContent = job.error;
    detail.append(error);
  }

  const grid = document.createElement("div");
  grid.className = "detail-grid";
  const description = document.createElement("label");
  description.append(document.createTextNode("任务描述"));
  const descriptionInput = document.createElement("textarea");
  descriptionInput.value = job.sopText;
  descriptionInput.addEventListener("change", () => {
    job.sopText = descriptionInput.value.trim() || job.sopText;
  });
  description.append(descriptionInput);
  const time = document.createElement("label");
  time.append(document.createTextNode("每天运行"));
  const timeInput = document.createElement("input");
  timeInput.type = "time";
  timeInput.value = job.runAt || "09:00";
  timeInput.addEventListener("change", () => {
    job.runAt = timeInput.value;
  });
  time.append(timeInput);
  grid.append(description, time);

  const files = document.createElement("div");
  const filesLabel = document.createElement("p");
  filesLabel.textContent = "涉及文件";
  const fileRow = document.createElement("div");
  fileRow.className = "chip-row";
  for (const fileId of job.fileIds || []) {
    const catalog = catalogById(fileId);
    if (!catalog) continue;
    const chip = actionButton(catalog.sourceFile, () => {
      state.preview = `file:${fileId}`;
      renderJobs();
    });
    chip.classList.toggle("is-active", state.preview === `file:${fileId}`);
    fileRow.append(chip);
  }
  if (!fileRow.childElementCount) fileRow.textContent = "还没有选择文件。";
  files.append(filesLabel, fileRow);

  const codeWrap = document.createElement("div");
  const codeLabel = document.createElement("p");
  codeLabel.textContent = "指定代码";
  const codeChip = actionButton(job.codeId || "未指定代码", () => {
    state.preview = "code";
    renderJobs();
  });
  codeChip.classList.toggle("is-active", state.preview === "code");
  codeWrap.append(codeLabel, codeChip);
  grid.append(files, codeWrap);
  detail.append(grid);

  const preview = document.createElement("div");
  preview.className = "preview";
  if (state.preview.startsWith("file:")) {
    const catalog = catalogById(state.preview.slice(5));
    if (catalog) {
      const summary = document.createElement("p");
      summary.className = "summary";
      summary.textContent = `${catalog.summary} 表名 ${catalog.tableName}。`;
      preview.append(summary);
      const data = rows[catalog.id];
      preview.append(data.length ? resultBlock("样例数据", renderTable(catalog.fields.map((field) => field.name), data)) : Object.assign(document.createElement("p"), { className: "empty-rows", textContent: "这张表目前没有数据行。" }));
    }
  } else if (state.preview === "code") {
    const block = document.createElement("pre");
    block.className = "code-view";
    block.textContent = job.codeId && codeBodies[job.codeId] ? codeBodies[job.codeId] : "未指定代码，任务不会运行。";
    preview.append(resultBlock(job.codeId || "代码", block));
  }
  detail.append(preview);

  const actions = document.createElement("div");
  actions.className = "chip-row";
  const run = actionButton(job.status === "running" ? "运行中" : "演示运行", () => runJob(job.id));
  run.disabled = job.status === "running";
  const cancel = actionButton("取消任务", () => {
    const index = jobs.findIndex((item) => item.id === job.id);
    if (index >= 0) jobs.splice(index, 1);
    state.openJobId = null;
    state.preview = "";
    renderJobs();
  });
  actions.append(run, cancel);
  detail.append(actions);
}

function runJob(id) {
  const job = jobs.find((item) => item.id === id);
  if (!job || job.status === "running") return;
  if (!job.codeId) {
    job.status = "never";
    job.error = "未指定代码，任务不运行。表数据保持原样。";
    job.lastRun = nowLabel();
    renderJobs();
    return;
  }
  if (!Object.prototype.hasOwnProperty.call(codes, job.codeId) || job.codeId === "missing_updater") {
    job.status = "failed";
    job.error = "指定的代码不存在，数据保持原样。";
    job.lastRun = nowLabel();
    renderJobs();
    return;
  }
  if (job.successDay === todayKey()) {
    job.error = "当天已经成功运行，不再写入。";
    renderJobs();
    return;
  }

  const before = cloneRows(job.codeId === "sync_chengjiang_terms" ? "chengjiang" : "qinglan");
  job.status = "running";
  job.error = "";
  renderJobs();
  const delay = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 500;
  window.setTimeout(() => finishJob(job, before), delay);
}

function finishJob(job, before) {
  if (job.codeId === "update_qinglan_flow") {
    rows.qinglan.unshift({
      biz_date: todayKey(),
      product_name: "青岚稳健180",
      subscribe_amt: 210000,
      redeem_count: 1,
      branch_name: "南桥支行",
    });
    catalogById("qinglan").updatedAt = nowLabel();
    job.status = "succeeded";
    job.successDay = todayKey();
    job.lastRun = nowLabel();
    job.error = "";
  } else if (job.codeId === "sync_chengjiang_terms") {
    rows.chengjiang.splice(0, rows.chengjiang.length, ...before);
    job.status = "failed";
    job.lastRun = nowLabel();
    job.error = "字段与元数据不一致：多出 phone。本次没有写入。";
  } else if (job.codeId === "refresh_nanqiao_roster") {
    catalogById("nanqiao").updatedAt = nowLabel();
    job.status = "succeeded";
    job.successDay = todayKey();
    job.lastRun = nowLabel();
    job.error = "";
  }
  renderJobs();
  if (state.view === "archive") renderArchive();
}

function addMessage(role, content) {
  ui.welcome.hidden = true;
  const item = document.createElement("article");
  item.className = `message ${role}`;
  if (role === "user") {
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = content;
    item.append(bubble);
  } else {
    item.append(renderAnswer(content));
  }
  ui.messages.append(item);
  ui.messages.scrollTop = ui.messages.scrollHeight;
}

function renderAnswer(answer) {
  const box = document.createElement("div");
  box.className = "answer";
  const kind = document.createElement("p");
  kind.className = "kind";
  kind.textContent = answer.kind;
  const trace = document.createElement("p");
  trace.className = "trace";
  trace.textContent = answer.trace;
  const text = document.createElement("p");
  text.textContent = answer.text;
  box.append(kind, trace, text);
  if (answer.columns && answer.records) {
    box.append(resultBlock("查询结果", renderTable(answer.columns, answer.records)));
  }
  if (answer.choices) {
    const choices = document.createElement("div");
    choices.className = "choices";
    for (const choice of answer.choices) {
      const button = document.createElement("button");
      button.className = "choice";
      button.type = "button";
      button.textContent = choice.label;
      button.addEventListener("click", () => submitQuestion(choice.value));
      choices.append(button);
    }
    box.append(choices);
  }
  const sources = answer.sources || (answer.source ? [answer.source] : []);
  sources.forEach((item, index) => {
    const source = document.createElement("p");
    source.className = "source";
    source.append(document.createTextNode(`[${index + 1}] ${item.file} · 表 `));
    const table = document.createElement("code");
    table.textContent = item.table;
    source.append(table, document.createTextNode(` · 字段 ${item.fields.join("、")}`));
    box.append(source);
  });
  return box;
}

function resultBlock(label, table) {
  const block = document.createElement("div");
  block.className = "result-block";
  const bar = document.createElement("div");
  bar.className = "result-bar";
  bar.textContent = label;
  block.append(bar, table);
  return block;
}

function renderTable(columns, records) {
  const table = document.createElement("table");
  table.className = "result-table";
  const head = document.createElement("tr");
  for (const column of columns) {
    const cell = document.createElement("th");
    cell.textContent = fieldLabels[column] || column;
    head.append(cell);
  }
  const thead = document.createElement("thead");
  thead.append(head);
  const body = document.createElement("tbody");
  for (const record of records) {
    const row = document.createElement("tr");
    for (const column of columns) {
      const cell = document.createElement("td");
      const value = record[column];
      cell.textContent = column.endsWith("_amt") ? money(value) : String(value);
      row.append(cell);
    }
    body.append(row);
  }
  table.append(thead, body);
  return table;
}

function sourceOf(id, fields) {
  const catalog = catalogById(id);
  return { file: catalog.sourceFile, table: catalog.tableName, fields };
}

function traceRead(chosen) {
  const names = catalogs.map((item) => item.tableName).join("、");
  return `已读 ${catalogs.length} 组元数据（${names}）。选用 ${chosen}。`;
}

function answerQuestion(raw) {
  const text = raw.trim();
  if (!text) return { error: "请先写下要查的内容或要完成的需求。" };
  if (text.length > 4000) return { error: "内容超过 4000 字，没有进入查询。" };

  if (state.pending === "join") {
    state.pending = null;
    if (/分开|不要|先不/.test(text)) return separateAnswer();
    if (/对上|关联|可以|确认|按网点/.test(text)) return joinedAnswer();
    state.pending = "join";
    return askJoin();
  }

  if (/更新|同步|跑一下|执行/.test(text)) {
    return {
      kind: "不运行更新",
      trace: "对话只读元数据和表数据。",
      text: "数据更新在左侧每日任务里运行。青岚日申赎对应任务「青岚日申赎更新」，代码是 update_qinglan_flow。",
    };
  }
  if (/备付|岚桥/.test(text)) {
    return {
      kind: "数据查找",
      trace: `${traceRead("lanqiao_reserve")} 字段说明里的 500000 没有当作余额。`,
      text: "岚桥备付金说明已经登记，表中没有记录。",
      source: sourceOf("lanqiao", ["as_of", "reserve_amt"]),
    };
  }
  if (/收益|净值|收益率/.test(text)) {
    return {
      kind: "没有对应的表",
      trace: `已读 ${catalogs.length} 组元数据，没有字段记载收益或净值。`,
      text: "没有对应的文件表，演示里也不编造收益。正式系统里，这类问题会转到原来的资料查询。",
    };
  }
  if (/负责人/.test(text) && /赎回|申购|青岚|网点/.test(text)) return askJoin();
  if (/汇总|合计|按产品/.test(text) && /青岚|申购/.test(text)) return aggregateAnswer();
  if (/青岚/.test(text)) return qinglanAnswer(text);
  if (/澄江/.test(text)) return chengjiangAnswer(text);
  if (/南桥|西岭|网点|负责人/.test(text)) return branchAnswer(text);
  return {
    kind: "没有对应的表",
    trace: `已读 ${catalogs.length} 组元数据，没有一组说明对得上这个问题。`,
    text: "没有对应的文件表。演示里没有文档库，所以不会另外编造一段资料回答。",
  };
}

function askJoin() {
  state.pending = "join";
  return {
    kind: "需要确认",
    trace: "青岚日申赎和南桥网点名册都有网点名称。元数据没有写明这两张表可以关联。",
    text: "可以按网点名称对上，也可以分开看。未确认之前，不会合成一张结果。",
    choices: [
      { label: "按网点名称对上", value: "按网点名称对上" },
      { label: "分开看", value: "分开看" },
    ],
  };
}

function aggregateAnswer() {
  const totals = new Map();
  for (const row of rows.qinglan) {
    const current = totals.get(row.product_name) || { product_name: row.product_name, subscribe_amt: 0, redeem_count: 0 };
    current.subscribe_amt += row.subscribe_amt;
    current.redeem_count += row.redeem_count;
    totals.set(row.product_name, current);
  }
  return {
    kind: "需求完成",
    trace: `${traceRead("qinglan_daily_flow")} 按 product_name 汇总 subscribe_amt 和 redeem_count。`,
    text: "下面是青岚日申赎表里现有记录按产品汇总的结果。",
    columns: ["product_name", "subscribe_amt", "redeem_count"],
    records: [...totals.values()],
    source: sourceOf("qinglan", ["product_name", "subscribe_amt", "redeem_count"]),
  };
}

function qinglanAnswer(text) {
  let matched = rows.qinglan;
  const fields = ["product_name"];
  if (/昨天/.test(text)) {
    matched = matched.filter((row) => row.biz_date === dateOffset(-1));
    fields.push("biz_date");
  } else if (/今天/.test(text)) {
    matched = matched.filter((row) => row.biz_date === todayKey());
    fields.push("biz_date");
  }
  const product = ["青岚稳健180", "青岚货币A"].find((name) => text.includes(name));
  if (product) matched = matched.filter((row) => row.product_name === product);
  if (/南桥/.test(text)) {
    matched = matched.filter((row) => row.branch_name === "南桥支行");
    fields.push("branch_name");
  }
  if (/西岭/.test(text)) {
    matched = matched.filter((row) => row.branch_name === "西岭支行");
    fields.push("branch_name");
  }
  if (!matched.length) {
    return {
      kind: "数据查找",
      trace: `${traceRead("qinglan_daily_flow")} 过滤后没有记录。`,
      text: "青岚日申赎表里没有对应数据。",
      source: sourceOf("qinglan", fields),
    };
  }
  if (/赎回/.test(text)) fields.push("redeem_count");
  else fields.push("subscribe_amt");
  const uniqueFields = [...new Set(fields)];
  const amount = matched.reduce((sum, row) => sum + row.subscribe_amt, 0);
  const count = matched.reduce((sum, row) => sum + row.redeem_count, 0);
  const when = /昨天/.test(text) ? "昨天" : /今天/.test(text) ? "今天" : "现有记录";
  const who = product || "青岚全部产品";
  const measure = /赎回/.test(text) ? `赎回 ${count} 笔` : `申购 ${money(amount)} 元`;
  return {
    kind: "数据查找",
    trace: `${traceRead("qinglan_daily_flow")} 只读取 ${uniqueFields.join("、")}。`,
    text: `${who}${when}的${measure}。`,
    columns: ["biz_date", "product_name", "branch_name", "subscribe_amt", "redeem_count"],
    records: matched,
    source: sourceOf("qinglan", uniqueFields),
  };
}

function chengjiangAnswer(text) {
  const product = ["澄江稳盈90", "澄江货币B"].find((name) => text.includes(name.replace("90", "")) || text.includes(name));
  const matched = rows.chengjiang.filter((row) => !product || row.product_name.includes(product.replace("90", "")));
  if (!matched.length) {
    return {
      kind: "数据查找",
      trace: `${traceRead("chengjiang_terms")} 没有这个产品的记录。`,
      text: "澄江产品条款表里没有对应数据。",
      source: sourceOf("chengjiang", ["product_name"]),
    };
  }
  const fields = /到账|赎回/.test(text)
    ? ["product_name", "redeem_arrival"]
    : /持有/.test(text)
      ? ["product_name", "min_hold_days"]
      : ["product_name", "risk_level"];
  return {
    kind: "数据查找",
    trace: `${traceRead("chengjiang_terms")} 条款表没有收益字段。`,
    text: "下面是条款表里能够对上的记录。",
    columns: ["product_name", "risk_level", "min_hold_days", "redeem_arrival"],
    records: matched,
    source: sourceOf("chengjiang", fields),
  };
}

function branchAnswer() {
  return {
    kind: "数据查找",
    trace: `${traceRead("nanqiao_branches")} 这一问只涉及网点名册。`,
    text: "下面是南桥网点名册中的记录。",
    columns: ["branch_name", "city", "manager"],
    records: rows.nanqiao,
    source: sourceOf("nanqiao", ["branch_name", "city", "manager"]),
  };
}

function separateAnswer() {
  const qinglan = {};
  for (const row of rows.qinglan) qinglan[row.branch_name] = (qinglan[row.branch_name] || 0) + row.redeem_count;
  const records = Object.entries(qinglan).map(([branch_name, redeem_count]) => ({ branch_name, redeem_count }));
  return {
    kind: "数据查找",
    trace: "按你的选择分开查看。两张表没有建立关联。",
    text: "先给出申赎表按网点汇总的赎回笔数。负责人仍在南桥网点名册里，需要的话可以单独问。",
    columns: ["branch_name", "redeem_count"],
    records,
    source: sourceOf("qinglan", ["branch_name", "redeem_count"]),
  };
}

function joinedAnswer() {
  const records = rows.qinglan.map((row) => {
    const branch = rows.nanqiao.find((item) => item.branch_name === row.branch_name);
    return {
      biz_date: row.biz_date,
      branch_name: row.branch_name,
      manager: branch ? branch.manager : "名册中没有",
      redeem_count: row.redeem_count,
    };
  });
  return {
    kind: "需求完成",
    trace: "已按你的确认，用网点名称把两张表对上。元数据里仍然没有登记这层关联。",
    text: "下面是对上之后的赎回笔数和负责人。",
    columns: ["biz_date", "branch_name", "manager", "redeem_count"],
    records,
    sources: [
      sourceOf("qinglan", ["branch_name", "redeem_count"]),
      sourceOf("nanqiao", ["branch_name", "manager"]),
    ],
  };
}

function submitQuestion(text) {
  ui.formError.hidden = true;
  const answer = answerQuestion(text);
  if (answer.error) {
    ui.formError.hidden = false;
    ui.formError.textContent = answer.error;
    return;
  }
  showView("ask");
  addMessage("user", text);
  addMessage("assistant", annotate(answer));
  remember(text);
  ui.question.value = "";
}

function renderArchive() {
  ui.catalogList.replaceChildren();
  for (const catalog of catalogs) {
    const button = document.createElement("button");
    button.className = `file-card${catalog.id === state.selectedId ? " is-active" : ""}`;
    button.type = "button";
    const title = document.createElement("strong");
    title.textContent = catalog.sourceFile;
    const table = document.createElement("small");
    table.textContent = catalog.tableName;
    button.append(title, table);
    button.addEventListener("click", () => {
      state.selectedId = catalog.id;
      state.archiveNote = null;
      renderArchive();
    });
    ui.catalogList.append(button);
  }

  const catalog = catalogById(state.selectedId);
  const detail = ui.catalogDetail;
  detail.replaceChildren();
  const heading = document.createElement("h2");
  heading.textContent = catalog.sourceFile;
  const summary = document.createElement("p");
  summary.className = "summary";
  summary.textContent = `${catalog.summary} 表名 ${catalog.tableName}。元数据更新于 ${catalog.updatedAt}。`;
  detail.append(heading, summary);

  if (state.archiveNote && state.archiveNote.id === catalog.id) {
    const banner = document.createElement("p");
    banner.className = `banner ${state.archiveNote.tone}`;
    banner.textContent = state.archiveNote.text;
    detail.append(banner);
  }

  const fields = document.createElement("ul");
  fields.className = "field-list";
  for (const field of catalog.fields) {
    const item = document.createElement("li");
    item.className = "field";
    const name = document.createElement("b");
    const code = document.createElement("code");
    code.textContent = field.name;
    name.append(code, document.createTextNode(` ${fieldLabels[field.name] || ""}`));
    const description = document.createElement("span");
    description.textContent = field.description;
    item.append(name, description);
    fields.append(item);
  }
  detail.append(fields);

  if (catalog.id === "qinglan") {
    const actions = document.createElement("div");
    actions.className = "detail-actions";
    actions.append(
      actionButton("试一次不一致的入库", () => {
        state.archiveNote = {
          id: "qinglan",
          tone: "fail",
          text: `拒绝入库。多了字段 phone。原有 ${rows.qinglan.length} 条还在。`,
        };
        renderArchive();
      }),
      actionButton("按现有字段追加一行", () => {
        rows.qinglan.unshift({
          biz_date: todayKey(),
          product_name: "青岚货币A",
          subscribe_amt: 50000,
          redeem_count: 0,
          branch_name: "西岭支行",
        });
        catalog.updatedAt = nowLabel();
        state.archiveNote = { id: "qinglan", tone: "ok", text: "已按字段清单追加 1 条。" };
        renderArchive();
      }),
    );
    detail.append(actions);
  }

  const data = rows[catalog.id];
  if (!data.length) {
    const empty = document.createElement("p");
    empty.className = "empty-rows";
    empty.textContent = "这张表目前没有数据行。";
    detail.append(empty);
    return;
  }
  detail.append(resultBlock("样例数据", renderTable(catalog.fields.map((field) => field.name), data)));
}

function annotate(answer) {
  if (!answer.trace) return answer;
  const note = ui.webSearch.checked ? "联网搜索已打开，演示不接入真实网页。" : "联网搜索已关闭。";
  return { ...answer, trace: `${answer.trace} ${note}` };
}

function renderMemories() {
  ui.memoryList.replaceChildren();
  if (!state.memories.length) {
    const item = document.createElement("li");
    item.textContent = "还没有长期记忆。";
    ui.memoryList.append(item);
    return;
  }
  for (const text of state.memories) {
    const item = document.createElement("li");
    item.append(document.createTextNode(text), actionButton("删除", () => {
      state.memories = state.memories.filter((entry) => entry !== text);
      renderMemories();
    }));
    ui.memoryList.append(item);
  }
}

function renderUploads() {
  ui.uploadList.replaceChildren();
  if (!state.uploads.length) {
    const item = document.createElement("li");
    item.textContent = "还没有选择文件。";
    ui.uploadList.append(item);
    return;
  }
  for (const name of state.uploads) {
    const item = document.createElement("li");
    item.textContent = `${name} · 演示未入库`;
    ui.uploadList.append(item);
  }
}

function renderDatabase() {
  ui.databaseList.replaceChildren();
  const list = document.createElement("div");
  list.className = "card-grid";
  for (const catalog of catalogs) {
    const button = document.createElement("button");
    button.className = "plain-card";
    button.type = "button";
    const title = document.createElement("strong");
    title.textContent = catalog.sourceFile;
    const meta = document.createElement("small");
    meta.textContent = `${catalog.tableName} · ${rows[catalog.id].length} 行`;
    button.append(title, meta);
    button.addEventListener("click", () => {
      state.selectedId = catalog.id;
      showView("archive");
    });
    list.append(button);
  }
  ui.databaseList.append(list);
}

function renderPluginState() {
  ui.pluginWebState.textContent = ui.webSearch.checked ? "当前已打开" : "当前已关闭";
}

function actionButton(label, onClick) {
  const button = document.createElement("button");
  button.className = "ghost";
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

function fillCodeSelect() {
  const options = [{ value: "", label: "不指定代码" }, ...Object.entries(codes).map(([value, label]) => ({ value, label }))];
  for (const option of options) {
    const item = document.createElement("option");
    item.value = option.value;
    item.textContent = option.value ? `${option.value} · ${option.label}` : option.label;
    ui.codeSelect.append(item);
  }
}

function bind() {
  for (const prompt of suggestions) {
    const button = document.createElement("button");
    button.className = "suggestion";
    button.type = "button";
    button.textContent = prompt;
    button.addEventListener("click", () => submitQuestion(prompt));
    ui.suggestions.append(button);
  }
  ui.brandHome.addEventListener("click", () => showView("ask"));
  ui.openAsk.addEventListener("click", newChat);
  ui.openJobs.addEventListener("click", () => {
    state.openJobId = null;
    state.preview = "";
    showView("jobs");
  });
  ui.openSkills.addEventListener("click", () => showView("skills"));
  ui.openPlugins.addEventListener("click", () => showView("plugins"));
  ui.openSidebar.addEventListener("click", toggleSidebar);
  ui.closeSidebar.addEventListener("click", () => setSidebar(false));
  ui.scrim.addEventListener("click", () => setSidebar(false));
  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === "s") {
      event.preventDefault();
      toggleSidebar();
    }
  });
  ui.openUserMenu.addEventListener("click", () => {
    ui.userMenu.hidden = !ui.userMenu.hidden;
    ui.openUserMenu.setAttribute("aria-expanded", String(!ui.userMenu.hidden));
  });
  ui.userMenu.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => showView(button.dataset.view));
  });
  document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => submitQuestion(button.dataset.prompt));
  });
  document.querySelector("#memoryForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = ui.memoryInput.value.trim();
    if (!text || state.memories.includes(text)) return;
    state.memories.unshift(text);
    ui.memoryInput.value = "";
    renderMemories();
  });
  document.querySelector("#uploadInput").addEventListener("change", (event) => {
    state.uploads = [...event.target.files].map((file) => file.name);
    renderUploads();
  });
  document.querySelector("#toggleWeb").addEventListener("click", () => {
    ui.webSearch.checked = !ui.webSearch.checked;
    renderPluginState();
  });
  ui.webSearch.addEventListener("change", renderPluginState);
  document.querySelector("#openJobDialog").addEventListener("click", () => ui.dialog.showModal());
  document.querySelector("#cancelJob").addEventListener("click", () => {
    ui.jobForm.reset();
    ui.dialog.close();
  });
  ui.jobForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = new FormData(ui.jobForm);
    jobs.unshift({
      id: `job-${Date.now()}`,
      name: String(data.get("name")).trim(),
      sop: String(data.get("sop")).trim(),
      sopText: String(data.get("description") || "").trim() || "新建任务，描述还可以再改。",
      runAt: String(data.get("runAt") || "09:00"),
      fileIds: data.getAll("files"),
      codeId: String(data.get("code")),
      status: "never",
      lastRun: "",
      successDay: "",
      error: "",
    });
    ui.jobForm.reset();
    ui.dialog.close();
    state.openJobId = null;
    renderJobs();
  });
  ui.form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitQuestion(ui.question.value);
  });
  ui.question.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      ui.form.requestSubmit();
    }
  });
}

fillCodeSelect();
bind();
renderJobs();
renderRecent();
showView("ask");
