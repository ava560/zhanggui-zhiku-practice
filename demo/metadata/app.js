const tables = [
  {
    tableName: "qinglan_daily_flow",
    role: "事实表",
    description: "青岚理财各产品每个工作日的申购金额和赎回笔数，按办理网点分行。",
    columns: ["biz_date", "product_name", "subscribe_amt", "redeem_count", "branch_name"],
  },
  {
    tableName: "chengjiang_terms",
    role: "维度表",
    description: "澄江系列产品已登记的风险等级、最短持有天数和赎回到账时间。",
    columns: ["product_name", "risk_level", "min_hold_days", "redeem_arrival"],
  },
  {
    tableName: "nanqiao_branches",
    role: "维度表",
    description: "南桥片区网点名称、所在城市和负责人。",
    columns: ["branch_name", "city", "manager"],
  },
  {
    tableName: "lanqiao_reserve",
    role: "事实表",
    description: "岚桥备付金台账。字段已经登记，当前没有业务数据行。",
    columns: ["as_of", "reserve_amt"],
  },
];

const columns = [
  ["qinglan_daily_flow", "biz_date", "date", "日期", ["业务日期"], "业务日期，格式为年月日。"],
  ["qinglan_daily_flow", "product_name", "text", "维度", ["产品", "理财产品"], "理财产品名称。"],
  ["qinglan_daily_flow", "subscribe_amt", "numeric", "度量", ["申购金额", "认购金额"], "当日申购金额，单位是元。"],
  ["qinglan_daily_flow", "redeem_count", "integer", "度量", ["赎回笔数"], "当日赎回笔数。这里没有赎回金额。"],
  ["qinglan_daily_flow", "branch_name", "text", "维度", ["网点"], "办理网点的名称。"],
  ["chengjiang_terms", "product_name", "text", "维度", ["产品"], "产品名称。"],
  ["chengjiang_terms", "risk_level", "text", "属性", ["风险等级"], "销售文件中的风险等级，例如 R1、R2。等级说明不是收益。"],
  ["chengjiang_terms", "min_hold_days", "integer", "属性", ["最短持有天数"], "最短持有天数。"],
  ["chengjiang_terms", "redeem_arrival", "text", "属性", ["赎回到账"], "赎回资金到账时间的文字说明。"],
  ["nanqiao_branches", "branch_name", "text", "维度", ["网点"], "网点名称。"],
  ["nanqiao_branches", "city", "text", "属性", ["城市"], "网点所在城市。"],
  ["nanqiao_branches", "manager", "text", "属性", ["负责人"], "网点负责人姓名。"],
  ["lanqiao_reserve", "as_of", "date", "日期", ["观察日"], "备付金观察日。"],
  ["lanqiao_reserve", "reserve_amt", "numeric", "度量", ["备付金", "备付金余额"], "备付金余额，单位元。说明里的 500000 只是口径示例，不是余额。"],
].map(([tableName, columnName, dataType, role, alias, description]) => ({
  tableName, columnName, dataType, role, alias, description,
}));

const metrics = [
  {
    name: "申购金额",
    description: "青岚日申赎在选定日期、产品或网点之后，申购金额加总。",
    alias: ["认购金额", "买入金额"],
    fields: ["qinglan_daily_flow.subscribe_amt"],
    formula: "SUM(subscribe_amt)",
  },
  {
    name: "赎回笔数",
    description: "青岚日申赎在同样过滤条件下的赎回笔数加总。不能换成金额。",
    alias: ["赎回次数"],
    fields: ["qinglan_daily_flow.redeem_count"],
    formula: "SUM(redeem_count)",
  },
  {
    name: "产品申购金额",
    description: "按理财产品分别加总申购金额。",
    alias: ["分产品申购"],
    fields: ["qinglan_daily_flow.product_name", "qinglan_daily_flow.subscribe_amt"],
    formula: "SUM(subscribe_amt) GROUP BY product_name",
  },
  {
    name: "备付金余额",
    description: "岚桥备付金在某个观察日的余额。多个观察日不相加。",
    alias: ["备付金"],
    fields: ["lanqiao_reserve.as_of", "lanqiao_reserve.reserve_amt"],
    formula: "reserve_amt WHERE as_of = 观察日",
  },
];

const sheet = (pairs) => pairs.map(([name, description]) => ({ name, description }));

const docs = [
  {
    name: "青岚日申赎.xlsx",
    description: "青岚理财每日申赎流水的来源表。",
    format: "xlsx",
    tasks: ["青岚日申赎更新"],
    fields: sheet([
      ["biz_date", "业务日期，格式为年月日。"],
      ["product_name", "理财产品名称。"],
      ["subscribe_amt", "当日申购金额，单位是元。"],
      ["redeem_count", "当日赎回笔数。"],
      ["branch_name", "办理网点的名称。"],
    ]),
  },
  {
    name: "澄江产品条款.csv",
    description: "澄江系列产品条款的来源表。",
    format: "csv",
    tasks: ["澄江条款同步"],
    fields: sheet([
      ["product_name", "产品名称。"],
      ["risk_level", "销售文件中的风险等级。等级说明不是收益。"],
      ["min_hold_days", "最短持有天数。"],
      ["redeem_arrival", "赎回资金到账时间的文字说明。"],
    ]),
  },
  {
    name: "南桥网点名册.md",
    description: "南桥片区网点名册。正文是 Markdown，不是表格文件。",
    format: "md",
    tasks: ["南桥名册刷新"],
    fields: null,
  },
  {
    name: "岚桥备付金说明.xlsx",
    description: "岚桥备付金台账的来源表。当前没有挂每日任务。",
    format: "xlsx",
    tasks: [],
    fields: sheet([
      ["as_of", "备付金观察日。"],
      ["reserve_amt", "备付金余额，单位元。说明里的 500000 只是口径示例，不是余额。"],
    ]),
  },
];

const spreadsheet = new Set(["xls", "xlsx", "xlsm", "csv"]);
const state = { tableName: "" };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function chips(values, className) {
  const wrap = el("div", "chips");
  if (!values.length) {
    wrap.append(el("span", "empty", "无"));
    return wrap;
  }
  for (const value of values) wrap.append(el("span", className, value));
  return wrap;
}

function pill(role) {
  const kind = role === "事实表" || role === "度量" ? "fact" : "";
  return el("span", `pill ${kind}`.trim(), role);
}

function mark(row, hit) {
  row.classList.toggle("is-hit", Boolean(state.tableName) && hit);
  row.classList.toggle("is-dim", Boolean(state.tableName) && !hit);
}

function selectTable(tableName) {
  state.tableName = state.tableName === tableName ? "" : tableName;
  render();
  if (state.tableName) document.querySelector("#column-info").scrollIntoView({ block: "start" });
}

function renderTables(host) {
  const section = el("section", "block");
  section.id = "table-info";
  const head = el("div", "block-head");
  head.append(el("h2", "", "库表"));
  head.querySelector("h2").append(el("span", "", "table_info"));
  section.append(head, el("p", "caption", "一行一张业务表。最后一列只保留字段名，细节在 column_info。点一行，下面两张表会标出相关字段和指标。"));
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  for (const label of ["表名", "角色", "业务描述", "字段名称"]) hr.append(el("th", "", label));
  thead.append(hr);
  const body = document.createElement("tbody");
  for (const item of tables) {
    const row = document.createElement("tr");
    mark(row, item.tableName === state.tableName);
    row.append(el("td", "name", item.tableName), cell(pill(item.role)), el("td", "desc", item.description));
    const names = document.createElement("td");
    names.append(chips(item.columns, "chip"));
    row.append(names);
    row.addEventListener("click", () => selectTable(item.tableName));
    body.append(row);
  }
  table.append(thead, body);
  const scroll = el("div", "scroll");
  scroll.append(table);
  section.append(scroll);
  host.append(section);
}

function cell(node) {
  const td = document.createElement("td");
  td.append(node);
  return td;
}

function renderColumns(host) {
  const section = el("section", "block");
  section.id = "column-info";
  const head = el("div", "block-head");
  const title = el("h2", "", "字段");
  title.append(el("span", "", "column_info"));
  head.append(title);
  section.append(head, el("p", "caption", "一行一个实际字段。所属表对应 table_info.表名。别名给提问和指标用，不代替字段名。"));
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  for (const label of ["字段名", "类型", "角色", "别名", "描述", "所属表"]) hr.append(el("th", "", label));
  thead.append(hr);
  const body = document.createElement("tbody");
  for (const item of columns) {
    const row = document.createElement("tr");
    mark(row, item.tableName === state.tableName);
    row.append(
      el("td", "name", item.columnName),
      el("td", "name", item.dataType),
      cell(pill(item.role)),
      cell(chips(item.alias, "chip")),
      el("td", "desc", item.description),
      el("td", "name", item.tableName),
    );
    row.addEventListener("click", () => selectTable(item.tableName));
    body.append(row);
  }
  table.append(thead, body);
  const scroll = el("div", "scroll");
  scroll.append(table);
  section.append(scroll);
  host.append(section);
}

function usesTable(metric, tableName) {
  return metric.fields.some((field) => field.startsWith(`${tableName}.`));
}

function renderMetrics(host) {
  const section = el("section", "block");
  section.id = "metric-info";
  const head = el("div", "block-head");
  const title = el("h2", "", "指标");
  title.append(el("span", "", "metric_info"));
  head.append(title);
  section.append(head, el("p", "caption", "指标可以跨多个字段。关联字段写成「表名.字段名」。风险等级、持有天数这类说明不做指标。"));
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  for (const label of ["指标名称", "业务描述", "别名", "关联字段", "计算公式"]) hr.append(el("th", "", label));
  thead.append(hr);
  const body = document.createElement("tbody");
  for (const item of metrics) {
    const row = document.createElement("tr");
    const tableName = item.fields[0].split(".")[0];
    mark(row, usesTable(item, state.tableName));
    row.append(
      el("td", "", item.name),
      el("td", "desc", item.description),
      cell(chips(item.alias, "chip")),
      cell(chips(item.fields, "chip")),
      el("td", "formula", item.formula),
    );
    row.addEventListener("click", () => selectTable(tableName));
    body.append(row);
  }
  table.append(thead, body);
  const scroll = el("div", "scroll");
  scroll.append(table);
  section.append(scroll);
  host.append(section);
}

function fieldList(fields, format) {
  if (!spreadsheet.has(format)) return el("span", "empty", "非表格格式，不存字段");
  const list = el("ul", "fields");
  for (const field of fields) {
    const item = document.createElement("li");
    item.append(el("code", "", field.name), el("span", "", field.description));
    list.append(item);
  }
  return list;
}

function renderDocs(host) {
  const section = el("section", "block");
  section.id = "doc-info";
  const head = el("div", "block-head");
  const title = el("h2", "", "文档");
  title.append(el("span", "", "doc_info"));
  head.append(title);
  section.append(head, el("p", "caption", "关联任务是左侧每日任务的名称。表格文件把字段和描述存在这一行；南桥名册是 md，所以字段列为空。"));
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  for (const label of ["文档名称", "描述", "格式", "关联任务", "字段及描述"]) hr.append(el("th", "", label));
  thead.append(hr);
  const body = document.createElement("tbody");
  for (const item of docs) {
    const row = document.createElement("tr");
    row.append(
      el("td", "", item.name),
      el("td", "desc", item.description),
      el("td", "name", item.format),
      cell(chips(item.tasks, "pill")),
      cell(fieldList(item.fields, item.format)),
    );
    body.append(row);
  }
  table.append(thead, body);
  const scroll = el("div", "scroll");
  scroll.append(table);
  section.append(scroll);
  host.append(section);
}

function render() {
  const host = document.querySelector("#app");
  host.replaceChildren();
  renderTables(host);
  renderColumns(host);
  renderMetrics(host);
  renderDocs(host);
}

render();
