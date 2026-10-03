// Evaluate this expression in the loaded dashboard's browser context.
// These checks use read-only dashboard data and do not start or suspend guests.
(async () => {
  const checks = [];
  const assert = (condition, name) => {
    if (!condition) throw new Error(name);
    checks.push(name);
  };
  const get = id => document.getElementById(id);
  const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  const response = await fetch("/api/dashboard");
  const snapshot = await response.json();
  assert(snapshot.workspaces.length > 0, "A workspace is available for inspection");
  assert(Array.from(get("workspace-profile").options, option => option.value).join(",") === snapshot.profiles.join(","), "Create dialog offers every configured profile");
  const id = snapshot.workspaces.at(-1).id;
  get(`inspect-${id}`).click();
  await wait(1000);
  const panel = get("inspector-panel").getBoundingClientRect();
  assert(panel.top >= 0 && panel.bottom <= innerHeight - 20, "Details brings the whole panel into view");
  assert(document.activeElement === get("inspector-panel"), "Details focuses the panel");
  assert(get("workspace-select").value === id, "Details selects the requested workspace");
  const selected = snapshot.workspaces[0].id;
  get("workspace-select").value = selected;
  get("workspace-select").dispatchEvent(new Event("change", {bubbles:true}));
  assert(get("launch-command").value === `environment-codex ${selected}`, "Workspace picker changes the launcher command");
  assert(["control-toggle", "control-shutdown", "control-recover"].every(control => get(control).dataset.id === selected), "Selected workspace owns every details control");
  assert(get("detail-profile").textContent === snapshot.workspaces.find(row => row.id === selected).profile, "Details shows the bound workspace profile");
  if (snapshot.memory_overcommit) {
    assert(get("admission-label").textContent.includes("startup headroom"), "Memory chart describes the overcommit admission policy");
    assert(snapshot.startup_required_bytes === snapshot.host_reserve_bytes + snapshot.startup_headroom_bytes, "Overcommit threshold uses reserve plus startup headroom");
  }
  const ids = ["workspace-select", "launch-command", "control-toggle", `inspect-${id}`, "execution-chart-canvas", "memory-chart-canvas"];
  const nodes = ids.map(get);
  window.scrollTo(0, 0);
  for (const name of ["execution-chart", "memory-chart"]) {
    const canvas = get(`${name}-canvas`), rect = canvas.getBoundingClientRect();
    canvas.dispatchEvent(new PointerEvent("pointermove", {clientX:rect.left + rect.width / 2, clientY:rect.top + 30, bubbles:true}));
    const tooltip = get(`${name}-tooltip`);
    assert(!tooltip.hidden, `${name}: hover shows a tooltip`);
    assert(tooltip.querySelector("time").dateTime.length > 0, `${name}: tooltip has a sample timestamp`);
    assert(tooltip.querySelectorAll(".reading").length === (name === "execution-chart" ? 3 : 2), `${name}: tooltip includes every series`);
  }
  await wait(4500);
  const updated = await (await fetch("/api/dashboard")).json();
  assert(updated.sampled_at > snapshot.sampled_at, "Live samples advance during interaction");
  assert(ids.every((id, i) => get(id) === nodes[i]), "Live updates preserve interactive DOM nodes");
  assert(get("workspace-select").value === selected, "Live updates preserve workspace selection");
  assert(!get("execution-chart-tooltip").hidden && !get("memory-chart-tooltip").hidden, "Live updates preserve hover tooltips");
  const canvas = get("execution-chart-canvas"), tooltip = get("execution-chart-tooltip");
  canvas.dispatchEvent(new PointerEvent("pointerleave"));
  assert(tooltip.hidden, "Pointer leave hides the tooltip");
  canvas.focus();
  canvas.dispatchEvent(new KeyboardEvent("keydown", {key:"Home", bubbles:true}));
  const first = tooltip.querySelector("time").dateTime;
  canvas.dispatchEvent(new KeyboardEvent("keydown", {key:"End", bubbles:true}));
  const last = tooltip.querySelector("time").dateTime;
  assert(Date.parse(last) >= Date.parse(first), "Home and End inspect the history range");
  if (Date.parse(last) > Date.parse(first)) {
    canvas.dispatchEvent(new KeyboardEvent("keydown", {key:"ArrowLeft", bubbles:true}));
    assert(Date.parse(tooltip.querySelector("time").dateTime) < Date.parse(last), "Left inspects the previous sample");
  }
  canvas.dispatchEvent(new KeyboardEvent("keydown", {key:"Escape", bubbles:true}));
  assert(tooltip.hidden, "Escape hides the keyboard tooltip");
  get("memory-chart-canvas").dispatchEvent(new PointerEvent("pointerleave"));
  const logRows = [...get("activity").children].filter(row => row.querySelector(".time"));
  assert(logRows.every(row => {
    const time = row.querySelector(".time").getBoundingClientRect();
    const status = row.querySelector(".operation-status").getBoundingClientRect();
    const method = row.querySelector(".method").getBoundingClientRect();
    return time.right < status.left && status.right < method.left;
  }), "Request timestamps, status indicators, and methods do not overlap");
  assert(logRows.every(row => /^\d{2}:\d{2}:\d{2}$/.test(row.querySelector(".time").textContent)), "Request timestamps use an unbroken 24-hour format");
  return {passed:checks.length, checks, viewport:{width:innerWidth,height:innerHeight}};
})()
