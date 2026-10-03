"use strict";
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let data = null, selected = null, filter = "all", connected = false, source = null, confirmation = null;
const pending = new Map();
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

function message(text, error = false) { $("message").textContent = text; $("message").classList.toggle("error", error); $("message").hidden = false; }
function setConnection(value) {
  connected = value;
  $("connection").className = `connection ${value ? "live" : "down"}`;
  $("connection-label").textContent = value ? "live · 2s" : "reconnecting";
  $("connection-note").hidden = value;
}

function chart(id, history, columns, colors, format, minimum = 1, threshold = null) {
  const target = $(id);
  if (!history.length) { target.innerHTML = '<div class="chart-empty">Collecting live samples…</div>'; return; }
  const width = Math.max(280, target.clientWidth), height = 132, left = 43, right = width - 8, top = 7, bottom = 111;
  const canvas = document.createElement("canvas"), ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
  canvas.setAttribute("aria-hidden", "true"); target.replaceChildren(canvas);
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
  const palette = getComputedStyle(document.documentElement);
  const color = value => value.startsWith("var(") ? palette.getPropertyValue(value.slice(4, -1)).trim() : value;
  const first = history[0][0], last = history[history.length - 1][0], span = Math.max(30, last - first);
  const max = Math.max(minimum, threshold ?? 0, ...history.flatMap(point => columns.map(column => point[column] ?? 0))) * 1.1;
  const x = time => left + (time - first) / span * (right - left);
  const y = value => bottom - value / max * (bottom - top);
  ctx.font = "9px ui-monospace, Menlo, monospace"; ctx.textAlign = "right";
  for (let i = 0; i < 3; i++) {
    const value = max * i / 2, position = y(value);
    ctx.strokeStyle = color("var(--grid)"); ctx.lineWidth = 1; ctx.beginPath();
    ctx.moveTo(left, position); ctx.lineTo(right, position); ctx.stroke();
    ctx.fillStyle = color("var(--muted)"); ctx.fillText(format(value), left - 7, position + 3);
  }
  if (threshold != null) {
    ctx.strokeStyle = color("var(--accent)"); ctx.lineWidth = 1.6; ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(left, y(threshold)); ctx.lineTo(right, y(threshold)); ctx.stroke(); ctx.setLineDash([]);
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
}

function render() {
  if (!data) return;
  const focus = document.activeElement?.id, rows = data.workspaces, running = rows.filter(awake).length;
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
  $("workspaces").innerHTML = visible.map(row => {
    const disabled = !usable(row) || row.active_operations > 0 || row.queued_operations > 0 || attention(row);
    const action = awake(row) ? "suspend" : "resume";
    const statusClass = attention(row) ? "attention" : awake(row) ? "running" : row.state;
    const glyph = attention(row) ? "!" : awake(row) ? "●" : row.state === "transitioning" ? "◐" : "◌";
    return `<tr class="${row.id === selected ? "selected" : ""}"><td><span class="workspace-name">${escapeHTML(row.id)}</span><div class="sub">SWE · slot ${row.slot}</div></td><td><span class="badge ${escapeHTML(statusClass)}">${glyph} ${escapeHTML(state(row))}</span></td><td>${row.writer_connected ? "attached" : '<span class="dim">detached</span>'}<div class="sub">${row.active_operations} leases${row.queued_operations ? ` · ${row.queued_operations} queued` : ""}</div></td><td class="n">${awake(row) ? bytes(row.rss_bytes) : "—"}</td><td class="n">${latency(row.last_wake_ms)}</td><td class="n">${bytes(row.checkpoint_bytes)}</td><td class="n"><div class="actions"><button class="primary" id="primary-${row.id}" data-action="${action}" data-id="${row.id}" ${disabled ? "disabled" : ""}>${pending.has(row.id) ? "working…" : action === "resume" ? "Resume" : "Suspend"}</button><button class="inspect" id="inspect-${row.id}" data-inspect="${row.id}" aria-label="Inspect ${row.id}">⋯</button></div></td></tr>`;
  }).join("") || `<tr><td colspan="7" class="empty">${rows.length ? "No workspaces match this filter." : "No workspaces allocated. Create a workspace to begin."}</td></tr>`;
  $("legend-awake").textContent = running; $("legend-leases").textContent = leases; $("legend-queued").textContent = queued;
  const required = data.host_reserve_bytes + (running + 1) * 1024 ** 3;
  $("legend-available").textContent = bytes(data.host.available_bytes); $("legend-required").textContent = bytes(required);
  chart("execution-chart", data.history, [1,2,3], ["var(--blue)","var(--green)","var(--accent)"], value => Math.round(value), Math.max(1, data.slots));
  chart("memory-chart", data.history, [6], ["var(--blue)"], value => `${(value / 1024 ** 3).toFixed(0)}G`, data.host_reserve_bytes, required);
  const sampleSpan = data.history.length ? data.sampled_at - data.history[0][0] : 0;
  $("history-label").textContent = `last ${duration(sampleSpan)} · samples every 2s`;
  const row = rows.find(row => row.id === selected);
  $("inspector-id").textContent = row?.id ?? "select a workspace";
  if (row) {
    const image = (row.runner ?? "").split("/").pop().replace(/^[a-z0-9]{32}-/, "");
    const controlsDisabled = !usable(row) || row.active_operations > 0 || row.queued_operations > 0 || row.writer_connected;
    const diskPercent = row.disk_bytes ? Math.min(100, row.disk_allocated_bytes / row.disk_bytes * 100) : 0;
    const idleLeft = Math.max(0, data.idle_seconds - row.idle_elapsed_seconds);
    $("inspector").className = "";
    $("inspector").innerHTML = `<div class="detail-grid"><div><div class="k">state / session</div><div class="v">${escapeHTML(state(row))} · ${row.writer_connected ? "attached" : "detached"}</div></div><div><div class="k">guest memory / admission bound</div><div class="v">${awake(row) ? bytes(row.rss_bytes) : "no VMM process"} / ${bytes(row.memory_bound_bytes)}</div></div><div><div class="k">persistent disk · actual / logical</div><div class="v">${bytes(row.disk_allocated_bytes)} / ${bytes(row.disk_bytes)}</div><div class="disk-track"><i id="disk-usage"></i></div></div><div><div class="k">snapshot files / last snapshot</div><div class="v">${bytes(row.checkpoint_bytes)} / ${latency(row.last_suspend_ms)}</div></div><div><div class="k">guest address</div><div class="v">${escapeHTML(row.guest_host)}</div></div><div><div class="k">idle suspension</div><div class="v">${awake(row) ? data.idle_seconds <= 0 ? "disabled" : row.active_operations ? "held by execution lease" : `in ${duration(idleLeft)}` : "not running"}</div></div><div><div class="k">image</div><div class="v">${escapeHTML(image)}</div></div><div><div class="k">restore / Firecracker API</div><div class="v">${latency(row.last_wake_ms)} / ${latency(row.last_restore_api_ms)}</div></div></div><div class="launch"><label for="launch-command">Start an agent · run this on ${escapeHTML(data.hostname)}</label><div class="command"><input id="launch-command" readonly value="environment-codex ${row.id}" aria-label="Host launcher command"><button id="copy-command">Copy</button></div></div><div class="advanced"><button id="control-shutdown" data-action="shutdown" data-id="${row.id}" ${controlsDisabled || attention(row) ? "disabled" : ""}>Shut down</button>${attention(row) ? `<button class="danger" id="control-recover" data-action="recover" data-id="${row.id}" ${controlsDisabled ? "disabled" : ""}>Recover saved files</button>` : ""}<span class="dim">shutdown discards the memory session</span></div>${row.lifecycle_error || row.error ? `<p class="detail-error">${escapeHTML(row.lifecycle_error || row.error)}</p>` : ""}`;
    $("disk-usage").style.width = `${diskPercent}%`;
  } else { $("inspector").textContent = "Select a workspace to inspect its persistent disk and checkpoint."; }
  $("activity").innerHTML = data.recent_operations.map(operation => {
    const time = operation.created ? new Date(operation.created * 1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"}) : "—";
    const result = operation.state === "completed" && operation.method === "process/start" ? "started" : operation.state;
    return `<li><span class="time">${escapeHTML(time)}</span><span class="${escapeHTML(operation.state)}">${operation.state === "completed" ? "●" : operation.state === "pending" ? "◐" : "!"}</span><span class="method"><span class="workspace">${escapeHTML(operation.workspace)}</span>${escapeHTML(operation.method)}</span><span class="result">${escapeHTML(result)}</span></li>`;
  }).join("") || '<li class="empty">No requests recorded.</li>';
  $("footer-cpu").textContent = `self ${(data.service.cpu_percent ?? 0).toFixed(1)}% cpu`;
  $("footer-rss").textContent = `${bytes(data.service.rss_bytes)} RSS`;
  if (focus && $(focus) && !$(focus).disabled) $(focus).focus({preventScroll:true});
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
  } else if (target.dataset.inspect) { selected = target.dataset.inspect; render(); }
  else if (target.dataset.action) { const {id, action} = target.dataset; if (["shutdown", "recover"].includes(action)) confirm(id, action); else perform(id, action); }
  else if (target.dataset.close) $(target.dataset.close).close();
  else if (target.id === "new-workspace") { $("create-error").hidden = true; $("workspace-id").value = ""; $("create-dialog").showModal(); }
  else if (target.id === "copy-command") {
    const input = $("launch-command");
    try { if (navigator.clipboard) await navigator.clipboard.writeText(input.value); else { input.select(); if (!document.execCommand("copy")) throw new Error("copy unavailable"); } message("Host launcher command copied."); }
    catch { input.select(); message("Select and copy the host launcher command."); }
  }
});
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
