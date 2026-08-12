/* DeepSeek 用量统计 — 前端逻辑(ECharts 图表 + 数据加载 + 同步/登录交互) */
"use strict";

/* ---------------- 主题 ---------------- */

function themeNow() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}
/* 读取当前主题的 CSS 变量(图表颜色与主题联动) */
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function hexA(hex, a) {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${a})`;
}
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("theme", t); } catch (e) {}
  $("btnTheme").textContent = t === "dark" ? "☀" : "☾";
  reloadAll();   // 图表随主题重绘
}

/* ---------------- 工具 ---------------- */

const $ = (id) => document.getElementById(id);

const MODEL_LABELS = {
  "deepseek-chat": "DeepSeek-V3 (Chat)",
  "deepseek-reasoner": "DeepSeek-R1 (Reasoner)",
  "deepseek-coder": "DeepSeek-Coder",
  "deepseek-v3.2-exp": "DeepSeek-V3.2 (Exp)",
  "deepseek-v3.1-exp": "DeepSeek-V3.1 (Exp)",
  "deepseek-v4-pro": "DeepSeek-V4 Pro",
  "deepseek-v4-flash": "DeepSeek-V4 Flash",
  "deepseek-chat & deepseek-reasoner": "V3 & R1 (合并)",
};
function modelLabel(m) {
  if (MODEL_LABELS[m]) return MODEL_LABELS[m];
  const base = m.split("-").slice(0, 2).join("-");
  return base.charAt(0).toUpperCase() + base.slice(1) + (m.includes("exp") ? " (Exp)" : "");
}

/* Nord 低饱和色板(16 色: 基础 8 色 + 同族变体, 覆盖更多模型场景) */
const PALETTE = [
  "#88C0D0", "#81A1C1", "#5E81AC", "#BF616A", "#D08770", "#EBCB8B", "#A3BE8C", "#B48EAD",
  "#8FBCBB", "#9FB3C8", "#6C7A89", "#C97B7B", "#DEB887", "#C9B37E", "#7FA68E", "#9E8FB2",
];

/* 模型稳定配色: 同一模型名在任意图表/指标下永远同一个颜色 */
function modelColor(name) {
  if (!state.modelColors[name]) {
    state.modelColors[name] = PALETTE[Object.keys(state.modelColors).length % PALETTE.length];
  }
  return state.modelColors[name];
}

function fmtTokens(n) {
  n = Number(n) || 0;
  if (n >= 1e8) return (n / 1e8).toFixed(2) + " 亿";
  if (n >= 1e4) return (n / 1e4).toFixed(1) + " 万";
  return n.toLocaleString("en-US");
}
function fmtTokensFull(n) {
  n = Number(n) || 0;
  if (n >= 1e8) return (n / 1e8).toFixed(2) + " 亿";
  if (n >= 1e4) return (n / 1e4).toFixed(2) + " 万";
  return n.toLocaleString("en-US");
}
function fmtMoney(v, currency) {
  v = Number(v) || 0;
  const sym = currency === "USD" ? "$" : "¥";
  return sym + v.toFixed(2);
}
function fmtMoneyFull(v, currency) {
  v = Number(v) || 0;
  const sym = currency === "USD" ? "$" : "¥";
  if (Math.abs(v) >= 10000) return sym + (v / 10000).toFixed(2) + " 万";
  return sym + v.toFixed(2);
}
function fmtDate(d) {
  const [y, m, day] = d.split("-");
  return `${y}/${Number(m)}/${Number(day)}`;
}
function fmtLocal(iso) {
  /* UTC ISO → 本地时间显示 */
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso.replace("T", " ").slice(0, 16);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function api(path, opts) {
  const resp = await fetch(path, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `请求失败 (${resp.status})`);
  return data;
}

let toastTimer = null;
function toast(msg, isError) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast show" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = "toast"), 3200);
}

/* ---------------- 状态 ---------------- */

const state = {
  auth: { configured: false, mode: "live" },
  dailyDays: 30,
  dailyView: "model",
  modelsMetric: "tokens",
  cumulativeMetric: "tokens",
  heatmapMetric: "tokens",
  hourlyDays: 30,
  hourlyDetailView: "type",    // 分时弹窗视图: type(计费类型) / model / key
  hourlyDetailDate: null,      // 弹窗当前展示的日期(视图切换时重新加载)
  dailyLegendVisible: null,   // null = 全部显示; Set = 手动选择的模型集合
  modelsLegendVisible: null,  // 各模型占比图例(交互与每日走势一致)
  modelColors: {},
};

/* ---------------- 图表注册 ---------------- */

const charts = {
  daily: echarts.init($("chartDaily")),
  models: echarts.init($("chartModels")),
  cumulative: echarts.init($("chartCumulative")),
  hourly: echarts.init($("chartHourly")),
};
window.addEventListener("resize", () => Object.values(charts).forEach((c) => c.resize()));
/* 容器尺寸变化(如图例渲染后变窄)时自动重绘图表, 避免画布溢出导致页面横向滚动 */
if (window.ResizeObserver) {
  const ro = new ResizeObserver(() => {
    Object.values(charts).forEach((c) => c.resize());
  });
  ["chartDaily", "chartModels", "chartCumulative", "chartHourly"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) ro.observe(el);
  });
}

function emptyOption(text) {
  return {
    title: {
      text, left: "center", top: "middle",
      textStyle: { color: cssVar("--text-faint"), fontSize: 13, fontWeight: "normal" },
    },
  };
}

/* ---------------- 工具: 日期序列 ---------------- */

function genDateRange(start, end) {
  /* 生成 start~end(含)的完整日期字符串序列 */
  const out = [];
  const cur = new Date(`${start}T00:00:00`);
  const endD = new Date(`${end}T00:00:00`);
  while (cur <= endD) {
    out.push(`${cur.getFullYear()}-${String(cur.getMonth() + 1).padStart(2, "0")}-${String(cur.getDate()).padStart(2, "0")}`);
    cur.setDate(cur.getDate() + 1);
  }
  return out;
}

function timeAxisLabel() {
  return {
    color: cssVar("--text-dim"), fontSize: 11, hideOverlap: true,
    formatter: (v) => { const d = new Date(v); return `${d.getMonth() + 1}/${d.getDate()}`; },
  };
}

/* ---------------- 各图表渲染 ---------------- */

function renderDaily(rows, start, end) {
  if (!rows || !rows.length) { charts.daily.setOption(emptyOption("暂无数据, 请先同步"), true); return; }

  // 完整日期序列(含无数据日, 补 0), 配合时间轴让刻度按真实日期比例分布
  const dates = genDateRange(start, end);
  const byDate = {};
  rows.forEach((r) => {
    byDate[r.utc_date] = byDate[r.utc_date] || {};
    const dim = state.dailyView === "key" ? (r.api_key_name || "(未命名)") : r.model;
    byDate[r.utc_date][dim] = r;
  });

  let series;
  if (state.dailyView === "model") {
    const models = [...new Set(rows.map((r) => r.model))];
    series = models.map((m) => ({
      name: modelLabel(m), type: "bar", stack: "total",
      barMaxWidth: 20,
      itemStyle: { color: modelColor(modelLabel(m)), borderRadius: [2, 2, 0, 0] },
      data: dates.map((d) => {
        const row = byDate[d] && byDate[d][m];
        return [d, row ? row.cache_hit + row.cache_miss + row.output : 0];
      }),
    }));
  } else if (state.dailyView === "key") {
    const keys = [...new Set(rows.map((r) => r.api_key_name || "(未命名)"))];
    series = keys.map((k) => ({
      name: k, type: "bar", stack: "total",
      barMaxWidth: 20,
      itemStyle: { color: modelColor(k), borderRadius: [2, 2, 0, 0] },
      data: dates.map((d) => {
        const row = byDate[d] && byDate[d][k];
        return [d, row ? row.cache_hit + row.cache_miss + row.output : 0];
      }),
    }));
  } else {
    const agg = {};
    rows.forEach((r) => {
      agg[r.utc_date] = agg[r.utc_date] || { hit: 0, miss: 0, out: 0 };
      agg[r.utc_date].hit += r.cache_hit;
      agg[r.utc_date].miss += r.cache_miss;
      agg[r.utc_date].out += r.output;
    });
    const mkSeries = (name, key) => ({
      name, type: "bar", stack: "total", barMaxWidth: 20,
      itemStyle: { color: modelColor(name), borderRadius: [2, 2, 0, 0] },
      data: dates.map((d) => [d, (agg[d] || {})[key] || 0]),
    });
    series = [
      mkSeries("输入-缓存命中", "hit"),
      mkSeries("输入-缓存未命中", "miss"),
      mkSeries("输出", "out"),
    ];
  }

  charts.daily.setOption({
    tooltip: {
      trigger: "axis",
      backgroundColor: cssVar("--card"), borderColor: cssVar("--card-border"),
      textStyle: { color: cssVar("--text"), fontSize: 12 },
      formatter: (params) => {
        // 8月12日 - 4.07亿 tokens; 下方各系列显示原始数字(千分位)
        const d = new Date(params[0].axisValue);
        const dateStr = `${d.getMonth() + 1}月${d.getDate()}日`;
        const total = params.reduce((s, p) => s + (p.value[1] || 0), 0);
        const fmtTotal = (v) => fmtTokensFull(v).replace(/\s/g, "") + " tokens";
        const fmtRaw = (v) => (Number(v) || 0).toLocaleString("en-US");
        let html = `${dateStr} - ${fmtTotal(total)}`;
        params.forEach((p) => {
          html += `<div class="tip-row"><span class="tip-name">${p.marker}${p.seriesName}</span>` +
                  `<span class="tip-val">${fmtRaw(p.value[1] || 0)}</span></div>`;
        });
        return html;
      },
    },
    axisPointer: {
      label: { formatter: (p) => {
        const dd = new Date(p.value);
        return `${dd.getMonth() + 1}-${dd.getDate()}`;
      } },
    },
    legend: { show: false, selectedMode: "multiple" },   // 图例由下方自定义 DOM 控制
    grid: { left: 56, right: 16, top: 34, bottom: 28 },
    xAxis: { type: "time", axisLine: { lineStyle: { color: cssVar("--card-border") } }, axisLabel: timeAxisLabel() },
    yAxis: { type: "value", axisLabel: { color: cssVar("--text-dim"), fontSize: 11, formatter: (v) => fmtTokens(v) },
      splitLine: { lineStyle: { color: cssVar("--card-border") } } },
    ...(state.dailyDays === 0 ? {
      // 全部视图: 滚轮在鼠标位置缩放 + 按住拖拽平移; 再次点击「全部」按钮重置(setOption notMerge)
      dataZoom: [{
        type: "inside", start: 0, end: 100,
        minValueSpan: 7 * 24 * 3600 * 1000,   // 最小跨度 7 天(time 轴单位为毫秒)
        zoomOnMouseWheel: true,
        moveOnMouseMove: true,
      }],
    } : {}),
    series,
  }, true);
  renderDailyLegend(series.map((s) => s.name));
}

/* 自定义图例: 左键点击单独显示, 再点其他多选, 点唯一项恢复全部 */
function renderDailyLegend(names) {
  const box = $("dailyLegend");
  if (!names || !names.length) { box.innerHTML = ""; return; }
  const vis = state.dailyLegendVisible;
  box.innerHTML = names.map((n) => {
    const dim = vis !== null && !vis.has(n);
    return `<span class="lg-item${dim ? " dim" : ""}" data-name="${n}">
              <span class="lg-swatch" style="background:${modelColor(n)}"></span>${n}
            </span>`;
  }).join("");
}

function applyDailyLegend() {
  const names = [...document.querySelectorAll("#dailyLegend .lg-item")].map((x) => x.dataset.name);
  const vis = state.dailyLegendVisible;
  names.forEach((n) => {
    const show = vis === null || vis.has(n);
    charts.daily.dispatchAction({ type: show ? "legendSelect" : "legendUnSelect", name: n });
  });
  renderDailyLegend(names);   // 刷新灰显状态
}

function onDailyLegendClick(e) {
  const item = e.target.closest(".lg-item");
  if (!item) return;
  const name = item.dataset.name;
  const vis = state.dailyLegendVisible;
  if (vis === null) {
    state.dailyLegendVisible = new Set([name]);          // 单独显示
  } else if (vis.has(name)) {
    if (vis.size === 1) state.dailyLegendVisible = null;  // 点唯一的项 → 恢复全部
    else vis.delete(name);                                 // 从多选集合移除
  } else {
    vis.add(name);                                         // 多选累加
  }
  applyDailyLegend();
}

function renderModels(rows) {
  if (!rows || !rows.length) { charts.models.setOption(emptyOption("暂无数据, 请先同步"), true); return; }
  const isCost = state.modelsMetric === "cost";
  const currency = "CNY"; // 优先展示 CNY
  const items = rows
    .map((r) => ({
      name: modelLabel(r.model),
      value: isCost ? (r.cost[currency] ?? Object.values(r.cost)[0] ?? 0) : r.tokens,
    }))
    .filter((x) => x.value > 0)
    .sort((a, b) => b.value - a.value);

  if (!items.length) { charts.models.setOption(emptyOption("暂无数据"), true); return; }

  // 圆心显示总用量/总花费
  const totalVal = items.reduce((s, x) => s + x.value, 0);

  charts.models.setOption({
    title: {
      text: isCost ? fmtMoneyFull(totalVal, "CNY") : fmtTokensFull(totalVal),
      left: "center", top: "middle",
      textStyle: { fontSize: 24, fontWeight: 700, color: cssVar("--text") },
    },
    tooltip: {
      trigger: "item",
      backgroundColor: cssVar("--card"), borderColor: cssVar("--card-border"),
      textStyle: { color: cssVar("--text"), fontSize: 12 },
      formatter: (p) => `${p.marker} ${p.name}<br/>${fmtTokensFull(p.value)}${isCost ? "" : " tokens"} (${p.percent.toFixed(1)}%)`,
    },
    legend: { show: false, selectedMode: "multiple" },   // 图例由右侧自定义 DOM 控制
    series: [{
      type: "pie", radius: ["45%", "75%"], center: ["50%", "50%"],
      itemStyle: { borderColor: cssVar("--card"), borderWidth: 2, borderRadius: 4 },
      label: { show: false },   // 不显示引线标签, 名称由图例/悬停提示展示
      emphasis: { scaleSize: 6 },
      data: items.map((x) => ({ ...x, itemStyle: { color: modelColor(x.name) } })),
    }],
  }, true);
  renderModelsLegend(items.map((x) => x.name));
}

/* 各模型占比: 自定义图例(与每日走势交互一致: 单独显示/多选/恢复全部) */
function renderModelsLegend(names) {
  const box = $("modelsLegend");
  if (!names || !names.length) { box.innerHTML = ""; return; }
  const vis = state.modelsLegendVisible;
  box.innerHTML = names.map((n) => {
    const dim = vis !== null && !vis.has(n);
    return `<span class="lg-item${dim ? " dim" : ""}" data-name="${n}">
              <span class="lg-swatch" style="background:${modelColor(n)}"></span>${n}
            </span>`;
  }).join("");
}

function applyModelsLegend() {
  const names = [...document.querySelectorAll("#modelsLegend .lg-item")].map((x) => x.dataset.name);
  const vis = state.modelsLegendVisible;
  names.forEach((n) => {
    const show = vis === null || vis.has(n);
    charts.models.dispatchAction({ type: show ? "legendSelect" : "legendUnSelect", name: n });
  });
  renderModelsLegend(names);
}

function onModelsLegendClick(e) {
  const item = e.target.closest(".lg-item");
  if (!item) return;
  const name = item.dataset.name;
  const vis = state.modelsLegendVisible;
  if (vis === null) {
    state.modelsLegendVisible = new Set([name]);
  } else if (vis.has(name)) {
    if (vis.size === 1) state.modelsLegendVisible = null;
    else vis.delete(name);
  } else {
    vis.add(name);
  }
  applyModelsLegend();
}

function renderCumulative(rows) {
  if (!rows || !rows.length) { charts.cumulative.setOption(emptyOption("暂无数据, 请先同步"), true); return; }
  const isCost = state.cumulativeMetric === "cost";

  // 完整日期序列(最早~最晚), 无数据日结转上一个累计值(阶梯曲线), 时间轴真实刻度
  const sorted = [...rows].sort((a, b) => a.utc_date.localeCompare(b.utc_date));
  const dates = genDateRange(sorted[0].utc_date, sorted[sorted.length - 1].utc_date);

  let series, yFmt;
  if (isCost) {
    const currs = [...new Set(rows.map((r) => r.currency))];
    series = currs.map((c, i) => {
      const byDate = {};
      rows.filter((r) => r.currency === c).forEach((r) => (byDate[r.utc_date] = r.total));
      let last = 0;
      return {
        name: c, type: "line", smooth: false, symbol: "none", showSymbol: false,
        lineStyle: { width: 2 }, itemStyle: { color: PALETTE[i % PALETTE.length] },
        emphasis: { focus: "series" },
        data: dates.map((d) => {
          if (byDate[d] !== undefined) last = byDate[d];
          return [d, last];
        }),
      };
    });
    yFmt = (v) => fmtMoneyFull(v, "CNY");
  } else {
    const byDate = {};
    rows.forEach((r) => (byDate[r.utc_date] = r.total));
    let last = 0;
    series = [{
      name: "累计 Tokens", type: "line", smooth: false, symbol: "none",
      lineStyle: { width: 2, color: cssVar("--chart-accent") }, itemStyle: { color: cssVar("--chart-accent") },
      areaStyle: { color: hexA(cssVar("--chart-accent"), 0.12) },
      data: dates.map((d) => {
        if (byDate[d] !== undefined) last = byDate[d];
        return [d, last];
      }),
    }];
    yFmt = (v) => fmtTokens(v);
  }

  charts.cumulative.setOption({
    tooltip: {
      trigger: "axis",
      backgroundColor: cssVar("--card"), borderColor: cssVar("--card-border"),
      textStyle: { color: cssVar("--text"), fontSize: 12 },
      valueFormatter: (v) => (isCost ? fmtMoneyFull(v, "CNY") : fmtTokensFull(v) + " tokens"),
    },
    legend: { show: false },   // 累计趋势不显示图例
    grid: { left: 64, right: 16, top: 30, bottom: 28 },
    xAxis: { type: "time", axisLine: { lineStyle: { color: cssVar("--card-border") } }, axisLabel: timeAxisLabel() },
    yAxis: { type: "value", axisLabel: { color: cssVar("--text-dim"), fontSize: 11, formatter: yFmt },
      splitLine: { lineStyle: { color: cssVar("--card-border") } } },
    series,
  }, true);
}

/* 热力图范围: 近 52 周, 起点对齐到所在周的周一(GitHub 风格, 首列补空白格) */
/* 热力图颜色梯度(GitHub 绿)与月份缩写 */
const HEAT_COLORS = {
  dark: ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"],
  light: ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"],
};
const heatColors = () => HEAT_COLORS[themeNow()];
const HM_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function renderHeatmap(rowsTokens, rowsCost) {
  /* 严格 GitHub 风格: 列 = 自然周(周日开头), 52 列
     - 顶部: 月份缩写(每月 1 号所在列的上方, 开头月份不重复标)
     - 左侧: Sun~Sat 全部星期标签
     - 最后一列 = 当前周, 只画到"今天"为止
     - 悬停即时 tooltip: 日期 + tokens 用量 + 金额 */
  const end = new Date();
  const byTokens = {};
  (rowsTokens || []).forEach((r) => {
    byTokens[r.utc_date] = { v: (byTokens[r.utc_date]?.v || 0) + r.cache_hit + r.cache_miss + r.output };
  });
  const byCost = {};
  (rowsCost || []).forEach((r) => {
    byCost[r.utc_date] = { v: (byCost[r.utc_date]?.v || 0) + r.total };
  });

  const COLS = 52, ROWS = 7;
  const fmt = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const metricIsCost = state.heatmapMetric === "cost";

  // 本周周日(行 0 = 周日 … 行 6 = 周六)
  const curSun = new Date(end);
  curSun.setDate(curSun.getDate() - end.getDay());

  let maxV = 1;
  const grid = [];
  const labelCols = new Map();   // 列号 → 月份标签文本(每月 1 号所在列)
  for (let c = 0; c < COLS; c++) {
    const sunday = new Date(curSun);
    sunday.setDate(sunday.getDate() - (COLS - 1 - c) * 7);
    const col = [];
    for (let row = 0; row < ROWS; row++) {
      const d = new Date(sunday);
      d.setDate(d.getDate() + row);
      const future = d > end;                 // 当前周今天之后的格子: 不显示
      const key = fmt(d);
      const tok = future ? 0 : (byTokens[key]?.v || 0);
      const cost = future ? 0 : (byCost[key]?.v || 0);
      const v = metricIsCost ? cost : tok;
      if (v > maxV) maxV = v;
      if (!future && d.getDate() === 1) labelCols.set(c, HM_MONTHS[d.getMonth()]);
      col.push({ key, v, tok, cost, future });
    }
    grid.push(col);
  }
  const colorFor = (v) => v ? heatColors()[Math.min(4, 1 + Math.floor((v / maxV) * 4))] : heatColors()[0];
  const tipFor = (cell) =>
    `${cell.key}\n用量 ${fmtTokensFull(cell.tok)} tokens\n金额 ${fmtMoneyFull(cell.cost, "CNY")}`;

  // 顶部月份标签(仅每月 1 号所在列)
  let monthsHtml = "";
  for (let c = 0; c < COLS; c++) {
    monthsHtml += labelCols.has(c)
      ? `<div class="hm-month">${labelCols.get(c)}</div>`
      : `<div class="hm-month hm-month-empty"></div>`;
  }

  // 左侧星期标签(Sun~Sat 全部)
  const dayLabels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const daysHtml = dayLabels.map((t) => `<div class="hm-day">${t}</div>`).join("");

  // 网格
  let gridHtml = "";
  for (let ci = 0; ci < COLS; ci++) {
    gridHtml += `<div class="hm-col">` +
      grid[ci].map((cell) =>
        cell.future
          ? `<div class="hm-cell hm-future"></div>`
          : `<div class="hm-cell" style="background:${colorFor(cell.v)}" data-tip="${tipFor(cell)}"></div>`
      ).join("") + `</div>`;
  }

  const box = $("chartHeatmap");
  box.innerHTML =
    `<div class="heatmap-wrap">
       <div class="hm-months">${monthsHtml}</div>
       <div class="hm-body">
         <div class="hm-days">${daysHtml}</div>
         <div class="heatmap-grid">${gridHtml}</div>
       </div>
     </div>`;

  // 即时 tooltip(原生 title 有延迟, 这里自绘跟随鼠标; fixed 定位保证坐标与视口一致)
  let tip = box.querySelector(".hm-tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "hm-tip";
    box.appendChild(tip);
  }
  box.onmousemove = (e) => {
    const cell = e.target.closest(".hm-cell:not(.hm-future)");
    if (!cell || !cell.dataset.tip) { tip.style.display = "none"; return; }
    tip.textContent = cell.dataset.tip;
    tip.style.display = "block";
    const w = 210;
    tip.style.left = (e.clientX + 14 + w > window.innerWidth ? e.clientX - 14 - w : e.clientX + 14) + "px";
    tip.style.top = (e.clientY - 8) + "px";
  };
  box.onmouseleave = () => { tip.style.display = "none"; };
}

/* ---------------- 分时(小时级)图表 ---------------- */

/* 24 小时图公共 tooltip: tokens 拆分(柱) + 可选请求次数行 + 费用。
   showRequestsRow=false(弹窗: 请求数在下图单独展示)时不输出请求数。 */
function hourlyTooltip(hours, showRequestsRow = true) {
  return {
    trigger: "axis",
    backgroundColor: cssVar("--card"), borderColor: cssVar("--card-border"),
    textStyle: { color: cssVar("--text"), fontSize: 12 },
    formatter: (params) => {
      const h = hours[params[0].data[0]] || {};
      const bars = params.filter((p) => p.seriesType === "bar");
      const total = bars.reduce((s, p) => s + (p.value[1] || 0), 0);
      const fmtRaw = (v) => (Number(v) || 0).toLocaleString("en-US");
      let html = `${String(params[0].data[0]).padStart(2, "0")}:00 - ` +
                 `${fmtTokensFull(total).replace(/\s/g, "")} tokens`;
      params.forEach((p) => {
        html += `<div class="tip-row"><span class="tip-name">${p.marker}${p.seriesName}</span>` +
                `<span class="tip-val">${fmtRaw(p.value[1] || 0)}</span></div>`;
      });
      if (showRequestsRow) {
        html += `<div class="tip-row"><span class="tip-name">请求次数</span>` +
                `<span class="tip-val">${fmtRaw(h.requests)}</span></div>`;
      }
      Object.entries(h.cost || {}).forEach(([c, v]) => {
        html += `<div class="tip-row"><span class="tip-name">费用 (${c})</span>` +
                `<span class="tip-val">${fmtMoneyFull(v, c)}</span></div>`;
      });
      return html;
    },
  };
}

/* 24 小时堆叠柱(tokens 拆分, 单轴), x 轴 0:00~23:00 */
function hourlyBarSeries(hours) {
  const parts = [["输入-缓存命中", "cache_hit"], ["输入-缓存未命中", "cache_miss"], ["输出", "output"]];
  return parts.map(([name, key]) => ({
    name, type: "bar", stack: "hourly", barMaxWidth: 22,
    itemStyle: { color: modelColor(name), borderRadius: [2, 2, 0, 0] },
    data: hours.map((h) => [h.hour, h[key]]),
  }));
}

/* 按模型/API Key 分组的 24 小时堆叠柱(每组一色) */
function groupedBarSeries(hours, groups) {
  return groups.map((g) => ({
    name: modelLabel(g), type: "bar", stack: "hourly", barMaxWidth: 22,
    itemStyle: { color: modelColor(modelLabel(g)), borderRadius: [2, 2, 0, 0] },
    data: hours.map((h) => [h.hour, h.values[g] || 0]),
  }));
}

/* 请求次数柱状图(独立小图, 位于 tokens 图下方)。
   groups 非空时按模型/API Key 分组堆叠(颜色与上图一致), 否则为总量单系列。 */
function requestsBarOption(hours, groups) {
  const grouped = groups && groups.length;
  const series = grouped
    ? groups.map((g) => ({
        name: modelLabel(g), type: "bar", stack: "req", barMaxWidth: 18,
        itemStyle: { color: modelColor(modelLabel(g)), borderRadius: [2, 2, 0, 0] },
        data: hours.map((h) => [h.hour, (h.reqValues && h.reqValues[g]) || 0]),
      }))
    : [{
        name: "请求次数", type: "bar", barMaxWidth: 18,
        itemStyle: { color: cssVar("--chart-orange"), borderRadius: [2, 2, 0, 0] },
        data: hours.map((h) => [h.hour, h.requests]),
      }];
  return {
    tooltip: {
      trigger: "axis",
      backgroundColor: cssVar("--card"), borderColor: cssVar("--card-border"),
      textStyle: { color: cssVar("--text"), fontSize: 12 },
      formatter: (params) => {
        let html = `${String(params[0].value[0]).padStart(2, "0")}:00`;
        params.forEach((p) => {
          html += `<div class="tip-row"><span class="tip-name">${p.marker}${p.seriesName}</span>` +
                  `<span class="tip-val">${(Number(p.value[1]) || 0).toLocaleString("en-US")} 次</span></div>`;
        });
        if (params.length > 1) {
          const total = params.reduce((s, p) => s + (Number(p.value[1]) || 0), 0);
          html += `<div class="tip-row"><span class="tip-name">合计</span>` +
                  `<span class="tip-val">${total.toLocaleString("en-US")} 次</span></div>`;
        }
        return html;
      },
    },
    grid: { left: 56, right: 16, top: 26, bottom: 24 },
    xAxis: {
      type: "category", data: Array.from({ length: 24 }, (_, h) => `${String(h).padStart(2, "0")}:00`),
      axisLine: { lineStyle: { color: cssVar("--card-border") } },
      axisLabel: { color: cssVar("--text-dim"), fontSize: 11, interval: 1 },
    },
    yAxis: {
      type: "value", axisLabel: { color: cssVar("--text-dim"), fontSize: 11,
        formatter: (v) => (Number(v) || 0).toLocaleString("en-US") },
      splitLine: { lineStyle: { color: cssVar("--card-border") } },
    },
    series,
  };
}

/* 费用柱状图(独立小图, 位于请求数图下方)。
   groups 非空时按模型/API Key 分组堆叠(颜色与上图一致), 否则按币种单/多系列。 */
function costBarOption(hours, groups) {
  const grouped = groups && groups.length;
  const series = grouped
    ? groups.map((g) => ({
        name: modelLabel(g), type: "bar", stack: "cost", barMaxWidth: 18,
        itemStyle: { color: modelColor(modelLabel(g)), borderRadius: [2, 2, 0, 0] },
        data: hours.map((h) => [
          h.hour,
          Object.values((h.costValues && h.costValues[g]) || {}).reduce((s, v) => s + (v || 0), 0),
        ]),
      }))
    : [...new Set(hours.flatMap((h) => Object.keys(h.cost || {})))].map((c) => ({
        name: c, type: "bar", stack: "cost", barMaxWidth: 18,
        itemStyle: { color: cssVar("--chart-gold"), borderRadius: [2, 2, 0, 0] },
        data: hours.map((h) => [h.hour, h.cost[c] || 0]),
      }));
  return {
    tooltip: {
      trigger: "axis",
      backgroundColor: cssVar("--card"), borderColor: cssVar("--card-border"),
      textStyle: { color: cssVar("--text"), fontSize: 12 },
      formatter: (params) => {
        const h = hours[params[0].value[0]] || {};
        const cur = Object.keys(h.cost || {})[0] || "CNY";
        let html = `${String(params[0].value[0]).padStart(2, "0")}:00`;
        params.forEach((p) => {
          html += `<div class="tip-row"><span class="tip-name">${p.marker}${p.seriesName}</span>` +
                  `<span class="tip-val">${fmtMoneyFull(p.value[1] || 0, cur)}</span></div>`;
        });
        if (params.length > 1) {
          const total = params.reduce((s, p) => s + (Number(p.value[1]) || 0), 0);
          html += `<div class="tip-row"><span class="tip-name">合计</span>` +
                  `<span class="tip-val">${fmtMoneyFull(total, cur)}</span></div>`;
        }
        return html;
      },
    },
    grid: { left: 56, right: 16, top: 26, bottom: 24 },
    xAxis: {
      type: "category", data: Array.from({ length: 24 }, (_, h) => `${String(h).padStart(2, "0")}:00`),
      axisLine: { lineStyle: { color: cssVar("--card-border") } },
      axisLabel: { color: cssVar("--text-dim"), fontSize: 11, interval: 1 },
    },
    yAxis: {
      type: "value", axisLabel: { color: cssVar("--text-dim"), fontSize: 11,
        formatter: (v) => fmtMoney(v, "CNY") },
      splitLine: { lineStyle: { color: cssVar("--card-border") } },
    },
    series,
  };
}

function hourlyAxisOption(withRequestsAxis = true) {
  return {
    grid: { left: 56, right: withRequestsAxis ? 52 : 16, top: 34, bottom: 28 },
    xAxis: {
      type: "category", data: Array.from({ length: 24 }, (_, h) => `${String(h).padStart(2, "0")}:00`),
      axisLine: { lineStyle: { color: cssVar("--card-border") } },
      axisLabel: { color: cssVar("--text-dim"), fontSize: 11, interval: 1 },
    },
    yAxis: [
      { // 左轴: tokens
        type: "value", axisLabel: { color: cssVar("--text-dim"), fontSize: 11, formatter: (v) => fmtTokens(v) },
        splitLine: { lineStyle: { color: cssVar("--card-border") } },
      },
      ...(withRequestsAxis ? [{
        // 右轴: 请求次数
        type: "value", name: "请求次数",
        nameTextStyle: { color: cssVar("--text-faint"), fontSize: 11 },
        axisLabel: { color: cssVar("--text-dim"), fontSize: 11, formatter: (v) => (Number(v) || 0).toLocaleString("en-US") },
        splitLine: { show: false },
      }] : []),
    ],
    legend: { show: false },
  };
}

/* 分时面板: 所选范围内所有日的同一小时用量加总 */
function renderHourlyAggregate(agg) {
  const hours = agg.hours;
  const hasData = hours.some((h) => h.cache_hit + h.cache_miss + h.output + h.requests > 0);
  if (!hasData) {
    charts.hourly.setOption(emptyOption("暂无分时数据 — 同步后平台仅保留当天与昨天的分时用量"), true);
    $("hourlyRange").textContent = "—";
    return;
  }
  $("hourlyRange").textContent = `${agg.start} ~ ${agg.end} 同时段加总`;
  charts.hourly.setOption({
    tooltip: hourlyTooltip(hours, true),      // 汇总面板: 单轴, tooltip 显示请求行
    ...hourlyAxisOption(false),
    series: hourlyBarSeries(hours),
  }, true);
}

async function loadHourlyAggregate() {
  try {
    const agg = await api(`/api/stats/hourly/aggregate?days=${state.hourlyDays}`);
    renderHourlyAggregate(agg);
  } catch (e) { /* 忽略 */ }
}

/* 弹窗内图表: 首次打开时 init(容器需已可见), 复用实例。
   返回 [tokens 堆叠柱图, 请求次数柱状图, 费用柱状图]。 */
function hourlyDetailCharts() {
  return ["chartHourlyDetail", "chartHourlyDetailReq", "chartHourlyDetailCost"].map((id) => {
    const el = document.getElementById(id);
    let chart = echarts.getInstanceByDom(el);
    if (!chart) chart = echarts.init(el);
    chart.resize();
    return chart;
  });
}

/* 点击每日走势柱状图 → 弹窗显示当日 24 小时分时明细(按当前视图分组) */
async function openHourlyDetail(dateStr) {
  state.hourlyDetailDate = dateStr;
  $("hourlyModalTitle").textContent = `${fmtDate(dateStr)} 分时用量`;
  $("hourlyModalSub").textContent = "正在加载…";
  $("hourlyModal").classList.remove("hidden");
  try {
    const detail = await api(`/api/stats/hourly/detail?date=${dateStr}&group=${state.hourlyDetailView}`);
    renderHourlyDetail(detail);
  } catch (e) {
    $("hourlyModalSub").textContent = e.message;
  }
}

function renderHourlyDetail(detail) {
  if (detail.group && detail.group !== "type") { renderGroupedDetail(detail); return; }
  const hours = detail.hours;
  const totalT = hours.reduce((s, h) => s + h.cache_hit + h.cache_miss + h.output, 0);
  const totalR = hours.reduce((s, h) => s + h.requests, 0);
  const costs = {};
  hours.forEach((h) => Object.entries(h.cost || {}).forEach(([c, v]) => (costs[c] = (costs[c] || 0) + v)));
  const costStr = Object.entries(costs).map(([c, v]) => fmtMoneyFull(v, c)).join(" + ") || "¥0.00";

  const [chartT, chartR, chartC] = hourlyDetailCharts();
  if (totalT === 0 && totalR === 0) {
    $("hourlyModalSub").textContent = "该日无分时数据 — 平台仅保留当天与昨天的分时用量";
    chartT.setOption(emptyOption("无分时数据"), true);
    chartR.setOption(emptyOption(""), true);
    chartC.setOption(emptyOption(""), true);
    return;
  }
  $("hourlyModalSub").innerHTML =
    `总用量 ${fmtTokensFull(totalT)} tokens · 请求 ${totalR.toLocaleString("en-US")} 次 · 费用 ${costStr}`;
  chartT.setOption({
    tooltip: hourlyTooltip(hours, false),   // 请求/费用在下图单独展示, 上图 tooltip 不再重复
    ...hourlyAxisOption(false),
    series: hourlyBarSeries(hours),
  }, true);
  chartR.setOption(requestsBarOption(hours), true);
  chartC.setOption(costBarOption(hours), true);
}

/* 按模型/API Key 视图: 上图每组一根堆叠柱, 下图请求/费用柱状图(同步分组) */
function renderGroupedDetail(detail) {
  const hours = detail.hours;
  const groups = detail.groups || [];
  const totalT = hours.reduce((s, h) => s + Object.values(h.values || {}).reduce((a, v) => a + (v || 0), 0), 0);
  const totalR = hours.reduce((s, h) => s + h.requests, 0);
  const costs = {};
  hours.forEach((h) => Object.entries(h.cost || {}).forEach(([c, v]) => (costs[c] = (costs[c] || 0) + v)));
  const costStr = Object.entries(costs).map(([c, v]) => fmtMoneyFull(v, c)).join(" + ") || "¥0.00";

  const [chartT, chartR, chartC] = hourlyDetailCharts();
  if (totalT === 0 && totalR === 0) {
    $("hourlyModalSub").textContent = "该日无分时数据 — 平台仅保留当天与昨天的分时用量";
    chartT.setOption(emptyOption("无分时数据"), true);
    chartR.setOption(emptyOption(""), true);
    chartC.setOption(emptyOption(""), true);
    return;
  }
  $("hourlyModalSub").innerHTML =
    `总用量 ${fmtTokensFull(totalT)} tokens · 请求 ${totalR.toLocaleString("en-US")} 次 · 费用 ${costStr}`;
  chartT.setOption({
    tooltip: hourlyTooltip(hours, false),
    ...hourlyAxisOption(false),
    series: groupedBarSeries(hours, groups),
  }, true);
  chartR.setOption(requestsBarOption(hours, groups), true);   // 请求柱状图同样按模型/Key 拆分
  chartC.setOption(costBarOption(hours, groups), true);       // 费用柱状图同样按模型/Key 拆分
}

/* ---------------- 统计卡片 ---------------- */

function renderCards(s) {
  $("cTotalTokens").textContent = fmtTokens(s.total.tokens);
  $("cTotalTokensSub").innerHTML =
    `缓存命中 ${fmtTokensFull(s.total.cache_hit)} · 未命中 ${fmtTokensFull(s.total.cache_miss)}<br>输出 ${fmtTokensFull(s.total.output)} · 请求 ${s.total.requests.toLocaleString("en-US")} 次`;

  const costStr = Object.entries(s.total.cost).map(([c, v]) => fmtMoneyFull(v, c)).join(" + ") || "¥0.00";
  $("cTotalCost").textContent = costStr;
  let costSub = Object.keys(s.total.cost).length
    ? `按币种汇总 · 最近同步 ${fmtLocal(s.dates.last_sync_at)}`
    : "暂无费用数据(费用接口需登录后同步)";
  if (s.balance && s.balance.wallets && s.balance.wallets.length) {
    const bal = s.balance.wallets.map((w) => {
      const base = `${fmtMoneyFull(w.total, w.currency)} 余额`;
      return w.bonus > 0 ? `${base}(含赠送 ${fmtMoneyFull(w.bonus, w.currency)})` : base;
    }).join(" / ");
    costSub += `<br><span class="ok">${bal}</span>`;
  }
  $("cTotalCostSub").innerHTML = costSub;

  $("cMonthTokens").textContent = fmtTokens(s.month.tokens);
  const monthCost = Object.entries(s.month.cost).map(([c, v]) => fmtMoneyFull(v, c)).join(" + ") || "¥0.00";
  $("cMonthSub").innerHTML = `本月花费 ${monthCost}<br>请求 ${s.month.requests.toLocaleString("en-US")} 次`;

  $("cTodayTokens").textContent = fmtTokens(s.today.tokens);
  $("cTodaySub").innerHTML =
    `今日请求 ${s.today.requests.toLocaleString("en-US")} 次<br>按 GMT+8 本地日统计`;

  const range = s.dates.earliest ? `${s.dates.earliest} ~ ${s.dates.latest}` : "暂无数据";
  $("dataRange").textContent = `数据范围: ${range} (GMT+8 日)`;
  $("lastSync").textContent = s.dates.last_sync_at ? `最近同步 ${fmtLocal(s.dates.last_sync_at)}` : "尚未同步";
}

function renderLogs(logs) {
  const box = $("syncLog");
  if (!logs.length) { box.innerHTML = `<div class="empty">暂无同步记录</div>`; return; }
  const cls = { ok: "ok", error: "error", running: "running" };
  box.innerHTML = logs.map((l) => `
    <div class="log-row ${cls[l.status] || ""}">
      <span class="time">${fmtLocal(l.started_at)}</span>
      <span class="msg">${(l.message || (l.status === "running" ? "同步中..." : "")).replace(/</g, "&lt;")}</span>
    </div>`).join("");
}

/* ---------------- 数据加载 ---------------- */

async function loadSummary() {
  try {
    const s = await api("/api/stats/summary");
    renderCards(s);
  } catch (e) { toast(e.message, true); }
}

async function loadLogs() {
  try {
    const logs = await api("/api/sync/logs?limit=15");
    renderLogs(logs);
  } catch (e) { /* 忽略 */ }
}

async function loadCharts() {
  state.dailyLegendVisible = null;    // 切换视图/范围时重置图例选择
  state.modelsLegendVisible = null;
  try {
    const group = state.dailyView === "key" ? "key" : "model";
    const daily = await api(`/api/stats/daily?days=${state.dailyDays}&group=${group}`);
    renderDaily(daily.rows, daily.start, daily.end);
  } catch (e) { toast(e.message, true); }

  try {
    const models = await api("/api/stats/models");
    renderModels(models);
  } catch (e) { /* 忽略 */ }

  try {
    const cum = await api(`/api/stats/cumulative?metric=${state.cumulativeMetric}`);
    renderCumulative(cum);
  } catch (e) { /* 忽略 */ }

  await loadHeatmap();
  loadHourlyAggregate();
}

async function loadHeatmap() {
  try {
    const end = new Date();
    const start = new Date(end);
    start.setDate(start.getDate() - 363);   // 近 52 周数据
    const f = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const q = `start=${f(start)}&end=${f(end)}`;
    // 同时取 tokens 与费用, tooltip 两者都展示; 着色按当前选择的指标
    const [t, c] = await Promise.all([
      api(`/api/stats/heatmap?${q}&metric=tokens`),
      api(`/api/stats/heatmap?${q}&metric=cost`),
    ]);
    renderHeatmap(t.rows, c.rows);
  } catch (e) { /* 忽略 */ }
}

function reloadAll() {
  loadSummary();
  loadCharts();
  loadLogs();
}

/* ---------------- 认证状态与横幅 ---------------- */

async function loadAuth() {
  try {
    state.auth = await api("/api/auth/status");
    const badge = $("modeBadge");
    if (state.auth.mode === "mock") {
      badge.textContent = "演示模式 (mock)";
      badge.className = "badge mock";
      badge.classList.remove("hidden");
    } else {
      // 正常运行(实时模式)不显示模式徽章
      badge.classList.add("hidden");
    }
    // 登录/退出按钮互斥: 已配置只显退出登录, 未配置只显登录(mock 模式都不显)
    const live = state.auth.mode !== "mock";
    $("btnLogout").classList.toggle("hidden", !live || !state.auth.configured);
    $("btnLogin").classList.toggle("hidden", !live || state.auth.configured);
    if (state.auth.mode !== "mock" && !state.auth.configured) {
      showBanner("尚未登录 DeepSeek 平台 — 首次使用需要登录才能拉取全部历史用量", false);
    } else {
      hideBanner();
    }
  } catch (e) { /* 服务未就绪 */ }
}

function showBanner(text, isError) {
  const b = $("banner");
  b.classList.remove("hidden");
  b.classList.toggle("error", !!isError);
  $("bannerText").textContent = text;
  $("bannerBtn").textContent = "去登录";
}
function hideBanner() { $("banner").classList.add("hidden"); }

/* ---------------- 同步流程 ---------------- */

async function doSync() {
  const btn = $("btnSync");
  btn.disabled = true;
  btn.querySelector(".sync-icon").classList.add("spinning");
  try {
    const res = await api("/api/sync", { method: "POST" });
    toast(res.message || "同步已开始");
    pollSyncStatus();
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
    btn.querySelector(".sync-icon").classList.remove("spinning");
    if (e.message.includes("登录")) {
      showBanner(e.message + " — 请重新登录", true);
    }
  }
}

function pollSyncStatus() {
  const tick = async () => {
    const st = await api("/api/sync/status");
    if (st.running) {
      loadLogs();
      setTimeout(tick, 1200);
      return;
    }
    const btn = $("btnSync");
    btn.disabled = false;
    btn.querySelector(".sync-icon").classList.remove("spinning");
    if (st.error) {
      if (st.error.account_changed) {
        toast(st.error.message, true);
        $("accountModal").classList.remove("hidden");   // 账号变更: 弹窗让用户决策
      } else {
        toast(st.error.message, true);
        showBanner(st.error.message, true);
        if (st.error.expired) $("bannerBtn").textContent = "重新登录";
      }
    } else if (st.last) {
      toast("同步完成: " + st.last.message);
      hideBanner();
      reloadAll();
    }
    loadLogs();
  };
  tick();
}

/* 账号变更处理: 清空本地数据后用当前账号重新同步 */
async function accountClearSync() {
  $("accountModal").classList.add("hidden");
  try {
    await api("/api/data/clear", { method: "POST" });
    toast("旧账号数据已清空, 开始同步新账号...");
    doSync();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- 登录流程 ---------------- */

async function openLogin() {
  try {
    const res = await api("/api/login/start", { method: "POST" });
    if (res.manual) openTokenModal();
    else toast(res.message || "已打开登录窗口, 登录完成后将自动关闭");
  } catch (e) { toast(e.message, true); }
}

async function doLogout() {
  try {
    const res = await api("/api/logout", { method: "POST" });
    toast(res.message || "已退出登录");
    hideBanner();
    await loadAuth();      // 重新显示登录横幅、隐藏退出按钮
    loadSummary();         // 余额等缓存信息随登出清除
  } catch (e) { toast(e.message, true); }
}

/* ---------------- 清空数据 ---------------- */

async function openClearModal() {
  try {
    const s = await api("/api/stats/summary");
    const rows = s.total.tokens > 0 || Object.keys(s.total.cost).length > 0
      ? `${fmtTokensFull(s.total.tokens)} tokens / 费用 ${Object.entries(s.total.cost).map(([c, v]) => fmtMoneyFull(v, c)).join("+")}`
      : "暂无数据";
    $("clearCount").textContent = rows;
  } catch (e) { $("clearCount").textContent = "—"; }
  $("clearModal").classList.remove("hidden");
}
function closeClearModal() { $("clearModal").classList.add("hidden"); }

async function confirmClearData() {
  closeClearModal();
  try {
    const res = await api("/api/data/clear", { method: "POST" });
    toast(res.message || "已清空全部数据");
    reloadAll();       // 图表与卡片回到空状态
    loadAuth();        // 刷新「最近同步」等状态
  } catch (e) { toast(e.message, true); }
}

function openTokenModal() {
  $("tokenModal").classList.remove("hidden");
  $("tokenInput").value = "";
  $("tokenInput").focus();
}
function closeTokenModal() { $("tokenModal").classList.add("hidden"); }

async function saveToken() {
  const token = $("tokenInput").value.trim();
  if (!token) { toast("请粘贴登录凭证", true); return; }
  try {
    await api("/api/token", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }) });
    closeTokenModal();
    toast("登录凭证已保存");
    hideBanner();
    loadAuth();
    loadSummary();  // 立即刷新余额展示
  } catch (e) { toast(e.message, true); }
}

/* ---------------- 数据备份(导出/导入) ---------------- */

/* 导出: 优先弹「另存为」对话框(WebView2/Chrome/Edge), 可自选文件名与位置;
   环境不支持时回退为浏览器默认下载。 */
async function exportData() {
  try {
    const resp = await fetch("/api/data/export");
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || `导出失败 (${resp.status})`);
    }
    const blob = await resp.blob();
    const defaultName = `deepseek-usage-backup-${new Date().toISOString().slice(0, 10)}.json`;

    if (window.showSaveFilePicker) {
      try {
        const handle = await window.showSaveFilePicker({
          suggestedName: defaultName,
          types: [{ description: "JSON 备份", accept: { "application/json": [".json"] } }],
        });
        const writable = await handle.createWritable();
        await writable.write(blob);
        await writable.close();
        toast("已导出数据备份");
        return;
      } catch (e) {
        if (e && e.name === "AbortError") return;   // 用户取消保存
        // 其他异常(权限/策略等)回退普通下载
      }
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = defaultName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    toast("已导出数据备份");
  } catch (e) { toast(e.message, true); }
}

let pendingImport = null;   // 等待确认的导入 payload

async function onImportFileChosen() {
  const f = $("importFile").files[0];
  $("importFile").value = "";
  if (!f) return;
  try {
    const payload = JSON.parse(await f.text());
    if (!payload || payload.app !== "deepseek-usage-stats") {
      throw new Error("不是本工具导出的备份文件");
    }
    pendingImport = payload;
    const n = (a) => (Array.isArray(a) ? a.length : 0);
    $("importInfo").textContent =
      `文件: ${f.name} · 将导入用量 ${n(payload.amount_daily)} 行, ` +
      `费用 ${n(payload.cost_daily)} 行, 分时 ${n(payload.hourly_usage) + n(payload.hourly_cost)} 行`;
    $("importModal").classList.remove("hidden");
  } catch (e) {
    pendingImport = null;
    toast("文件读取失败: " + e.message, true);
  }
}

function closeImportModal() {
  $("importModal").classList.add("hidden");
  pendingImport = null;
}

async function confirmImport() {
  $("importModal").classList.add("hidden");
  if (!pendingImport) return;
  const payload = pendingImport;
  pendingImport = null;
  try {
    const res = await api("/api/data/import", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    toast(res.message);
    reloadAll();
    loadAuth();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- 交互绑定 ---------------- */

$("btnSync").addEventListener("click", doSync);
$("btnTheme").addEventListener("click", () => applyTheme(themeNow() === "dark" ? "light" : "dark"));
$("btnLogin").addEventListener("click", openLogin);
$("btnLogout").addEventListener("click", doLogout);
$("btnClearData").addEventListener("click", openClearModal);
$("clearCancel").addEventListener("click", closeClearModal);
$("clearConfirm").addEventListener("click", confirmClearData);
$("btnExport").addEventListener("click", exportData);
$("btnImport").addEventListener("click", () => $("importFile").click());
$("importFile").addEventListener("change", onImportFileChosen);
$("importCancel").addEventListener("click", closeImportModal);
$("importConfirm").addEventListener("click", confirmImport);
$("accountCancel").addEventListener("click", () => $("accountModal").classList.add("hidden"));
$("accountClearSync").addEventListener("click", accountClearSync);
$("bannerBtn").addEventListener("click", openLogin);
$("tokenCancel").addEventListener("click", closeTokenModal);
$("tokenSave").addEventListener("click", saveToken);

$("dailyLegend").addEventListener("click", onDailyLegendClick);
$("modelsLegend").addEventListener("click", onModelsLegendClick);
$("dailyViewSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.dailyView = b.dataset.view;
  document.querySelectorAll("#dailyViewSeg button").forEach((x) => x.classList.toggle("active", x === b));
  loadCharts();
});
$("dailyRangeSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.dailyDays = Number(b.dataset.days);
  document.querySelectorAll("#dailyRangeSeg button").forEach((x) => x.classList.toggle("active", x === b));
  loadCharts();
});
$("modelsMetricSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.modelsMetric = b.dataset.metric;
  document.querySelectorAll("#modelsMetricSeg button").forEach((x) => x.classList.toggle("active", x === b));
  loadCharts();
});
$("cumulativeMetricSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.cumulativeMetric = b.dataset.metric;
  document.querySelectorAll("#cumulativeMetricSeg button").forEach((x) => x.classList.toggle("active", x === b));
  loadCharts();
});
$("heatmapMetricSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.heatmapMetric = b.dataset.metric;
  document.querySelectorAll("#heatmapMetricSeg button").forEach((x) => x.classList.toggle("active", x === b));
  loadHeatmap();
});
$("hourlyRangeSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.hourlyDays = Number(b.dataset.days);
  document.querySelectorAll("#hourlyRangeSeg button").forEach((x) => x.classList.toggle("active", x === b));
  loadHourlyAggregate();
});
/* 分时弹窗视图切换(按计费类型/按模型/按 API Key): 重新加载当前日期的明细 */
$("hourlyDetailViewSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.hourlyDetailView = b.dataset.view;
  document.querySelectorAll("#hourlyDetailViewSeg button").forEach((x) => x.classList.toggle("active", x === b));
  if (state.hourlyDetailDate) openHourlyDetail(state.hourlyDetailDate);
});
/* 分时详情弹窗: 点击遮罩空白处关闭(点弹窗内部不关) */
$("hourlyModal").addEventListener("click", (e) => {
  if (e.target === $("hourlyModal")) $("hourlyModal").classList.add("hidden");
});

/* 点击每日用量走势的柱子 → 弹窗显示该日 24 小时分时明细(time 轴 value 为日期) */
charts.daily.on("click", (params) => {
  if (!params || !params.value) return;
  const d = new Date(params.value[0]);
  if (isNaN(d.getTime())) return;
  const dateStr = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  openHourlyDetail(dateStr);
});

/* ---------------- 启动 ---------------- */

(async function init() {
  // 顶部图标: 未提供 web/logo.png 时隐藏破图, 显示渐变占位块
  const logoImg = document.querySelector("#appLogo img");
  if (logoImg) {
    logoImg.onerror = () => { logoImg.style.display = "none"; };
  }
  // 主题按钮图标: 显示"将切换到的"主题
  $("btnTheme").textContent = themeNow() === "dark" ? "☀" : "☾";
  await loadAuth();
  reloadAll();
  // 轮询认证状态(等待登录窗口完成 / 退出登录后刷新按钮与横幅)
  setInterval(async () => {
    const before = state.auth.configured;
    await loadAuth();
    if (state.auth.configured && !before) {
      toast("登录成功, 点击「立即同步」拉取数据");
    }
  }, 4000);
})();
