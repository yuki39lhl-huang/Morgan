"use strict";

const byId = (id) => document.getElementById(id);
const priceFormat = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 8 });
const pnlFormat = new Intl.NumberFormat("zh-CN", {
  minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: "exceptZero",
});
const reasons = {
  rsi_mid: "RSI 回到中轨",
  rsi_timeout: "持仓到期",
  rsi_time: "持仓到期",
};
const dayMs = 86400000;
const dateTime = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});
const actions = { enter_now: "立即进", wait_one_bar: "等一根", skip: "跳过" };
const panelVersions = new Map();
let snapshot;
let period = 7;
let curveIndex = null;
let curveGeometry;

function numberText(value, format = priceFormat) {
  return Number.isFinite(value) ? format.format(value) : "—";
}

function pnlText(value) {
  return Number.isFinite(value) ? numberText(value, pnlFormat) + " USDT" : "—";
}

function setPnl(node, value) {
  const text = pnlText(value);
  if (node.textContent !== text) {
    const hadValue = node.dataset.pnlReady === "yes";
    node.textContent = text;
    if (hadValue) playMotion(node, [{ opacity: .55, transform: "translateY(2px)" }, { opacity: 1, transform: "none" }], 240);
  }
  node.dataset.pnlReady = "yes";
  node.classList.toggle("positive", value > 0);
  node.classList.toggle("negative", value < 0);
}

function playMotion(node, frames, duration) {
  if (!window.matchMedia || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  node.getAnimations?.().forEach(animation => animation.cancel());
  node.animate?.(frames, { duration, easing: "cubic-bezier(.2,.7,.2,1)" });
}

function moveIndicator(container, target) {
  if (!container || !target || !target.offsetWidth) return;
  let indicator = container.querySelector(".pill-indicator");
  if (!indicator) {
    indicator = element("span", "", "pill-indicator");
    indicator.setAttribute("aria-hidden", "true");
    container.prepend(indicator);
    container.classList.add("has-indicator");
  }
  indicator.style.width = target.offsetWidth + "px";
  indicator.style.transform = "translateX(" + target.offsetLeft + "px)";
}

function wireSlider(track, getControls, selected, scrollEdges = false) {
  let drag, suppressClick = false;
  function updateDrag(continueScroll = true) {
    if (!drag?.active) return;
    if (scrollEdges && continueScroll) {
      const rect = track.getBoundingClientRect();
      const edge = 28;
      if (drag.clientX < rect.left + edge) track.scrollLeft -= 9;
      else if (drag.clientX > rect.right - edge) track.scrollLeft += 9;
    }
    const controls = getControls();
    if (controls.length) {
      const distance = node => {
        const rect = node.getBoundingClientRect();
        return Math.abs(drag.clientX - rect.left - rect.width / 2);
      };
      drag.target = controls.reduce((nearest, item) => distance(item) < distance(nearest) ? item : nearest);
      moveIndicator(track, drag.target);
    }
    if (scrollEdges && continueScroll) drag.frame = requestAnimationFrame(() => updateDrag());
  }
  track.addEventListener("pointerdown", event => {
    if (!event.isPrimary || event.button !== 0) return;
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY, active: false };
    suppressClick = false;
  });
  track.addEventListener("pointermove", event => {
    if (!drag || drag.id !== event.pointerId) return;
    const distance = Math.abs(event.clientX - drag.x);
    if (!drag.active && (distance < 8 || distance < Math.abs(event.clientY - drag.y))) return;
    drag.clientX = event.clientX;
    if (!drag.active) {
      track.setPointerCapture(event.pointerId);
      track.dataset.sliding = "";
      drag.active = true;
      updateDrag();
    } else if (!scrollEdges) updateDrag();
  });
  const endDrag = (event, commit) => {
    if (!drag || drag.id !== event.pointerId) return;
    if (commit && drag.active && Number.isFinite(event.clientX)) {
      drag.clientX = event.clientX;
      updateDrag(false);
    }
    const target = drag.active && drag.target;
    if (drag.frame) cancelAnimationFrame(drag.frame);
    drag = null;
    delete track.dataset.sliding;
    if (!target) return;
    if (commit) {
      target.click();
      suppressClick = true;
    } else moveIndicator(track, getControls().find(selected));
  };
  track.addEventListener("pointerup", event => endDrag(event, true));
  track.addEventListener("pointercancel", event => endDrag(event, false));
  track.addEventListener("lostpointercapture", event => endDrag(event, false));
  track.addEventListener("click", event => {
    if (!suppressClick || !event.detail) return;
    suppressClick = false;
    event.preventDefault();
    event.stopPropagation();
  }, true);
  track.addEventListener("keydown", event => {
    const controls = getControls();
    const index = controls.indexOf(event.target);
    if (index < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const last = controls.length - 1;
    const next = {ArrowLeft: Math.max(0, index - 1), ArrowRight: Math.min(last, index + 1), Home: 0, End: last}[event.key];
    controls[next].click();
    controls[next].focus({ preventScroll: true });
  });
}

const dropdowns = new WeakMap();

function enhanceSelect(select) {
  const wrap = element("div", "", "dropdown");
  const trigger = element("button", "", "dropdown-trigger");
  const caption = element("span");
  const list = element("div", "", "dropdown-menu");
  const label = select.labels[0]?.textContent.trim() || "";
  trigger.type = "button";
  trigger.append(caption, element("span", "", "dropdown-chevron"));
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  list.id = select.id + "-menu";
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", label);
  trigger.setAttribute("aria-controls", list.id);
  list.hidden = true;
  select.before(wrap);
  wrap.append(select, trigger, list);
  select.hidden = true;
  let options = [], signature;

  function close(restoreFocus = false) {
    list.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) trigger.focus();
  }

  function sync() {
    const choices = Array.from(select.options);
    const next = JSON.stringify(choices.map(o => [o.value, o.text, o.disabled]));
    if (signature !== next) {
      close(list.contains(document.activeElement));
      signature = next;
      options = choices.map(choice => {
        const option = element("button", choice.text, "dropdown-option");
        option.type = "button";
        option.disabled = choice.disabled;
        option.tabIndex = -1;
        option.setAttribute("role", "option");
        option.addEventListener("click", () => {
          select.value = choice.value;
          close(true);
          sync();
          select.dispatchEvent(new Event("change", { bubbles: true }));
        });
        return option;
      });
      list.replaceChildren(...options);
    }
    caption.textContent = choices[select.selectedIndex]?.text || "—";
    trigger.setAttribute("aria-label", label + " " + caption.textContent);
    trigger.disabled = select.disabled || !options.some(option => !option.disabled);
    options.forEach((option, index) => option.setAttribute("aria-selected", String(index === select.selectedIndex)));
  }

  function open() {
    list.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    (options[select.selectedIndex] || options.find(option => !option.disabled))?.focus();
  }

  trigger.addEventListener("click", () => list.hidden ? open() : close(true));
  document.addEventListener("pointerdown", event => { if (!wrap.contains(event.target)) close(); });
  wrap.addEventListener("focusout", event => { if (!wrap.contains(event.relatedTarget)) close(); });
  wrap.addEventListener("keydown", event => {
    if (event.key === "Escape" && !list.hidden) {
      event.preventDefault();
      close(true);
      return;
    }
    if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    if (list.hidden) {
      open();
      return;
    }
    const available = options.filter(option => !option.disabled);
    const index = available.indexOf(document.activeElement);
    const last = available.length - 1;
    const next = { ArrowUp: Math.max(0, index - 1), ArrowDown: Math.min(last, index + 1), Home: 0, End: last }[event.key];
    available[next]?.focus();
  });
  dropdowns.set(select, sync);
  sync();
}

function syncDropdown(select) {
  dropdowns.get(select)?.();
}

function timeText(value) {
  return value ? value.slice(0, 19).replace("T", " ") : "—";
}

function cell(row, value) {
  const node = document.createElement("td");
  node.textContent = value;
  row.append(node);
  return node;
}

function sideCell(row, side) {
  const badge = document.createElement("span");
  badge.className = side === "LONG" ? "side long" : "side short";
  badge.textContent = side === "LONG" ? "多" : side === "SHORT" ? "空" : "—";
  cell(row, "").append(badge);
}

function table(id, data, width, placeholder, render) {
  const rows = document.createDocumentFragment();
  for (const item of data) {
    const row = document.createElement("tr");
    render(row, item);
    rows.append(row);
  }
  if (!data.length) {
    const row = document.createElement("tr");
    const placeholderCell = cell(row, placeholder);
    placeholderCell.colSpan = width;
    placeholderCell.className = "empty";
    rows.append(row);
  }
  byId(id).replaceChildren(rows);
}

function status(text, live = false) {
  const node = byId("status");
  if (node.textContent !== text) node.textContent = text;
  node.classList.toggle("live", live);
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function canvasContext(canvas) {
  const { width, height } = canvas.getBoundingClientRect();
  const scale = window.devicePixelRatio || 1;
  const pixelWidth = Math.round(width * scale);
  const pixelHeight = Math.round(height * scale);
  if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
  if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
  const ctx = canvas.getContext("2d");
  if (ctx) {
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.clearRect(0, 0, width, height);
  }
  return [ctx, width, height];
}

function drawCurve() {
  if (!snapshot) return;
  const canvas = byId("curve");
  const [ctx, width, height] = canvasContext(canvas);
  if (!ctx || width < 80) return;
  const records = new Map(snapshot.ledger.map((row) => [row.day, row]));
  const today = Date.parse(snapshot.updated.slice(0, 10) + "T00:00:00Z");
  let total = 0;
  let trades = 0;
  const points = Array.from({ length: period }, (_, i) => {
    const day = new Date(today - (period - 1 - i) * dayMs).toISOString().slice(0, 10);
    const record = records.get(day);
    total += record?.pnl ?? 0;
    trades += record?.trades ?? 0;
    return { day, total, pnl: record?.pnl ?? 0, count: record?.trades ?? 0 };
  });
  byId("curve-empty").hidden = !!trades;
  byId("curve-summary").textContent = trades ? (period + " 天 · " + trades + " 笔主平仓 · " + pnlText(total)) :
    snapshot.ledger_ready ? "0 笔主平仓" : "暂时无法读取账本";
  canvas.setAttribute("aria-label", byId("curve-summary").textContent);
  if (!trades) {
    curveGeometry = null;
    byId("curve-tip").hidden = true;
    return;
  }

  const values = [0, ...points.map((p) => p.total)];
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const padding = Math.max(0.1, (hi - lo) * 0.15);
  const left = 50, right = width - 15, top = 13, bottom = height - 35;
  const y = (value) => bottom - (value - lo + padding) * (bottom - top) / (hi - lo + 2 * padding);
  const dx = (right - left) / period;
  curveGeometry = { left, dx, length: points.length };
  ctx.font = "12px system-ui";
  ctx.fillStyle = "#68647b";
  for (const level of new Set([lo, 0, hi])) {
    ctx.strokeStyle = level === 0 ? "#b3a7bd" : "#e5dfe8";
    ctx.beginPath();
    ctx.moveTo(left, y(level));
    ctx.lineTo(right, y(level));
    ctx.stroke();
    ctx.fillText(numberText(level, pnlFormat), 1, y(level) + 4);
  }
  ctx.beginPath();
  ctx.moveTo(left, y(0));
  points.forEach((p, i) => ctx.lineTo(left + (i + 1) * dx, y(p.total)));
  ctx.strokeStyle = "#653dcc";
  ctx.lineWidth = 2.4;
  ctx.stroke();
  ctx.lineTo(right, y(0));
  ctx.closePath();
  ctx.fillStyle = "rgba(101, 61, 204, 0.12)";
  ctx.fill();
  ctx.fillStyle = "#68647b";
  ctx.fillText(points[0].day.slice(5), left, height - 11);
  const end = points.at(-1).day.slice(5);
  ctx.fillText(end, right - ctx.measureText(end).width, height - 11);
  const tip = byId("curve-tip");
  const point = points[curveIndex];
  tip.hidden = !point;
  if (point) {
    const x = left + (curveIndex + 1) * dx;
    ctx.strokeStyle = "#89769f";
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#653dcc";
    ctx.beginPath();
    ctx.arc(x, y(point.total), 4, 0, 2 * Math.PI);
    ctx.fill();
    tip.textContent = point.day + "｜累计 " + pnlText(point.total) +
      "｜当日 " + pnlText(point.pnl) + "｜" + point.count + " 笔";
    tip.style.left = Math.max(4, Math.min(x + 10, width - tip.offsetWidth - 5)) + "px";
  }
}

function exposures(rows) {
  const box = byId("exposure");
  const ordered = [...rows].sort((a, b) => Math.abs(b.estimated_pnl) - Math.abs(a.estimated_pnl));
  const max = Math.max(1, ...ordered.map((p) => Math.abs(p.estimated_pnl)));
  box.replaceChildren();
  if (!rows.length) box.append(element("p", "当前空仓", "muted"));
  for (const pos of ordered) {
    const row = element("div", "", "exposure-row");
    const canvas = element("canvas", "", "bar");
    canvas.setAttribute("aria-hidden", "true");
    const pnl = element("span");
    setPnl(pnl, pos.estimated_pnl);
    row.append(element("strong", pos.symbol), canvas, pnl);
    box.append(row);
    const [ctx, width, height] = canvasContext(canvas);
    if (!ctx) continue;
    ctx.fillStyle = "#eae0e3";
    ctx.fillRect(0, 4, width, height - 8);
    const length = (pos.estimated_pnl || 0) * width / (max * 2);
    ctx.fillStyle = length > 0 ? "#2a9d7b" : "#d45b71";
    ctx.fillRect(width / 2 + Math.min(0, length), 2, Math.abs(length), height - 4);
    ctx.fillStyle = "#968794";
    ctx.fillRect(width / 2, 0, 1, height);
  }
}

function timestamp(ms) {
  return Number.isFinite(ms) ? dateTime.format(ms) : "—";
}

function storyNode(title, url) {
  try {
    const parsed = new URL(url);
    if (["https:", "http:"].includes(parsed.protocol)) {
      const link = element("a", title, "story");
      link.href = parsed.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      return link;
    }
  } catch {}
  return element("span", title, "story");
}

function updatePanel(id, rows, empty, create, placeholder) {
  const version = JSON.stringify([rows, empty]);
  if (panelVersions.get(id) === version) return;
  panelVersions.set(id, version);
  byId(id).replaceChildren(...(rows.length ? rows.map(create) : [element(placeholder, empty, "muted")]));
}

function researchPanels(data) {
  const filter = byId("news-source");
  const feeds = data.news_by_source || {};
  const sources = Object.keys(feeds).sort();
  if (filter.dataset.sources !== JSON.stringify(sources)) {
    const prev = filter.value;
    filter.replaceChildren(new Option("全部来源", ""), ...sources.map((name) => new Option(name, name)));
    if (sources.includes(prev)) filter.value = prev;
    filter.dataset.sources = JSON.stringify(sources);
    syncDropdown(filter);
  }
  const visibleNews = filter.value ? feeds[filter.value] || [] : data.news;
  updatePanel("news", visibleNews, "暂无采集快讯", (item) => {
    const row = element("li");
    const lag = Number.isFinite(item.published_ms) ? Math.round((item.observed_ms - item.published_ms) / 1000) : null;
    const timing = lag !== null && lag >= 0 ? " · " + (lag < 60 ? lag + " 秒" :
      Math.floor(lag / 60) + " 分 " + lag % 60 + " 秒") : "";
    row.append(storyNode(item.title, item.url),
      element("p", item.source + " · " + (item.source === "Equation News" ? "收录 " : "发布 ") + (Number.isFinite(item.published_ms) ? timestamp(item.published_ms) : item.published) +
        " · 收到 " + timestamp(item.observed_ms) + timing, "meta"));
    return row;
  }, "li");
  updatePanel("rsi-opens", data.rsi_opens || [], "暂无 RSI 开仓账本记录", (item) => {
    const row = element("article");
    const header = element("div", "", "signal-head");
    header.append(element("strong", item.symbol + (item.side === "SHORT" ? " · 开空" : " · 开多")),
      element("time", timestamp(item.opened_ms)));
    const status = { pending: "等待影子分析", missing: "未匹配到影子结果", complete: "分析完成",
      no_news: "无可用时效新闻", model_error: "模型结果异常" }[item.status];
    const picks = element("div", "", "actions");
    picks.append(element("span", status, "pill"));
    for (const [label, result] of [["纯行情", item.market], ["加新闻", item.with_news]]) {
      if (result?.status === "ok") picks.append(element("span", label + " · " +
        (actions[result.action] || "已分析"), "pill"));
    }
    row.append(header, picks, element("p",
      (item.exact ? "信号已关联" : "按开仓小时推算信号") + " · " + timestamp(item.signal_ms), "meta"));
    return row;
  }, "p");
  updatePanel("shadow", data.shadow, "暂无 RSI 影子记录", (item) => {
    const row = element("article");
    const head = element("div", "", "signal-head");
    head.append(element("strong", item.symbol + (item.side === "SHORT" ? " · 开空信号" : " · 开多信号")),
      element("time", timestamp(item.signal_ms)));
    const picks = element("div", "", "actions");
    const details = element("details");
    details.append(element("summary", "分析依据"));
    for (const [name, result] of [["纯行情", item.market], ["加新闻", item.with_news]]) {
      const label = result?.status === "no_news" ? "无新闻" : result ? actions[result.action] || "不可用" : "待处理";
      picks.append(element("span", name + " · " + label, "pill"));
      if (result?.reason) details.append(element("p", name + " · " + result.reason));
    }
    let summary = "RSI " + numberText(item.rsi, pnlFormat) + " · " + item.news_count + " 条新闻";
    if (item.next?.close) summary += " · 下一小时收盘 " + numberText(item.next.close);
    row.append(head, element("p", summary, "meta"), picks);
    if (details.childElementCount > 1) row.append(details);
    return row;
  }, "p");
  const grouped = new Map();
  for (const item of data.events) {
    if (item.quote_status === "invalid_ticker") continue;
    const key = item.event_key + "|" + item.stage;
    const existing = grouped.get(key);
    const first = existing && existing.observed_ms < item.observed_ms ? existing : item;
    grouped.set(key, { ...first, count: (existing?.count ?? 0) + 1 });
  }
  const events = [...grouped.values()].sort((a, b) => b.observed_ms - a.observed_ms);
  updatePanel("events", events, "暂无动态事件记录", (item) => {
    const row = element("li");
    const stage = { watch: "待跟踪", reported: "媒体报道", confirmed: "已确认" }[item.stage] || item.stage;
    row.append(element("strong", item.symbol + " · " + stage + " · " + item.title),
      element("p", item.source + " · 发布 " + timestamp(item.published_ms) + " · 收到 " +
        timestamp(item.observed_ms) + (item.count > 1 ? " · " + item.count + " 条同阶段报道" : ""), "meta"));
    const quoteStatus = {
      watch: "等待后续确认", pending: "等待盘口", monitoring: "观察中", complete: "盘口观察完成",
      incomplete: "盘口观察不完整", no_testnet_contract: "测试网无可交易合约",
      late_news: "消息到达已晚", expired: "错过初始盘口", no_quote: "未获取有效买卖报价",
    }[item.quote_status];
    if (quoteStatus) row.append(element("p", quoteStatus, "meta"));
    if (item.book) {
      const horizon = item.book.horizons;
      const samples = [60, 300, 900].map((s) => horizon[s] ?
        (s / 60 + " 分钟 · 多 " + numberText(horizon[s].long_pct, pnlFormat) + "% / 空 " +
          numberText(horizon[s].short_pct, pnlFormat) + "%") : (s / 60 + " 分钟 · 暂无"));
      row.append(element("p", "首笔买卖价 " + numberText(item.book.entry.bid) + " / " +
        numberText(item.book.entry.ask) + " · 价差 " + numberText(item.book.spread_bps) + " bps", "meta"),
        element("p", samples.join(" · "), "meta"));
    }
    return row;
  }, "li");
  const account = data.news_model_account;
  const accountLabel = { available: "DeepSeek API 账户可用", unavailable: "DeepSeek API 账户不可用",
    check_failed: "DeepSeek API 账户检查失败", unsupported: "当前服务商未提供账户检查",
    stale: "API 账户状态已过期" }[account?.status] || "等待 API 账户检查";
  const accountText = account?.checked_ms ? accountLabel + " · 检查于 " + timestamp(account.checked_ms) : accountLabel;
  const accountNode = byId("news-model-account");
  if (accountNode.textContent !== accountText) accountNode.textContent = accountText;
  const { push, rest } = data.benzinga || {};
  const transportParts = [];
  if (push?.last_article_ms) transportParts.push("最近推送 " + timestamp(push.last_article_ms));
  else if (push?.last_heartbeat_ms) transportParts.push("心跳 " + timestamp(push.last_heartbeat_ms) + " · 尚无新闻推送");
  if (push?.status?.startsWith("HTTP ")) transportParts.push("推送 " + push.status);
  if (rest?.status === "ok") transportParts.push("REST 补采检查 " + timestamp(rest.last_ok_ms));
  else if (rest) transportParts.push("REST 补采异常" + (rest.status.startsWith("HTTP ") ? " · " + rest.status : ""));
  byId("benzinga-transport").textContent = transportParts.length
    ? "Benzinga · " + transportParts.join(" · ") : "等待 Benzinga 采集状态";
  const acceptance = data.news_acceptance;
  const phase = {
    waiting: "等待首笔自然信号", pending: "首笔信号已进入测试网执行队列",
    filled: "首笔已成交，等待平仓", closing: "首笔等待平仓确认",
    reconciling: "首笔已平仓，等待账本对账", verified: "首笔已成交、平仓并入账",
  }[acceptance?.status];
  const acceptanceText = phase
    ? "新闻测试网 · " + phase + (acceptance.symbol ? " · " + acceptance.symbol : "")
    : "等待新闻测试网验收状态";
  const acceptanceNode = byId("news-trade-acceptance");
  if (acceptanceNode.textContent !== acceptanceText) acceptanceNode.textContent = acceptanceText;
  updatePanel("news-entries", data.news_entries || [], "等待有足够时效的新事件", (item) => {
    const row = element("article");
    const head = element("div", "", "signal-head");
    head.append(element("strong", item.symbol), element("time", timestamp(item.decision_ms ?? item.started_ms)));
    const bias = { long: "做多", short: "做空", wait: "等待", skip: "跳过" }[item.action] ||
      (item.status === "model_error" ? "无结果" : "待分析");
    const stage = { watch: "线索", reported: "媒体报道", confirmed: "已确认" }[item.stage] || item.stage;
    const state = {
      testnet_queued: "等待交易进程", testnet_order_pending: "等待币安成交确认",
      testnet_open: "测试网持仓中", testnet_closed: "测试网已平仓", testnet_closing: "平仓待确认",
      testnet_expired: "下单时窗已过", testnet_capacity: "已达持仓上限",
      testnet_position_conflict: "存在同币持仓或持仓冲突", testnet_quantity: "数量不满足合约规则",
      testnet_setup_failed: "币安杠杆或保证金设置失败", testnet_rejected: "测试网订单未成交",
      testnet_disabled: "测试网新闻交易未启用", testnet_close_unverified: "等待平仓对账",
      queued: "等待筛选", market_only: "行情异动，仅观察", window_expired: "入场时窗已过",
      analyzing: "分析中", wait: "等新证据", skip: "AI 跳过", needs_confirmation: "证据待确认",
      late_news: "消息到达过晚", catalog_unavailable: "合约目录不可用", no_testnet_contract: "测试网无可交易合约",
      stale_decision: "判断窗口已过", checking_book: "核对盘口", no_quote: "未取得新报价",
      wide_spread: "价差过宽", thin_book: "盘口数量不足", duplicate_entry: "同一事件已有模拟",
      paper_capacity: "模拟仓位已满", paper_open: "模拟观察中", paper_closed: "模拟已结算",
      paper_incomplete: "盘口结果不完整", model_error: "本次分析失败", model_retrying: "等待模型重试", interrupted: "处理曾中断",
    }[item.status] || item.status;
    const picks = element("div", "", "actions");
    const result = item.order_id ? "测试网成交" : item.paper?.entry ? "已模拟开仓" :
      ["queued", "analyzing", "model_retrying", "checking_book", "testnet_queued", "testnet_order_pending"].includes(item.status) ? "待完成" : "未开仓";
    const verdict = item.observation_reason ? "规则观察" : "AI 建议 · " + bias;
    picks.append(element("span", verdict, "pill signal-verdict"),
      element("span", result + " · " + state, "pill"), element("span", stage, "pill"));
    row.append(head, picks);
    if (item.observation_reason) row.append(element("p", item.observation_reason, "meta"));
    if (item.reason) row.append(element("p",
      item.status === "model_error" && !item.model_error ? item.reason + " · 历史记录无 HTTP 状态" : item.reason, "meta"));
    if (item.model_error) {
      const error = item.model_error;
      const code = error.http_status ? "HTTP " + error.http_status : error.kind;
      const detail = (item.status === "model_retrying" || item.status === "model_error" ? "模型请求" : "此前请求") +
        " · 第 " + error.attempt + " 次 · " + code +
        (item.status === "model_retrying" ? " · 下次 " + timestamp(error.next_retry_ms) : "");
      row.append(element("p", detail, "meta"));
    }
    if (item.order_id) row.append(element("p", "订单 " + item.order_id + " · 成交量 " +
      numberText(item.quantity) + " · 均价 " + numberText(item.entry_price), "meta"));
    const entry = item.paper?.entry;
    const book = entry?.quote || item.gate;
    if (book) {
      const text = "买一 / 卖一 " + numberText(book.bid) + " / " + numberText(book.ask) +
        (entry ? " · 模拟入场 " + numberText(entry.price) + " · 名义 " + numberText(entry.notional_usdt) + " USDT" : "");
      row.append(element("p", text, "meta"));
    }
    if (item.paper) {
      const outcome = [60, 300, 900].map(s => {
        const value = item.paper.outcomes?.[s];
        return s / 60 + " 分钟 " + (value?.status === "ok" ? pnlText(value.net_usdt) :
          value ? "缺有效报价" : "待观察");
      });
      row.append(element("p", "估算净盈亏 · " + outcome.join(" · "), "meta"));
      const exit = item.paper.outcomes?.[900];
      if (exit?.status === "ok") row.append(element("p", "15 分钟成本 · 手续费 " +
        numberText(exit.fee_usdt) + " · 滑点 " + numberText(exit.slippage_usdt) + " USDT", "meta"));
    }
    if (item.evidence.length) {
      const details = element("details");
      details.append(element("summary", "查看原始新闻"));
      for (const source of item.evidence) {
        details.append(storyNode(source.source + " · " + source.title, source.url));
      }
      row.append(details);
    }
    return row;
  }, "p");
}

function render(data) {
  snapshot = data;
  const count = data.positions.length;
  byId("market").textContent = data.market + " · 只读";
  byId("timestamp").textContent = timeText(data.updated);
  status(data.fresh ? "快照正常" : data.counts_match ? "快照已过期" : "持仓同步中", data.fresh);
  byId("position-count").textContent = count + " / " + (data.capacity ?? "—");
  const longs = data.positions.filter((row) => row.side === "LONG").length;
  const shorts = data.positions.filter((row) => row.side === "SHORT").length;
  byId("position-detail").textContent = longs + " 多 · " + shorts + " 空";
  byId("open-summary").textContent = count ? ("共 " + count + " 笔") : "";
  setPnl(byId("floating-pnl"), data.estimated_pnl);
  setPnl(byId("daily-pnl"), data.daily_pnl);
  table("positions", data.positions, 7, "当前空仓", (row, item) => {
    cell(row, item.symbol);
    sideCell(row, item.side);
    cell(row, numberText(item.quantity));
    const entry = cell(row, numberText(item.entry_price));
    const source = document.createElement("small");
    source.textContent = item.regime === "news_trend" ? "新闻趋势 · " + item.entry_source : item.entry_source;
    entry.append(source);
    cell(row, numberText(item.last_price));
    setPnl(cell(row, ""), item.estimated_pnl);
    cell(row, timeText(item.entry_time));
  });
  table("closed", data.closed, 5, data.ledger_ready ? "暂无平仓记录" : "暂时无法读取平仓账本", (row, item) => {
    cell(row, item.symbol);
    sideCell(row, item.direction);
    cell(row, timeText(item.close_ts));
    const reason = Object.hasOwn(reasons, item.close_reason) ? reasons[item.close_reason] : (item.close_reason || "—");
    cell(row, item.regime === "news_trend" ? "新闻趋势 · " + (reason === "news_15m" ? "15 分钟到期" : reason) : reason);
    setPnl(cell(row, ""), item.pnl_usdt);
  });
  drawCurve();
  exposures(data.positions);
  researchPanels(data);
  window.dispatchEvent(new CustomEvent("morgan:snapshot", { detail: data }));
}

async function refresh() {
  try {
    const response = await fetch("/api/dashboard", {
      cache: "no-store", signal: AbortSignal.timeout(4500),
    });
    if (response.status === 503) {
      status("等候监控快照");
    } else {
      if (!response.ok) throw new Error("无法读取快照");
      render(await response.json());
    }
  } catch {
    status("连接中断 · 保留上次快照");
  } finally {
    setTimeout(refresh, 5000);
  }
}

document.querySelectorAll("[data-days]").forEach((button) => {
  button.addEventListener("click", () => {
    period = Number(button.dataset.days);
    curveIndex = null;
    document.querySelectorAll("[data-days]").forEach((peer) => peer.setAttribute("aria-pressed", String(peer === button)));
    drawCurve();
  });
});
const curve = byId("curve");
curve.addEventListener("pointermove", (event) => {
  if (!curveGeometry) return;
  const { left, dx, length } = curveGeometry;
  const next = Math.max(0, Math.min(length - 1, Math.round((event.clientX - curve.getBoundingClientRect().left - left) / dx) - 1));
  if (next === curveIndex) return;
  curveIndex = next;
  drawCurve();
});
function resetCurve() {
  curveIndex = null;
  drawCurve();
}
curve.addEventListener("pointerleave", resetCurve);
curve.addEventListener("blur", resetCurve);
curve.addEventListener("keydown", (event) => {
  if (!curveGeometry || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  event.preventDefault();
  const last = curveGeometry.length - 1;
  curveIndex = Math.max(0, Math.min(last, (curveIndex ?? last) + (event.key === "ArrowLeft" ? -1 : 1)));
  drawCurve();
});
for (const id of ["news-source", "market-symbol"]) enhanceSelect(byId(id));
byId("news-source").addEventListener("change", () => { if (snapshot) researchPanels(snapshot); });
const navTrack = document.querySelector("nav.slide-control");
const navLinks = [...navTrack.querySelectorAll("a")];
const navSections = navLinks.map(link => byId(link.hash.slice(1)));
const masthead = document.querySelector(".masthead");
let scrollFrame = 0;
let navDestination = null, navFallback;

function selectNav(active) {
  for (const link of navLinks) {
    link.classList.toggle("selected", link === active);
    if (link === active) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  }
  moveIndicator(navTrack, active);
}

function syncNavToScroll() {
  if (navDestination !== null) {
    if (Math.abs(window.scrollY - navDestination) > 2) return;
    clearNavDestination();
  }
  const inset = parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop) || 0;
  const edge = Math.max(masthead.getBoundingClientRect().bottom + 36, inset + 3);
  const atBottom = window.scrollY > 0 &&
    document.documentElement.scrollHeight - window.innerHeight - window.scrollY <= 2;
  const index = atBottom ? navSections.length - 1 : navSections.findLastIndex(section =>
    section.getBoundingClientRect().top <= edge);
  selectNav(navLinks[Math.max(index, 0)]);
}

function scheduleNavSync() {
  if (scrollFrame) return;
  scrollFrame = requestAnimationFrame(() => {
    scrollFrame = 0;
    syncNavToScroll();
  });
}

function clearNavDestination() {
  navDestination = null;
  clearTimeout(navFallback);
}

function syncNavToHash() {
  clearNavDestination();
  selectNav(navLinks.find(link => link.hash === window.location.hash) || navLinks[0]);
}

for (const [index, link] of navLinks.entries()) {
  link.addEventListener("click", event => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey ||
        event.shiftKey || event.altKey) return;
    event.preventDefault();
    clearNavDestination();
    const root = document.documentElement;
    const inset = parseFloat(getComputedStyle(root).scrollPaddingTop) || 0;
    const end = Math.max(0, root.scrollHeight - window.innerHeight);
    const target = window.scrollY + navSections[index].getBoundingClientRect().top - inset;
    const top = Math.min(end, Math.max(0, target));
    const animate = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (window.location.hash !== link.hash) history.pushState(null, "", link.hash);
    selectNav(link);
    if (animate && Math.abs(window.scrollY - top) > 2) {
      navDestination = top;
      navFallback = setTimeout(() => {
        clearNavDestination();
        scheduleNavSync();
      }, 1400);
    }
    window.scrollTo({ top, behavior: animate ? "smooth" : "instant" });
  });
}

for (const event of ["wheel", "touchstart", "pointerdown"]) {
  window.addEventListener(event, clearNavDestination, { passive: true });
}
window.addEventListener("keydown", event => {
  if (!event.defaultPrevented && ["PageDown", "PageUp", "ArrowDown", "ArrowUp", "Home", "End", " "].includes(event.key)) {
    clearNavDestination();
  }
});
wireSlider(navTrack, () => navLinks, link => link.classList.contains("selected"));
window.addEventListener("hashchange", syncNavToHash);
window.addEventListener("scroll", scheduleNavSync, { passive: true });
window.addEventListener("resize", () => {
  scheduleNavSync();
  if (snapshot) {
    drawCurve();
    exposures(snapshot.positions);
  }
});
window.addEventListener("pageshow", scheduleNavSync);
syncNavToHash();
refresh();
