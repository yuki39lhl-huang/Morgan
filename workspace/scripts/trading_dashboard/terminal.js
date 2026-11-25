"use strict";

(() => {
  const chart = byId("candles");
  const rsiChart = byId("rsi-chart");
  const hour = 3_600_000;
  const select = byId("market-symbol");
  const strip = byId("ticker-strip");
  const green = "#85edc1", red = "#ff88a1", muted = "#bdb3d7";
  let symbol = "BTC", range = 72, bars = [], rsi = [], cursor = null, geometry;
  let request = 0, fetchedAt = 0, busy = false;
  let plottedSymbol = null, controller;
  const chartStatus = byId("chart-status");
  const quotes = new Map();
  let symbols = [];

  function placeTickerSelection(name, reveal) {
    const button = quotes.get(name)?.button;
    if (!button || Object.hasOwn(strip.dataset, "sliding")) return;
    moveIndicator(strip, button);
    if (!reveal) return;
    const left = button.offsetLeft;
    const right = left + button.offsetWidth;
    if (left < strip.scrollLeft) strip.scrollLeft = left;
    else if (right > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = right - strip.clientWidth;
  }

  function renderSymbol(reveal = false) {
    select.value = symbol;
    syncDropdown(select);
    const shownSymbol = plottedSymbol ?? symbol;
    byId("quote-title").textContent = shownSymbol + "/USDT";
    byId("quote-last").textContent = numberText(quotes.get(shownSymbol)?.value);
    for (const [name, item] of quotes) {
      item.button.setAttribute("aria-pressed", String(name === symbol));
    }
    placeTickerSelection(symbol, reveal);
  }

  function drawCandles() {
    geometry = null;
    const [ctx, width, height] = canvasContext(chart);
    if (!ctx || width < 130) return;
    const shown = bars.slice(-range);
    const empty = byId("candle-empty");
    empty.hidden = shown.length > 0;
    if (!shown.length) {
      geometry = null;
      byId("ohlc").textContent = "—";
      byId("quote-change").textContent = "";
      return;
    }
    const left = 10, right = width - 69, top = 15, bottom = height - 71, axis = height - 19;
    const hi = Math.max(...shown.map((b) => b[2]));
    const lo = Math.min(...shown.map((b) => b[3]));
    const padding = (hi - lo || hi * .001) * .09;
    const maxVol = Math.max(1, ...shown.map((b) => b[5]));
    const dy = (hi - lo + 2 * padding) || 1;
    const y = (value) => bottom - (value - lo + padding) / dy * (bottom - top);
    const startMs = shown[0][0], stepMs = hour;
    const slots = (shown.at(-1)[0] - startMs) / stepMs + 1;
    const dx = (right - left) / slots;
    const x = (bar) => left + ((bar[0] - startMs) / stepMs + .5) * dx;
    const tickWidth = Math.max(1, Math.min(12, dx * .65));
    geometry = { shown, x };
    ctx.font = "12px system-ui";
    for (let i = 0; i <= 4; i++) {
      const value = lo + (hi - lo) * i / 4;
      const level = y(value);
      ctx.strokeStyle = "#66588254";
      ctx.beginPath();
      ctx.moveTo(left, level);
      ctx.lineTo(right, level);
      ctx.stroke();
      ctx.fillStyle = muted;
      ctx.fillText(numberText(value), right + 6, level + 4);
    }
    for (const bar of shown) {
      const [, opened, high, low, close, volume] = bar;
      const center = x(bar), color = close >= opened ? green : red;
      ctx.strokeStyle = color;
      ctx.beginPath();
      ctx.moveTo(center, y(high));
      ctx.lineTo(center, y(low));
      ctx.stroke();
      ctx.fillStyle = color;
      ctx.fillRect(center - tickWidth / 2, Math.min(y(opened), y(close)), tickWidth, Math.max(1, Math.abs(y(opened) - y(close))));
      ctx.globalAlpha = .4;
      const volumeHeight = (axis - bottom - 13) * volume / maxVol;
      ctx.fillRect(center - tickWidth / 2, axis - volumeHeight, tickWidth, volumeHeight);
      ctx.globalAlpha = 1;
    }
    ctx.fillStyle = muted;
    ctx.fillText("成交量", right + 6, axis - 4);
    for (const i of new Set([0, Math.floor((shown.length - 1) / 2), shown.length - 1])) {
      const label = timestamp(shown[i][0]).slice(0, -3);
      const labelX = width < 470 && i > 0 && i < shown.length - 1 ? null :
        Math.max(left, Math.min(x(shown[i]) - 25, right - ctx.measureText(label).width));
      if (labelX !== null) ctx.fillText(label, labelX, height - 3);
    }
    const last = shown.at(-1);
    const bar = shown[cursor] || last;
    byId("ohlc").textContent = timestamp(bar[0]) + "  开 " + numberText(bar[1]) +
      "  高 " + numberText(bar[2]) + "  低 " + numberText(bar[3]) + "  收 " + numberText(bar[4]) +
      "  量 " + numberText(bar[5]);
    const change = byId("quote-change");
    const pct = (last[4] / last[1] - 1) * 100;
    change.textContent = numberText(pct, pnlFormat) + "% / 1H";
    change.classList.toggle("positive", pct >= 0);
    change.classList.toggle("negative", pct < 0);
    if (cursor !== null && shown[cursor]) {
      ctx.strokeStyle = "#d8c1fb";
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(x(bar), top);
      ctx.lineTo(x(bar), axis);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  function drawRsi() {
    const [ctx, width, height] = canvasContext(rsiChart);
    const value = byId("rsi-value");
    const empty = byId("rsi-empty");
    const values = rsi.slice(-range);
    const any = values.some(Number.isFinite);
    empty.hidden = any;
    if (!any) {
      empty.textContent = bars.length ? "该区间暂无 RSI（需至少 15 根连续收盘 K 线）" :
        byId("candle-empty").textContent || "等待缓存 K 线";
      value.textContent = "—";
    }
    if (!ctx || !geometry || width < 130 || !any) return;
    const { shown, x } = geometry;
    const left = 10, right = width - 69, top = 9, bottom = height - 10;
    const y = (v) => bottom - v / 100 * (bottom - top);
    ctx.fillStyle = "#ff88a116";
    ctx.fillRect(left, top, right - left, y(70) - top);
    ctx.fillStyle = "#85edc116";
    ctx.fillRect(left, y(30), right - left, bottom - y(30));
    ctx.font = "12px system-ui";
    for (const level of [70, 50, 30]) {
      ctx.setLineDash(level === 50 ? [3, 4] : []);
      ctx.strokeStyle = "#665882a0";
      ctx.beginPath();
      ctx.moveTo(left, y(level));
      ctx.lineTo(right, y(level));
      ctx.stroke();
      ctx.fillStyle = muted;
      ctx.fillText(String(level), right + 6, y(level) + 4);
    }
    ctx.setLineDash([]);
    ctx.strokeStyle = "#c9b6ff";
    ctx.fillStyle = "#c9b6ff";
    ctx.lineWidth = 1.5;
    for (let i = 0; i < shown.length; i++) {
      if (!Number.isFinite(values[i])) continue;
      const contiguous = i > 0 && Number.isFinite(values[i - 1]) && shown[i][0] - shown[i - 1][0] === hour;
      const center = x(shown[i]), level = y(values[i]);
      ctx.beginPath();
      if (contiguous) {
        ctx.moveTo(x(shown[i - 1]), y(values[i - 1]));
        ctx.lineTo(center, level);
        ctx.stroke();
      } else {
        ctx.arc(center, level, 1.5, 0, 2 * Math.PI);
        ctx.fill();
      }
    }
    const index = cursor ?? shown.length - 1;
    const selected = values[index];
    value.textContent = timestamp(shown[index][0] + hour) + " 收盘 · " + numberText(selected, pnlFormat);
    if (cursor !== null) {
      const center = x(shown[index]);
      ctx.strokeStyle = "#d8c1fb";
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(center, top);
      ctx.lineTo(center, bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      if (Number.isFinite(selected)) {
        ctx.beginPath();
        ctx.arc(center, y(selected), 3, 0, 2 * Math.PI);
        ctx.fill();
      }
    }
  }

  function drawCharts() {
    drawCandles();
    drawRsi();
  }

  async function loadCandles() {
    const active = ++request;
    const target = symbol;
    controller?.abort();
    controller = new AbortController();
    busy = true;
    const empty = byId("candle-empty");
    const changing = plottedSymbol !== target;
    chartStatus.hidden = true;
    const loading = changing ? setTimeout(() => {
      if (active !== request) return;
      chartStatus.textContent = "正在读取 " + target + "/USDT";
      chartStatus.hidden = false;
    }, 140) : null;
    try {
      const response = await fetch("/api/klines?symbol=" + encodeURIComponent(target), {
        cache: "no-store", signal: AbortSignal.any([controller.signal, AbortSignal.timeout(4000)]),
      });
      if (!response.ok) throw new Error("无法读取 K 线");
      const data = await response.json();
      if (active !== request || data.symbol !== symbol) return;
      if (!Array.isArray(data.candles) || !Array.isArray(data.rsi) || data.rsi.length !== data.candles.length) {
        throw new Error("行情数据不完整");
      }
      if (changing) cursor = null;
      bars = data.candles;
      rsi = data.rsi;
      plottedSymbol = target;
      if (cursor !== null && cursor >= Math.min(bars.length, range)) cursor = null;
      fetchedAt = Date.now();
      if (!bars.length) empty.textContent = "该标的暂无已收盘缓存";
      chartStatus.hidden = true;
      renderSymbol();
      drawCharts();
      if (changing && bars.length) {
        for (const canvas of [chart, rsiChart]) {
          playMotion(canvas, [{ opacity: .56 }, { opacity: 1 }], 260);
        }
      }
    } catch {
      if (active !== request) return;
      fetchedAt = Date.now();
      if (changing) {
        chartStatus.textContent = target + "/USDT 暂时无法读取";
        chartStatus.hidden = false;
      }
    } finally {
      clearTimeout(loading);
      if (active === request) busy = false;
    }
  }

  function chooseSymbol(name) {
    if (!quotes.has(name)) return;
    if (name === symbol) {
      placeTickerSelection(symbol, true);
      if (!busy && plottedSymbol !== name) loadCandles();
      return;
    }
    symbol = name;
    cursor = null;
    renderSymbol(true);
    loadCandles();
  }

  function ticker(data) {
    const names = Object.keys(data.quotes);
    if (names.join("|") !== symbols.join("|")) {
      symbols = names;
      strip.classList.remove("has-indicator");
      strip.replaceChildren();
      select.replaceChildren();
      quotes.clear();
      for (const name of names) {
        select.append(new Option(name + "/USDT", name));
        const button = element("button", "", "ticker");
        const value = element("span");
        button.type = "button";
        button.append(element("strong", name), value);
        button.addEventListener("click", () => chooseSymbol(name));
        strip.append(button);
        quotes.set(name, { button, text: value, value: null });
      }
      if (!quotes.has(symbol) && names.length) chooseSymbol(names[0]);
    }
    for (const [name, item] of quotes) {
      const value = data.quotes[name];
      if (Number.isFinite(item.value) && Number.isFinite(value) && value !== item.value) {
        playMotion(item.text, [{ color: value > item.value ? "#08735b" : "#b43750",
          background: value > item.value ? "#d9eddf" : "#f5dce1" },
        { color: "#605b69", background: "transparent" }], 650);
      }
      item.value = value;
      item.text.textContent = numberText(value);
    }
    renderSymbol();
    if (!busy && Date.now() - fetchedAt >= 15000) loadCandles();
  }

  function showDigest(digest) {
    const content = byId("digest");
    const key = String(digest?.generated_ms || "");
    if (content.dataset.updated === key) return;
    content.dataset.updated = key;
    if (!digest) {
      content.replaceChildren(element("p", "等待有可核查来源的简报", "muted"));
      return;
    }
    byId("digest-time").textContent = timestamp(digest.generated_ms);
    const summary = element("p", digest.summary, "digest-summary");
    const cards = (digest.items || []).map(item => {
      const card = element("article", "", "digest-item");
      const evidence = element("div", "", "citations");
      for (const source of item.evidence || []) {
        evidence.append(storyNode(source.source + " · " + timestamp(source.published_ms), source.url));
      }
      card.append(element("h4", item.title), element("p", item.why), evidence);
      return card;
    });
    content.replaceChildren(summary, ...cards);
  }

  function applySnapshot(data) {
    ticker(data);
    showDigest(data.digest);
    const digest = data.digest;
    byId("digest-time").textContent = digest ? timestamp(digest.generated_ms) +
      (Date.now() - digest.generated_ms > 6 * 3600000 ? " · 历史简报" : "") : "";
  }

  byId("market-symbol").addEventListener("change", () => chooseSymbol(select.value));
  wireSlider(strip, () => symbols.map(name => quotes.get(name).button),
    button => button.getAttribute("aria-pressed") === "true", true);
  document.querySelectorAll("[data-bars]").forEach(button => button.addEventListener("click", () => {
    range = Number(button.dataset.bars);
    cursor = null;
    document.querySelectorAll("[data-bars]").forEach(peer => peer.setAttribute("aria-pressed", String(peer === button)));
    drawCharts();
  }));
  const newsTabs = [...document.querySelectorAll("[data-news]")];
  const newsViewport = byId("news-viewport");
  wireSlider(newsTabs[0].parentElement, () => newsTabs, button => button.getAttribute("aria-pressed") === "true");
  newsTabs.forEach(button => button.addEventListener("click", () => {
    if (button.getAttribute("aria-pressed") === "true") return;
    newsTabs.forEach(peer => peer.setAttribute("aria-pressed", String(peer === button)));
    newsViewport.querySelectorAll("[data-news-panel]").forEach(panel => {
      panel.hidden = panel.dataset.newsPanel !== button.dataset.news;
    });
    newsViewport.scrollTop = 0;
    moveIndicator(button.parentElement, button);
  }));
  for (const canvas of [chart, rsiChart]) {
    canvas.addEventListener("pointermove", event => {
      if (!geometry) return;
      const target = event.clientX - canvas.getBoundingClientRect().left;
      const next = geometry.shown.reduce((best, bar, i, all) =>
        Math.abs(geometry.x(bar) - target) < Math.abs(geometry.x(all[best]) - target) ? i : best, 0);
      if (next === cursor) return;
      cursor = next;
      drawCharts();
    });
    canvas.addEventListener("keydown", event => {
      if (!geometry || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const last = geometry.shown.length - 1;
      cursor = Math.max(0, Math.min(last, (cursor ?? last) + (event.key === "ArrowLeft" ? -1 : 1)));
      drawCharts();
    });
    for (const type of ["pointerleave", "blur"]) canvas.addEventListener(type, () => {
      cursor = null;
      drawCharts();
    });
  }
  function placeNewsIndicator() {
    const active = document.querySelector('[data-news][aria-pressed="true"]');
    moveIndicator(active?.parentElement, active);
  }
  window.addEventListener("resize", () => {
    drawCharts();
    placeTickerSelection(symbol, true);
    placeNewsIndicator();
  });
  placeNewsIndicator();
  window.addEventListener("morgan:snapshot", event => applySnapshot(event.detail));
  if (snapshot) applySnapshot(snapshot);
})();
