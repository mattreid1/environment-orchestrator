"use strict";
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let data = null, selected = null, filter = "all", connected = false, source = null, confirmation = null;
const pending = new Map();
const charts = new Map(), workspaceRows = new Map();
let messageTimer;
const bytes = value => {
  if (value == null) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let n = Math.max(0, value), unit = 0;
  while (n >= 1024 && unit < units.length - 1) { n /= 1024; unit++; }
  return `${n.toFixed(unit && n < 10 ? 1 : 0)} ${units[unit]}`;
};
const latency = value => value == null ? "—" : value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(2)} s`;
const duration = value => value < 60 ? `${Math.floor(value)}s` : value < 3600 ? `${Math.floor(value / 60)}m ${Math.floor(value % 60)}s` : `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}m`;
const awake = row => Number.isInteger(row.pid);
const attention = row => row.recovery_required || row.state === "error" || row.lifecycle_error;
const state = row => attention(row) ? "recovery required" : row.state;
const busy = row => pending.has(row.id) || row.state === "transitioning";
const usable = row => connected && !busy(row);

function message(text, error = false) {
  clearTimeout(messageTimer);
  $("message").textContent = text; $("message").classList.toggle("error", error); $("message").hidden = false;
  if (!error) messageTimer = setTimeout(() => { $("message").hidden = true; }, 8000);
}
function setConnection(value) {
  connected = value;
  $("connection").className = `connection ${value ? "live" : "down"}`;
  $("connection-label").textContent = value ? "live · 2s" : "reconnecting";
  $("connection-note").hidden = value;
}

function chart(id, history, columns, colors, format, minimum, labels, valueFormat, threshold = null) {
  const target = $(id);
  let graph = charts.get(id);
  if (!graph) {
    const canvas = document.createElement("canvas"), tooltip = document.createElement("div");
    canvas.id = `${id}-canvas`; canvas.tabIndex = 0; canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `${target.getAttribute("aria-label")}. Hover or use Left and Right to inspect samples.`);
    tooltip.id = `${id}-tooltip`; tooltip.className = "chart-tooltip"; tooltip.setAttribute("role", "tooltip"); tooltip.hidden = true;
    canvas.setAttribute("aria-describedby", tooltip.id); target.replaceChildren(canvas, tooltip);
    graph = {target, canvas, tooltip, mode:null, timestamp:null, pointerX:null}; charts.set(id, graph);
    canvas.addEventListener("pointermove", event => {
      graph.pointerX = event.clientX - canvas.getBoundingClientRect().left;
      graph.mode = "pointer"; chooseChartSample(graph); drawChart(graph);
    });
    canvas.addEventListener("pointerleave", () => { if (graph.mode === "pointer") { graph.mode = null; drawChart(graph); } });
    canvas.addEventListener("focus", () => {
      graph.mode = "keyboard"; graph.timestamp = graph.history?.at(-1)?.[0]; drawChart(graph);
    });
    canvas.addEventListener("blur", () => { if (graph.mode === "keyboard") { graph.mode = null; drawChart(graph); } });
    canvas.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End", "Escape"].includes(event.key) || !graph.history.length) return;
      event.preventDefault();
      if (event.key === "Escape") { graph.mode = null; drawChart(graph); return; }
      const index = nearestSample(graph.history, graph.timestamp ?? graph.history.at(-1)[0]);
      const next = event.key === "Home" ? 0 : event.key === "End" ? graph.history.length - 1 : Math.max(0, Math.min(graph.history.length - 1, index + (event.key === "ArrowLeft" ? -1 : 1)));
      graph.mode = "keyboard"; graph.timestamp = graph.history[next][0]; drawChart(graph);
    });
  }
  Object.assign(graph, {history, columns, colors, format, minimum, labels, valueFormat, threshold});
  drawChart(graph);
}

function nearestSample(history, timestamp) {
  let best = 0;
  for (let i = 1; i < history.length; i++) if (Math.abs(history[i][0] - timestamp) < Math.abs(history[best][0] - timestamp)) best = i;
  return best;
}
function chooseChartSample(graph) {
  if (!graph.history?.length) return;
  const first = graph.history[0][0], span = Math.max(30, graph.history.at(-1)[0] - first);
  const timestamp = first + Math.max(0, Math.min(1, (graph.pointerX - 43) / Math.max(1, graph.target.clientWidth - 51))) * span;
  graph.timestamp = graph.history[nearestSample(graph.history, timestamp)][0];
}
function drawChart(graph) {
  const {target, canvas, tooltip, history, columns, colors, format, minimum, labels, valueFormat, threshold} = graph;
  if (!history) return;
  const width = Math.max(1, target.clientWidth), height = 132, left = 43, right = width - 8, top = 7, bottom = 111;
  const ratio = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(width * ratio)) canvas.width = Math.round(width * ratio);
  if (canvas.height !== Math.round(height * ratio)) canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d"); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0,0,width,height);
  if (!history.length) { tooltip.hidden = true; return; }
  const palette = getComputedStyle(document.documentElement);
  const color = value => value.startsWith("var(") ? palette.getPropertyValue(value.slice(4, -1)).trim() : value;
  const first = history[0][0], last = history[history.length - 1][0], span = Math.max(30, last - first);
  const max = Math.max(minimum, ...history.flatMap(point => [...columns.map(column => point[column] ?? 0), threshold ? threshold(point) : 0])) * 1.1;
  const x = time => left + (time - first) / span * (right - left);
  const y = value => bottom - value / max * (bottom - top);
  ctx.font = "9px ui-monospace, Menlo, monospace"; ctx.textAlign = "right";
  for (let i = 0; i < 3; i++) {
    const value = max * i / 2, position = y(value);
    ctx.strokeStyle = color("var(--grid)"); ctx.lineWidth = 1; ctx.beginPath();
    ctx.moveTo(left, position); ctx.lineTo(right, position); ctx.stroke();
    ctx.fillStyle = color("var(--muted)"); ctx.fillText(format(value), left - 7, position + 3);
  }
  if (threshold) {
    ctx.strokeStyle = color("var(--accent)"); ctx.lineWidth = 1.6; ctx.setLineDash([4,4]);
    ctx.beginPath(); history.forEach((point, i) => { if (i) ctx.lineTo(x(point[0]), y(threshold(point))); else ctx.moveTo(x(point[0]), y(threshold(point))); }); ctx.stroke(); ctx.setLineDash([]);
  }
  columns.forEach((column, index) => {
    ctx.strokeStyle = color(colors[index]); ctx.fillStyle = color(colors[index]); ctx.lineWidth = 1.6; ctx.beginPath();
    history.forEach((point, i) => { if (i) ctx.lineTo(x(point[0]), y(point[column] ?? 0)); else ctx.moveTo(x(point[0]), y(point[column] ?? 0)); });
    ctx.stroke(); const point = history[history.length - 1];
    ctx.beginPath(); ctx.arc(x(point[0]), y(point[column] ?? 0), 2, 0, 2 * Math.PI); ctx.fill();
  });
  ctx.fillStyle = color("var(--muted)"); ctx.textAlign = "left";
  ctx.fillText(new Date(first * 1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}), left, 129);
  ctx.textAlign = "right"; ctx.fillText("now", right, 129);
  if (!graph.mode) { tooltip.hidden = true; return; }
  if (graph.mode === "pointer") chooseChartSample(graph);
  const point = history[nearestSample(history, graph.timestamp ?? last)], position = x(point[0]);
  graph.timestamp = point[0];
  ctx.strokeStyle = color("var(--muted)"); ctx.lineWidth = 1; ctx.setLineDash([2,3]);
  ctx.beginPath(); ctx.moveTo(position, top); ctx.lineTo(position, bottom); ctx.stroke(); ctx.setLineDash([]);
  const readings = columns.map((column, i) => ({label:labels[i],value:point[column] ?? 0,color:colors[i]}));
  if (threshold) readings.push({label:"next start requires",value:threshold(point),color:"var(--accent)"});
  readings.forEach(reading => { ctx.fillStyle = color(reading.color); ctx.beginPath(); ctx.arc(position, y(reading.value), 3, 0, 2*Math.PI); ctx.fill(); });
  tooltip.innerHTML = `<time datetime="${new Date(point[0]*1000).toISOString()}">${escapeHTML(new Date(point[0]*1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"}))}</time>` + readings.map(reading => `<div class="reading"><i class="swatch"></i><span class="reading-label">${escapeHTML(reading.label)}</span><b>${escapeHTML(valueFormat(reading.value))}</b></div>`).join("");
  tooltip.querySelectorAll(".swatch").forEach((dot, i) => { dot.style.backgroundColor = color(readings[i].color); });
  tooltip.hidden = false;
  const tooltipWidth = tooltip.offsetWidth;
  tooltip.style.left = `${Math.max(4, Math.min(width - tooltipWidth - 4, position + tooltipWidth + 12 < width ? position + 12 : position - tooltipWidth - 12))}px`;
}

function selectWorkspace(id, reveal = false) {
  if (!data?.workspaces.some(row => row.id === id)) return;
  selected = id; render();
  if (reveal) {
    const panel = $("inspector-panel"); panel.focus({preventScroll:true});
    panel.scrollIntoView({block:"start", behavior:"instant"});
  }
}

function renderWorkspaces(rows) {
  const body = $("workspaces"), ids = new Set(rows.map(row => row.id));
  for (const child of [...body.children]) if (!ids.has(child.dataset.workspace)) child.remove();
  rows.forEach((row, index) => {
    let element = workspaceRows.get(row.id);
    if (!element) {
      element = document.createElement("tr"); element.dataset.workspace = row.id;
      element.innerHTML = `<td><button class="workspace-name" id="workspace-${row.id}" data-inspect="${row.id}" aria-controls="inspector-panel"></button><div class="sub">SWE · slot ${row.slot}</div></td><td><span class="badge"></span></td><td><span class="writer"></span><div class="sub leases"></div></td><td class="n rss"></td><td class="n wake"></td><td class="n checkpoint"></td><td class="n"><div class="actions"><button class="primary" id="primary-${row.id}" data-id="${row.id}"></button><button class="inspect" id="inspect-${row.id}" data-inspect="${row.id}" aria-label="View details for ${row.id}" aria-controls="inspector-panel">Details</button></div></td>`;
      workspaceRows.set(row.id, element);
    }
    element.classList.toggle("selected", row.id === selected);
    const name = element.querySelector(".workspace-name"); name.textContent = row.id; name.setAttribute("aria-pressed", row.id === selected);
    const badge = element.querySelector(".badge");
    badge.className = `badge ${attention(row) ? "attention" : awake(row) ? "running" : row.state}`;
    badge.textContent = `${attention(row) ? "!" : awake(row) ? "●" : row.state === "transitioning" ? "◐" : "◌"} ${state(row)}`;
    const writer = element.querySelector(".writer"); writer.textContent = row.writer_connected ? "attached" : "detached"; writer.classList.toggle("dim", !row.writer_connected);
    element.querySelector(".leases").textContent = `${row.active_operations} leases${row.queued_operations ? ` · ${row.queued_operations} queued` : ""}`;
    element.querySelector(".rss").textContent = awake(row) ? bytes(row.rss_bytes) : "—";
    element.querySelector(".wake").textContent = latency(row.last_wake_ms);
    element.querySelector(".checkpoint").textContent = bytes(row.checkpoint_bytes);
    const control = element.querySelector(".primary");
    control.dataset.action = awake(row) ? "suspend" : "resume";
    control.textContent = pending.has(row.id) ? "working…" : awake(row) ? "Suspend" : "Resume";
    control.disabled = !usable(row) || row.active_operations > 0 || row.queued_operations > 0 || attention(row);
    element.querySelector(".inspect").setAttribute("aria-pressed", row.id === selected);
    if (body.children[index] !== element) body.insertBefore(element, body.children[index] ?? null);
  });
  if (!rows.length) body.innerHTML = `<tr><td colspan="7" class="empty">${data.workspaces.length ? "No workspaces match this filter." : "No workspaces allocated. Create a workspace to begin."}</td></tr>`;
  for (const id of workspaceRows.keys()) if (!data.workspaces.some(row => row.id === id)) workspaceRows.delete(id);
}

function renderInspector(rows) {
  const picker = $("workspace-select"), choices = JSON.stringify(rows.map(row => row.id));
  if (picker.dataset.choices !== choices) {
    picker.innerHTML = rows.map(row => `<option value="${row.id}">${escapeHTML(row.id)}</option>`).join("") || '<option value="">No workspaces</option>';
    picker.dataset.choices = choices;
  }
  if (picker.value !== selected) picker.value = selected ?? "";
  picker.disabled = !rows.length;
  const row = rows.find(row => row.id === selected);
  $("inspector").hidden = !row; $("inspector-empty").hidden = !!row;
  if (!row) return;
  $("detail-state").textContent = `${state(row)} · ${row.writer_connected ? "attached" : "detached"}`;
  $("detail-memory").textContent = `${awake(row) ? bytes(row.rss_bytes) : "no VMM process"} / ${bytes(row.memory_bound_bytes)}`;
  $("detail-disk").textContent = `${bytes(row.disk_allocated_bytes)} / ${bytes(row.disk_bytes)}`;
  $("disk-usage").style.width = `${row.disk_bytes ? Math.min(100, row.disk_allocated_bytes / row.disk_bytes * 100) : 0}%`;
  $("detail-snapshot").textContent = `${bytes(row.checkpoint_bytes)} / ${latency(row.last_suspend_ms)}`;
  $("detail-address").textContent = row.guest_host;
  $("detail-idle").textContent = !awake(row) ? "not running" : data.idle_seconds <= 0 ? "disabled" : row.active_operations ? "held by execution lease" : `in ${duration(Math.max(0, data.idle_seconds - row.idle_elapsed_seconds))}`;
  $("detail-image").textContent = (row.runner ?? "").split("/").pop().replace(/^[a-z0-9]{32}-/, "");
  $("detail-wake").textContent = `${latency(row.last_wake_ms)} / ${latency(row.last_restore_api_ms)}`;
  $("launch-label").textContent = `Start an agent · run this on ${data.hostname}`;
  const command = `environment-codex ${row.id}`; if ($("launch-command").value !== command) $("launch-command").value = command;
  for (const id of ["control-toggle", "control-shutdown", "control-recover"]) $(id).dataset.id = row.id;
  $("control-toggle").dataset.action = awake(row) ? "suspend" : "resume";
  $("control-toggle").textContent = pending.has(row.id) ? "working…" : awake(row) ? "Suspend" : "Resume";
  $("control-toggle").disabled = !usable(row) || row.active_operations > 0 || row.queued_operations > 0 || attention(row);
  const destructiveDisabled = !usable(row) || row.active_operations > 0 || row.queued_operations > 0 || row.writer_connected;
  $("control-shutdown").disabled = destructiveDisabled || attention(row);
  $("control-recover").disabled = destructiveDisabled; $("control-recover").hidden = !attention(row);
  $("detail-error").textContent = row.lifecycle_error || row.error || ""; $("detail-error").hidden = !$("detail-error").textContent;
}

function render() {
  if (!data) return;
  const rows = data.workspaces, running = rows.filter(awake).length;
  const leases = rows.reduce((sum, row) => sum + row.active_operations, 0), queued = rows.reduce((sum, row) => sum + row.queued_operations, 0);
  selected = rows.some(row => row.id === selected) ? selected : rows[0]?.id;
  $("host").textContent = `agent@${data.hostname}`;
  document.title = `environments · ${data.hostname}`;
  $("service-version").textContent = `rust ${data.version}`;
  $("idle-policy").textContent = data.idle_seconds > 0 ? `idle suspension ${duration(data.idle_seconds)}` : "idle suspension disabled";
  $("reserve-policy").textContent = `host reserve ${bytes(data.host_reserve_bytes)}`;
  $("uptime").textContent = `up ${duration(data.uptime_seconds)}`;
  $("workspace-count").innerHTML = `${rows.length}<small> / ${data.slots}</small>`;
  $("slot-count").textContent = `${data.slots - rows.length} available slots`;
  $("awake-count").textContent = running;
  $("sleep-count").textContent = `${rows.filter(row => row.state === "suspended").length} suspended`;
  $("lease-count").textContent = leases;
  $("harness-count").textContent = `${rows.filter(row => row.writer_connected).length} attached harnesses`;
  $("queue-count").textContent = queued;
  $("service-memory").textContent = bytes(data.service.pss_bytes);
  $("host-memory").textContent = bytes(data.host.available_bytes);
  $("host-total").textContent = `${bytes(data.host.total_bytes)} total RAM`;
  $("attention-count").textContent = rows.filter(attention).length;
  $("slots-note").textContent = rows.length >= data.slots ? "all configured slots allocated" : `${data.slots - rows.length} free slots`;
  $("new-workspace").disabled = !connected || rows.length >= data.slots;
  const visible = rows.filter(row => filter === "all" || (filter === "awake" && awake(row)) || (filter === "suspended" && row.state === "suspended") || (filter === "attention" && attention(row)));
  renderWorkspaces(visible);
  $("legend-awake").textContent = running; $("legend-leases").textContent = leases; $("legend-queued").textContent = queued;
  const required = data.host_reserve_bytes + (running + 1) * 1024 ** 3;
  $("legend-available").textContent = bytes(data.host.available_bytes); $("legend-required").textContent = bytes(required);
  chart("execution-chart", data.history, [1,2,3], ["var(--blue)","var(--green)","var(--accent)"], value => Math.round(value), Math.max(1, data.slots), ["awake", "execution leases", "queued"], value => String(value));
  chart("memory-chart", data.history, [6], ["var(--blue)"], value => `${(value / 1024 ** 3).toFixed(0)}G`, data.host_reserve_bytes, ["host available"], value => `${(value / 1024 ** 3).toFixed(2)} GiB`, point => data.host_reserve_bytes + (point[1] + 1) * 1024 ** 3);
  const sampleSpan = data.history.length ? data.sampled_at - data.history[0][0] : 0;
  $("history-label").textContent = `last ${duration(sampleSpan)} · samples every 2s`;
  renderInspector(rows);
  const logScroll = $("activity").scrollTop;
  $("activity").innerHTML = data.recent_operations.map(operation => {
    const time = operation.created ? new Date(operation.created * 1000).toLocaleTimeString("en-GB", {hour:"2-digit", minute:"2-digit", second:"2-digit", hourCycle:"h23"}) : "—";
    const result = operation.state === "completed" && operation.method === "process/start" ? "started" : operation.state;
    return `<li><span class="time" title="${escapeHTML(operation.created ? new Date(operation.created * 1000).toLocaleString() : "")}">${escapeHTML(time)}</span><span class="operation-status ${escapeHTML(operation.state)}" role="img" aria-label="${escapeHTML(operation.state)}">${operation.state === "completed" ? "●" : operation.state === "pending" ? "◐" : "!"}</span><span class="method"><span class="workspace">${escapeHTML(operation.workspace)}</span><span class="method-name">${escapeHTML(operation.method)}</span></span><span class="result">${escapeHTML(result)}</span></li>`;
  }).join("") || '<li class="empty">No requests recorded.</li>';
  $("footer-cpu").textContent = `self ${(data.service.cpu_percent ?? 0).toFixed(1)}% cpu`;
  $("footer-rss").textContent = `${bytes(data.service.rss_bytes)} RSS`;
  $("activity").scrollTop = logScroll;
}

async function post(path, body = {}) {
  const response = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json", "X-Environment-UI":"1"}, body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error ?? `Request failed (${response.status})`);
  return result;
}
async function perform(id, action) {
  if (!connected || pending.has(id)) return;
  pending.set(id, action); render();
  try {
    await post(`/api/workspaces/${encodeURIComponent(id)}/${action}`);
    const response = await fetch("/api/dashboard"); if (response.ok) data = await response.json();
    message(`${id}: ${action === "suspend" ? "checkpoint saved; VM suspended" : action === "resume" ? "guest ready" : action === "shutdown" ? "guest shut down; saved files retained" : "saved files recovered in a new session"}.`);
  } catch (error) { message(`${id}: ${error.message}`, true); }
  finally { pending.delete(id); render(); }
}
function confirm(id, action) {
  confirmation = {id, action};
  $("confirm-title").textContent = `${action === "shutdown" ? "Shut down" : "Recover"} ${id}?`;
  $("confirm-description").textContent = action === "shutdown" ? "Shut down the guest and keep its saved files. This discards the memory checkpoint and current executor session." : "Cold boot the saved workspace files. The previous memory session cannot be recovered. Unknown requests will not be repeated.";
  $("confirm-submit").textContent = action === "shutdown" ? "Shut down" : "Recover saved files";
  $("confirm-dialog").showModal();
}
document.addEventListener("click", async event => {
  const target = event.target.closest("button"); if (!target || target.disabled) return;
  if (target.dataset.filter) {
    filter = target.dataset.filter;
    document.querySelectorAll("[data-filter]").forEach(button => { const on = button === target; button.classList.toggle("on", on); button.setAttribute("aria-selected", on); }); render();
  } else if (target.dataset.inspect) { selectWorkspace(target.dataset.inspect, true); }
  else if (target.dataset.action) { const {id, action} = target.dataset; if (["shutdown", "recover"].includes(action)) confirm(id, action); else perform(id, action); }
  else if (target.dataset.close) $(target.dataset.close).close();
  else if (target.id === "new-workspace") { $("create-error").hidden = true; $("workspace-id").value = ""; $("create-dialog").showModal(); }
  else if (target.id === "copy-command") {
    const input = $("launch-command");
    try { if (navigator.clipboard) await navigator.clipboard.writeText(input.value); else { input.select(); if (!document.execCommand("copy")) throw new Error("copy unavailable"); } message("Host launcher command copied."); }
    catch { input.select(); message("Select and copy the host launcher command."); }
  }
});
$("workspace-select").addEventListener("change", event => selectWorkspace(event.target.value));
$("confirm-form").addEventListener("submit", event => { event.preventDefault(); $("confirm-dialog").close(); if (confirmation) perform(confirmation.id, confirmation.action); });
$("create-form").addEventListener("submit", async event => {
  event.preventDefault(); $("create-submit").disabled = true; $("create-error").hidden = true;
  try { const result = await post("/api/workspaces", {id:$("workspace-id").value}); selected = result.id; $("create-dialog").close(); message(`${result.id}: workspace allocated; VM remains asleep.`); }
  catch (error) { $("create-error").textContent = error.message; $("create-error").hidden = false; }
  finally { $("create-submit").disabled = false; }
});
function connect() {
  source?.close(); if (document.hidden) return;
  source = new EventSource("/events");
  source.addEventListener("snapshot", event => { try { data = JSON.parse(event.data); setConnection(true); render(); } catch (error) { message(`Dashboard update failed: ${error.message}`, true); } });
  source.onerror = () => { setConnection(false); render(); };
}
document.addEventListener("visibilitychange", () => { if (document.hidden) { source?.close(); source = null; } else connect(); });
window.addEventListener("resize", () => { if (data) render(); });
setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString(); }, 1000);
$("port").textContent = `:${location.port || "80"}`;
$("vitals-link").href = `http://${location.hostname}:5400`;
connect();
