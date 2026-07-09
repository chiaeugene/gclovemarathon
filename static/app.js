// GC 520 Marathon Spin Wheel — frontend logic
// Vibrant modern palette — red/violet/pink/orange/blue/green, echoing the 5 station colors but saturated.
const COLORS = ["#ef233c","#8b5cf6","#f472b6","#fb923c","#38bdf8","#34d399","#fbbf24","#a78bfa","#ff4d6d","#22d3ee"];

let participants = [];
let prizesCache = { normal: [], special: [] };
let results = [];
let specialData = { gifts: [], awarded: [] }; // special wheel = a shared raffle, not per-person spins
let specialPool = []; // one ticket per remaining special spin, computed server-side
let raffles = {}; // generic named raffles (e.g. "graduation"): { id: {label, gifts, roster, awarded, pool} }
let selectedNo = null;
let currentWheel = "normal";
let spinning = false;
let autoPlaying = false;
let currentRotation = 0;
let lastPool = []; // pool used for the most recent draw, so we can map result -> wedge index

const el = (id) => document.getElementById(id);
const canvas = () => el("wheelCanvas");

// Any wheel tab that isn't "normal" or "special" is a generic named raffle.
function isGenericRaffle(wheel) { return wheel !== "normal" && wheel !== "special"; }

let toastTimer = null;
function showToast(msg, isError) {
  const t = el("toast");
  t.textContent = msg;
  t.classList.toggle("err", !!isError);
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 3200);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `${path} failed`);
  }
  return res.json();
}

async function loadState() {
  const data = await api("/api/state");
  participants = data.state.participants || [];
  prizesCache = data.prizes || { normal: [], special: [] };
  results = data.results || [];
  specialData = data.special || { gifts: [], awarded: [] };
  specialPool = data.specialPool || [];
  raffles = data.raffles || {};
  el("syncedAt").textContent = data.state.last_synced
    ? `synced ${new Date(data.state.last_synced).toLocaleTimeString()}`
    : "not synced yet";
  renderParticipantList();
  renderLog();
  updateTabs();
  updateSelectedPanel();
  drawWheel(computePool(currentWheel), true);
}

function remaining(p, wheel) {
  return p[`${wheel}_total`] - p[`${wheel}_used`];
}

function renderParticipantList() {
  const q = el("search").value.trim().toLowerCase();
  const list = el("participantList");
  list.innerHTML = "";
  const wonNos = new Set(specialData.awarded.map(a => a.no));
  participants
    .filter(p => p.name.toLowerCase().includes(q))
    .filter(p => p.normal_total > 0 || p.special_total > 0)
    .sort((a, b) => a.no - b.no)
    .forEach(p => {
      const normRem = remaining(p, "normal");
      const specialActive = p.special_total > 0 && !wonNos.has(p.no);
      const row = document.createElement("div");
      row.className = "participant-row" + (p.no === selectedNo ? " selected" : "")
        + ((normRem <= 0 && !specialActive) ? " done" : "");
      const specialBadge = p.special_total > 0
        ? (wonNos.has(p.no)
            ? `<span class="badge badge-special">🎁 won a gift</span>`
            : `<span class="badge badge-special">🎁 ${p.special_total} raffle ticket${p.special_total > 1 ? "s" : ""}</span>`)
        : "";
      row.innerHTML = `
        <div class="participant-name">#${p.no} ${p.name}</div>
        <div class="participant-badges">
          ${p.normal_total > 0 ? `<span class="badge badge-normal">${normRem}/${p.normal_total} normal</span>` : ""}
          ${specialBadge}
        </div>`;
      row.onclick = () => selectParticipant(p.no);
      list.appendChild(row);
    });
}

function selectParticipant(no) {
  selectedNo = no;
  renderParticipantList();
  updateSelectedPanel();
  if (currentWheel === "normal") {
    drawWheel(computePool(currentWheel), true);
  }
}

function updateSelectedPanel() {
  const panel = el("selectedPanel");

  if (currentWheel === "special") {
    renderRafflePanel(panel, { ...specialData, label: "Special Raffle" }, specialPool, "No special gifts configured yet — add some via Manage Prizes.");
    return;
  }
  if (isGenericRaffle(currentWheel)) {
    const r = raffles[currentWheel];
    if (!r) {
      panel.innerHTML = `<div class="selected-empty">This raffle isn't set up yet.</div>`;
      el("btnSpin").disabled = true; el("btnAuto").disabled = true; el("btnUndo").disabled = true;
      return;
    }
    renderRafflePanel(panel, r, r.pool, `No gifts configured yet for ${r.label} — add some via its manager.`);
    return;
  }

  const p = participants.find(x => x.no === selectedNo);
  if (!p) {
    panel.innerHTML = `<div class="selected-empty">Select a participant from the left to begin.</div>`;
    el("btnSpin").disabled = true;
    el("btnAuto").disabled = true;
    el("btnUndo").disabled = true;
    return;
  }
  const rem = remaining(p, currentWheel);
  panel.innerHTML = `
    <div class="selected-name">#${p.no} ${p.name}</div>
    <div class="selected-sub">${rem} ${currentWheel} spin${rem === 1 ? "" : "s"} remaining</div>`;
  const canSpin = rem > 0 && computePool(currentWheel).length > 0;
  el("btnSpin").disabled = !canSpin || spinning;
  el("btnAuto").disabled = !canSpin || spinning;
  el("btnUndo").disabled = !hasHistory(p.no) || spinning;
}

// Shared by the special wheel and every generic named raffle — same shape:
// {label?, gifts, awarded}, plus its current ticket pool.
function renderRafflePanel(panel, raffleObj, pool, emptyMsg) {
  const total = raffleObj.gifts.length;
  const done = raffleObj.awarded.length;
  const label = raffleObj.label ? `${raffleObj.label} — ` : "";
  if (total === 0) {
    panel.innerHTML = `<div class="selected-empty">${emptyMsg}</div>`;
  } else if (done >= total) {
    panel.innerHTML = `<div class="selected-name">🎉 All ${raffleObj.label ? raffleObj.label + " " : ""}gifts have been awarded!</div>`;
  } else {
    panel.innerHTML = `
      <div class="selected-name">${label}Gift ${done + 1} of ${total}</div>
      <div class="selected-sub">${raffleObj.gifts[done]}</div>`;
  }
  const canDraw = total > 0 && done < total && pool.length > 0;
  el("btnSpin").disabled = !canDraw || spinning;
  el("btnAuto").disabled = !canDraw || spinning;
  el("btnUndo").disabled = done === 0 || spinning;
}

function hasHistory(no) {
  return results.some(r => r.no === no);
}

function updateTabs() {
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.wheel === currentWheel);
    const wheel = btn.dataset.wheel;
    if (wheel === "normal") return;
    if (wheel === "special") { btn.disabled = !specialData.gifts || specialData.gifts.length === 0; return; }
    const r = raffles[wheel];
    btn.disabled = !r || !r.gifts || r.gifts.length === 0;
  });

  el("btnSpin").textContent = currentWheel === "normal" ? "SPIN" : "DRAW WINNER";
  if (!autoPlaying) {
    el("btnAuto").textContent = currentWheel === "normal" ? "Auto-Play Remaining" : "Draw All Remaining";
  }
}

function computePool(wheel) {
  if (wheel === "normal") return (prizesCache.normal || []).filter(p => p.qty > 0);
  if (wheel === "special") return specialPool;
  return raffles[wheel] ? raffles[wheel].pool : [];
}

// ---------------------------------------------------------------- canvas --
function drawWheel(pool, resetRotation) {
  lastPool = pool;
  const cv = canvas();
  const ctx = cv.getContext("2d");
  const w = cv.width, h = cv.height, r = w / 2 - 8;
  ctx.clearRect(0, 0, w, h);
  if (pool.length === 0) {
    ctx.fillStyle = "#241f33";
    ctx.beginPath(); ctx.arc(w/2, h/2, r, 0, Math.PI*2); ctx.fill();
    ctx.fillStyle = "#a8a3c2"; ctx.font = "bold 18px 'Space Grotesk', Arial"; ctx.textAlign = "center";
    ctx.fillText(currentWheel === "normal" ? "No prizes left" : "No one left", w/2, h/2);
  } else {
    const n = pool.length;
    const wedge = (Math.PI * 2) / n;
    for (let i = 0; i < n; i++) {
      const start = -Math.PI/2 + i * wedge;
      const end = start + wedge;
      ctx.beginPath();
      ctx.moveTo(w/2, h/2);
      ctx.arc(w/2, h/2, r, start, end);
      ctx.closePath();
      ctx.fillStyle = COLORS[i % COLORS.length];
      ctx.fill();
      ctx.strokeStyle = "rgba(255,255,255,.85)"; ctx.lineWidth = 3; ctx.stroke();

      ctx.save();
      ctx.translate(w/2, h/2);
      ctx.rotate(start + wedge/2);
      ctx.textAlign = "right";
      ctx.fillStyle = "#fff";
      ctx.font = "800 19px 'Space Grotesk', Arial";
      ctx.shadowColor = "rgba(0,0,0,.5)"; ctx.shadowBlur = 5;
      ctx.fillText(pool[i].name, r - 22, 6, r - 70);
      ctx.restore();
    }
    // glossy dome highlight for extra pop
    const gloss = ctx.createRadialGradient(w/2, h*0.28, r*0.05, w/2, h*0.35, r*1.05);
    gloss.addColorStop(0, "rgba(255,255,255,.28)");
    gloss.addColorStop(0.5, "rgba(255,255,255,.06)");
    gloss.addColorStop(1, "rgba(255,255,255,0)");
    ctx.fillStyle = gloss;
    ctx.beginPath(); ctx.arc(w/2, h/2, r, 0, Math.PI*2); ctx.fill();
  }
  if (resetRotation) {
    cv.style.transition = "none";
    currentRotation = 0;
    cv.style.transform = "rotate(0deg)";
    // force reflow so the next transition re-applies
    void cv.offsetWidth;
    cv.style.transition = "";
  }
}

function angleForIndex(index, n) {
  const wedge = 360 / n;
  return index * wedge + wedge / 2; // degrees clockwise from top
}

function spinToIndex(index, n, fast) {
  const cv = canvas();
  const required = (360 - angleForIndex(index, n)) % 360;
  const spins = fast ? 3 : (5 + Math.floor(Math.random() * 2));
  let base = Math.ceil(currentRotation / 360) * 360;
  let target = base + spins * 360 + required;
  while (target <= currentRotation) target += 360;
  const duration = fast ? 1100 : 3600;
  cv.style.transition = `transform ${duration}ms cubic-bezier(.17,.67,.24,1)`;
  cv.style.transform = `rotate(${target}deg)`;
  currentRotation = target;
  return duration;
}

// helper shared by every spin/draw flow — keeps the scroll position from
// jumping around while a spin animates (see notes further down).
function makeScrollPin() {
  const stageEl = document.querySelector(".stage");
  const lockedScroll = stageEl ? stageEl.scrollTop : 0;
  return () => { if (stageEl) stageEl.scrollTop = lockedScroll; };
}

// ------------------------------------------------------------ spin flow --
async function doSpin() {
  if (spinning || !selectedNo) return;
  const p = participants.find(x => x.no === selectedNo);
  if (!p || remaining(p, currentWheel) <= 0) return;

  // Pin the page's scroll position for the whole spin — something (focus
  // shifting off a disabled button, list re-renders, etc.) was yanking
  // .stage's scroll to the bottom every time a spin landed, which looked
  // like the wheel "repositioning" itself, especially jarring in auto-play.
  const pinScroll = makeScrollPin();

  const pool = computePool(currentWheel);
  if (pool.length === 0) { showToast("No prizes left in this wheel — add more via Manage Prizes.", true); return; }
  drawWheel(pool, false);

  spinning = true;
  // Blur before disabling — disabling a focused button makes some browsers
  // auto-scroll the nearest scrollable ancestor to "keep it visible", which
  // was yanking the whole page down every single spin during auto-play.
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  el("btnSpin").disabled = true;
  el("btnAuto").disabled = true;
  el("resultBanner").textContent = "";
  el("resultBanner").classList.remove("pop");
  pinScroll();
  requestAnimationFrame(pinScroll);

  let resp;
  try {
    resp = await api("/api/spin", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ no: selectedNo, wheel: currentWheel }),
    });
  } catch (e) {
    showToast(e.message, true);
    spinning = false;
    updateSelectedPanel();
    return;
  }

  const idx = pool.findIndex(pr => pr.id === resp.result.prize_id);
  const fast = el("fastMode").checked;
  const duration = spinToIndex(idx < 0 ? 0 : idx, pool.length, fast);

  await new Promise(res => setTimeout(res, duration + 150));

  // apply server-confirmed state
  Object.assign(p, resp.participant);
  prizesCache[currentWheel] = resp.wheel_pool;
  results.push(resp.result);

  el("resultBanner").textContent = `🎉 ${p.name} won: ${resp.result.prize_name}!`;
  el("resultBanner").classList.add("pop");
  fireConfetti();

  renderParticipantList();
  renderLog();
  updateSelectedPanel();
  // Don't reset rotation here — the wheel is already sitting at the correct
  // landed angle. Snapping it back to 0 caused a visible tilt-then-correct
  // jump every time a spin finished. Only redraw wedge art if the prize pool
  // shape actually changed (e.g. a prize sold out); keep the current angle.
  if (computePool(currentWheel).length !== lastPool.length) {
    drawWheel(computePool(currentWheel), false);
  }
  pinScroll();
  requestAnimationFrame(pinScroll);

  spinning = false;
  updateSelectedPanel();
  pinScroll();
  requestAnimationFrame(pinScroll);
}

async function doAutoPlay() {
  if (autoPlaying) { autoPlaying = false; return; } // second click = stop
  if (!selectedNo) return;
  autoPlaying = true;
  el("btnAuto").textContent = "Stop Auto-Play";

  while (autoPlaying) {
    const p = participants.find(x => x.no === selectedNo);
    if (!p || remaining(p, currentWheel) <= 0) break;
    if (computePool(currentWheel).length === 0) break;
    await doSpin();
    await new Promise(res => setTimeout(res, el("fastMode").checked ? 300 : 900));
  }
  autoPlaying = false;
  updateTabs();
  updateSelectedPanel();
}

async function doUndo() {
  if (!selectedNo || spinning) return;
  try {
    await api("/api/undo", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ no: selectedNo }),
    });
  } catch (e) { showToast(e.message, true); return; }
  await loadState();
}

// -------------------------------------------------------- special raffle --
async function doSpecialDraw() {
  if (spinning) return;
  if (specialData.awarded.length >= specialData.gifts.length) return;

  const pinScroll = makeScrollPin();
  const pool = specialPool.slice();
  if (pool.length === 0) { showToast("No one left in the special draw.", true); return; }
  drawWheel(pool, false);

  spinning = true;
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  el("btnSpin").disabled = true;
  el("btnAuto").disabled = true;
  el("resultBanner").textContent = "";
  el("resultBanner").classList.remove("pop");
  pinScroll();
  requestAnimationFrame(pinScroll);

  let resp;
  try {
    resp = await api("/api/special-draw", { method: "POST" });
  } catch (e) {
    showToast(e.message, true);
    spinning = false;
    updateSelectedPanel();
    return;
  }

  const idx = pool.findIndex(t => t.no === resp.result.no);
  const fast = el("fastMode").checked;
  const duration = spinToIndex(idx < 0 ? 0 : idx, pool.length, fast);
  await new Promise(res => setTimeout(res, duration + 150));

  specialData = resp.special;
  specialPool = resp.specialPool;

  el("resultBanner").textContent = `🎉 ${resp.result.name} wins: ${resp.result.gift}!`;
  el("resultBanner").classList.add("pop");
  fireConfetti();

  renderParticipantList();
  renderLog();
  updateSelectedPanel();
  drawWheel(computePool("special"), false); // redraw shrunk pool, keep current angle (no jump)
  pinScroll();
  requestAnimationFrame(pinScroll);

  spinning = false;
  updateSelectedPanel();
  pinScroll();
  requestAnimationFrame(pinScroll);
}

async function doSpecialAutoPlay() {
  if (autoPlaying) { autoPlaying = false; return; } // second click = stop
  autoPlaying = true;
  el("btnAuto").textContent = "Stop";

  while (autoPlaying) {
    if (specialData.awarded.length >= specialData.gifts.length) break;
    if (specialPool.length === 0) break;
    await doSpecialDraw();
    await new Promise(res => setTimeout(res, el("fastMode").checked ? 300 : 900));
  }
  autoPlaying = false;
  updateTabs();
  updateSelectedPanel();
}

async function doSpecialUndo() {
  if (spinning) return;
  try {
    const resp = await api("/api/special-undo", { method: "POST" });
    specialData = resp.special;
    specialPool = resp.specialPool;
  } catch (e) { showToast(e.message, true); return; }
  renderParticipantList();
  renderLog();
  updateSelectedPanel();
  drawWheel(computePool("special"), true);
}

// -------------------------------------------------- generic named raffles --
// Same mechanic as the special wheel, but for any raffle the server knows
// about (e.g. "graduation"), driven purely by its id via /api/raffle-*.
async function doRaffleDraw(wheelId) {
  if (spinning) return;
  const r = raffles[wheelId];
  if (!r || r.awarded.length >= r.gifts.length) return;

  const pinScroll = makeScrollPin();
  const pool = r.pool.slice();
  if (pool.length === 0) { showToast("No one left in this draw.", true); return; }
  drawWheel(pool, false);

  spinning = true;
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  el("btnSpin").disabled = true;
  el("btnAuto").disabled = true;
  el("resultBanner").textContent = "";
  el("resultBanner").classList.remove("pop");
  pinScroll();
  requestAnimationFrame(pinScroll);

  let resp;
  try {
    resp = await api("/api/raffle-draw", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ raffle: wheelId }),
    });
  } catch (e) {
    showToast(e.message, true);
    spinning = false;
    updateSelectedPanel();
    return;
  }

  const idx = pool.findIndex(t => t.name === resp.result.name);
  const fast = el("fastMode").checked;
  const duration = spinToIndex(idx < 0 ? 0 : idx, pool.length, fast);
  await new Promise(res => setTimeout(res, duration + 150));

  raffles[wheelId] = { ...resp.raffle, pool: resp.pool };

  el("resultBanner").textContent = `🎉 ${resp.result.name} wins: ${resp.result.gift}!`;
  el("resultBanner").classList.add("pop");
  fireConfetti();

  renderLog();
  updateSelectedPanel();
  drawWheel(computePool(wheelId), false); // redraw shrunk pool, keep current angle (no jump)
  pinScroll();
  requestAnimationFrame(pinScroll);

  spinning = false;
  updateSelectedPanel();
  pinScroll();
  requestAnimationFrame(pinScroll);
}

async function doRaffleAutoPlay(wheelId) {
  if (autoPlaying) { autoPlaying = false; return; } // second click = stop
  autoPlaying = true;
  el("btnAuto").textContent = "Stop";

  while (autoPlaying) {
    const r = raffles[wheelId];
    if (!r || r.awarded.length >= r.gifts.length) break;
    if (r.pool.length === 0) break;
    await doRaffleDraw(wheelId);
    await new Promise(res => setTimeout(res, el("fastMode").checked ? 300 : 900));
  }
  autoPlaying = false;
  updateTabs();
  updateSelectedPanel();
}

async function doRaffleUndo(wheelId) {
  if (spinning) return;
  try {
    const resp = await api("/api/raffle-undo", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ raffle: wheelId }),
    });
    raffles[wheelId] = { ...resp.raffle, pool: resp.pool };
  } catch (e) { showToast(e.message, true); return; }
  renderLog();
  updateSelectedPanel();
  drawWheel(computePool(wheelId), true);
}

// Each wheel keeps its own separate log — switches automatically with the
// tab. Entries themselves are never deleted by switching tabs or reloading;
// only Reset clears them (see confirmReset).
function wheelLogTitle(wheel) {
  if (wheel === "normal") return "Normal Wheel Log";
  if (wheel === "special") return "Special Wheel Log";
  const r = raffles[wheel];
  return `${r ? r.label : wheel} Log`;
}

function renderLog() {
  const box = el("logList");
  box.innerHTML = "";
  el("logTitle").textContent = wheelLogTitle(currentWheel);

  let entries;
  if (currentWheel === "normal") {
    entries = results.map(r => ({ ts: r.ts, no: r.no, name: r.name, label: r.prize_name }));
  } else if (currentWheel === "special") {
    entries = specialData.awarded.map(a => ({ ts: a.ts, no: a.no, name: a.name, label: a.gift }));
  } else {
    const r = raffles[currentWheel];
    entries = r ? r.awarded.map(a => ({ ts: a.ts, no: null, name: a.name, label: a.gift })) : [];
  }
  entries = [...entries].sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0));

  if (entries.length === 0) {
    box.innerHTML = `<div class="log-empty">No draws yet on this wheel.</div>`;
    return;
  }
  entries.slice(0, 100).forEach(r => {
    const row = document.createElement("div");
    row.className = "log-row";
    const namePart = r.no != null ? `#${r.no} ${r.name}` : r.name;
    row.innerHTML = `<span>${namePart} — ${r.label}</span>`;
    box.appendChild(row);
  });
}

function fireConfetti() {
  if (typeof confetti !== "function") return; // canvas-confetti not loaded (offline)
  const palette = ["#ef233c","#8b5cf6","#f472b6","#fb923c","#fbbf24","#38bdf8"];
  confetti({ particleCount: 130, spread: 100, startVelocity: 50, gravity: 0.85, ticks: 220,
    origin: { x: 0.5, y: 0.42 }, colors: palette, scalar: 1.15, zIndex: 200 });
  setTimeout(() => confetti({ particleCount: 70, angle: 60, spread: 65, startVelocity: 55,
    origin: { x: 0.05, y: 0.65 }, colors: palette, zIndex: 200 }), 120);
  setTimeout(() => confetti({ particleCount: 70, angle: 120, spread: 65, startVelocity: 55,
    origin: { x: 0.95, y: 0.65 }, colors: palette, zIndex: 200 }), 120);
  setTimeout(() => confetti({ particleCount: 40, spread: 360, startVelocity: 30, gravity: 0.6,
    origin: { x: 0.5, y: 0.4 }, colors: ["#ffffff", "#fbbf24"], shapes: ["circle"], scalar: 0.8, zIndex: 200 }), 250);
}

// --------------------------------------------------------- prize manager --
let specialGiftsDraft = [];

function openPrizeModal() {
  renderPrizeEditor("normal");
  specialGiftsDraft = [...specialData.gifts];
  renderSpecialGiftsEditor();
  el("prizeModal").classList.remove("hidden");
}
function closePrizeModal() { el("prizeModal").classList.add("hidden"); }

function renderPrizeEditor(wheel) {
  const box = el("prizeListNormal");
  box.innerHTML = "";
  (prizesCache[wheel] || []).forEach((p, i) => {
    const row = document.createElement("div");
    row.className = "prize-edit-row";
    row.innerHTML = `
      <input type="text" value="${p.name}" data-wheel="${wheel}" data-idx="${i}" data-field="name">
      <input type="number" min="0" value="${p.qty}" data-wheel="${wheel}" data-idx="${i}" data-field="qty">
      <button data-remove="${wheel}:${i}">✕</button>`;
    box.appendChild(row);
  });
}

function addPrizeRow(wheel) {
  const id = wheel[0] + Date.now();
  prizesCache[wheel].push({ id, name: "New Prize", qty: 1 });
  renderPrizeEditor(wheel);
}

function collectPrizeEditor() {
  document.querySelectorAll("#prizeModal input[data-wheel]").forEach(inp => {
    const wheel = inp.dataset.wheel, idx = +inp.dataset.idx, field = inp.dataset.field;
    if (!wheel) return;
    if (field === "qty") prizesCache[wheel][idx].qty = Math.max(0, parseInt(inp.value) || 0);
    if (field === "name") prizesCache[wheel][idx].name = inp.value;
  });
}

function renderSpecialGiftsEditor() {
  const box = el("specialGiftsList");
  box.innerHTML = "";
  specialGiftsDraft.forEach((g, i) => {
    const row = document.createElement("div");
    row.className = "prize-edit-row";
    row.innerHTML = `
      <input type="text" value="${g}" data-gift-idx="${i}">
      <button data-remove-gift="${i}">✕</button>`;
    box.appendChild(row);
  });
}

function collectSpecialGiftsDraft() {
  document.querySelectorAll("#specialGiftsList input[data-gift-idx]").forEach(inp => {
    specialGiftsDraft[+inp.dataset.giftIdx] = inp.value;
  });
}

function addGiftRow() {
  collectSpecialGiftsDraft();
  specialGiftsDraft.push("New Gift");
  renderSpecialGiftsEditor();
}

async function savePrizes() {
  collectPrizeEditor();
  collectSpecialGiftsDraft();
  try {
    await api("/api/prizes", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(prizesCache),
    });
    const giftsResp = await api("/api/special-gifts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gifts: specialGiftsDraft }),
    });
    specialData.gifts = giftsResp.gifts;
  } catch (e) { showToast(e.message, true); return; }
  closePrizeModal();
  updateTabs();
  updateSelectedPanel();
  drawWheel(computePool(currentWheel), true);
  showToast("Prizes & special gifts updated.");
}

// ---------------------------------------------------------- reset event --
function openResetModal() {
  el("resetError").classList.add("hidden");
  el("resetModal").classList.remove("hidden");
}
function closeResetModal() { el("resetModal").classList.add("hidden"); }

async function confirmReset() {
  const errBox = el("resetError");
  errBox.classList.add("hidden");
  try {
    await api("/api/reset", { method: "POST" });
  } catch (e) {
    errBox.textContent = e.message;
    errBox.classList.remove("hidden");
    return;
  }
  closeResetModal();
  selectedNo = null;
  await loadState();
  showToast("Event reset — everyone has their spins back, prizes are fully stocked, and every raffle starts fresh.");
}

// ------------------------------------------------------------ edit spins --
let spinsDraft = [];

function openSpinsModal() {
  spinsDraft = participants.map(p => ({ ...p }));
  renderSpinsTable();
  el("spinsModal").classList.remove("hidden");
}
function closeSpinsModal() { el("spinsModal").classList.add("hidden"); }

function renderSpinsTable() {
  const body = el("spinsTableBody");
  body.innerHTML = "";
  spinsDraft
    .sort((a, b) => (+a.no || 0) - (+b.no || 0))
    .forEach((p, i) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="no-cell">${p.no}</td>
        <td><input type="text" value="${p.name}" data-field="name" data-idx="${i}"></td>
        <td><input type="number" min="0" value="${p.normal_total}" data-field="normal_total" data-idx="${i}"></td>
        <td><input type="number" min="0" value="${p.special_total}" data-field="special_total" data-idx="${i}"></td>
        <td><button class="row-remove" data-remove-idx="${i}">✕</button></td>`;
      body.appendChild(tr);
    });
}

function collectSpinsDraft() {
  document.querySelectorAll("#spinsTableBody input").forEach(inp => {
    const idx = +inp.dataset.idx, field = inp.dataset.field;
    if (field === "name") spinsDraft[idx].name = inp.value;
    else spinsDraft[idx][field] = Math.max(0, parseInt(inp.value) || 0);
  });
}

function addParticipantRow() {
  collectSpinsDraft();
  const nextNo = spinsDraft.length ? Math.max(...spinsDraft.map(p => +p.no || 0)) + 1 : 1;
  spinsDraft.push({
    no: nextNo, name: "New Participant",
    normal_total: 0, normal_used: 0, special_total: 0, special_used: 0,
  });
  renderSpinsTable();
}

async function saveSpins() {
  collectSpinsDraft();
  try {
    await api("/api/participants", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ participants: spinsDraft }),
    });
  } catch (e) { showToast(e.message, true); return; }
  closeSpinsModal();
  await loadState();
  showToast("Roster updated.");
}

// ------------------------------------------------------- graduation reward --
let graduationRosterDraft = [];
let graduationGiftsDraft = [];

function openGraduationModal() {
  const r = raffles.graduation || { label: "Graduation Reward", gifts: [], roster: [] };
  graduationRosterDraft = r.roster.map(x => ({ ...x }));
  graduationGiftsDraft = [...r.gifts];
  renderGraduationRoster();
  renderGraduationGifts();
  el("graduationModal").classList.remove("hidden");
}
function closeGraduationModal() { el("graduationModal").classList.add("hidden"); }

function renderGraduationRoster() {
  const body = el("graduationRosterBody");
  body.innerHTML = "";
  graduationRosterDraft.forEach((p, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="text" value="${p.name}" data-field="name" data-idx="${i}"></td>
      <td><input type="number" min="1" value="${p.tickets}" data-field="tickets" data-idx="${i}"></td>
      <td><button class="row-remove" data-remove-idx="${i}">✕</button></td>`;
    body.appendChild(tr);
  });
}

function collectGraduationRoster() {
  document.querySelectorAll("#graduationRosterBody input").forEach(inp => {
    const idx = +inp.dataset.idx, field = inp.dataset.field;
    if (field === "name") graduationRosterDraft[idx].name = inp.value;
    else graduationRosterDraft[idx][field] = Math.max(1, parseInt(inp.value) || 1);
  });
}

function addGraduateRow() {
  collectGraduationRoster();
  graduationRosterDraft.push({ name: "New Person", tickets: 1 });
  renderGraduationRoster();
}

function renderGraduationGifts() {
  const box = el("graduationGiftsList");
  box.innerHTML = "";
  graduationGiftsDraft.forEach((g, i) => {
    const row = document.createElement("div");
    row.className = "prize-edit-row";
    row.innerHTML = `
      <input type="text" value="${g}" data-grad-gift-idx="${i}">
      <button data-remove-grad-gift="${i}">✕</button>`;
    box.appendChild(row);
  });
}

function collectGraduationGifts() {
  document.querySelectorAll("#graduationGiftsList input[data-grad-gift-idx]").forEach(inp => {
    graduationGiftsDraft[+inp.dataset.gradGiftIdx] = inp.value;
  });
}

function addGraduationGiftRow() {
  collectGraduationGifts();
  graduationGiftsDraft.push("New Gift");
  renderGraduationGifts();
}

async function saveGraduation() {
  collectGraduationRoster();
  collectGraduationGifts();
  try {
    const resp = await api("/api/raffle-roster", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ raffle: "graduation", label: "Graduation Reward", gifts: graduationGiftsDraft, roster: graduationRosterDraft }),
    });
    raffles.graduation = resp;
  } catch (e) { showToast(e.message, true); return; }
  closeGraduationModal();
  updateTabs();
  updateSelectedPanel();
  if (currentWheel === "graduation") drawWheel(computePool("graduation"), true);
  showToast("Graduation Reward updated.");
}

// ------------------------------------------------------------------ init --
function buildLightRing() {
  const ring = el("lightRing");
  if (!ring || ring.childElementCount) return;
  const n = 28, radius = 235;
  for (let i = 0; i < n; i++) {
    const angle = (i / n) * Math.PI * 2;
    const bulb = document.createElement("div");
    bulb.className = "light-bulb";
    bulb.style.left = `calc(50% + ${Math.cos(angle) * radius}px - 4px)`;
    bulb.style.top = `calc(50% + ${Math.sin(angle) * radius}px - 4px)`;
    bulb.style.animationDelay = (i % 2 === 0 ? "0s" : "0.5s");
    ring.appendChild(bulb);
  }
}

window.addEventListener("DOMContentLoaded", () => {
  loadState();
  buildLightRing();
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => drawWheel(computePool(currentWheel), false));
  }

  el("search").addEventListener("input", renderParticipantList);

  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      currentWheel = btn.dataset.wheel;
      updateTabs();
      updateSelectedPanel();
      renderLog();
      drawWheel(computePool(currentWheel), true);
    });
  });

  el("btnSpin").addEventListener("click", () => {
    if (currentWheel === "normal") doSpin();
    else if (currentWheel === "special") doSpecialDraw();
    else doRaffleDraw(currentWheel);
  });
  el("btnAuto").addEventListener("click", () => {
    if (currentWheel === "normal") doAutoPlay();
    else if (currentWheel === "special") doSpecialAutoPlay();
    else doRaffleAutoPlay(currentWheel);
  });
  el("btnUndo").addEventListener("click", () => {
    if (currentWheel === "normal") doUndo();
    else if (currentWheel === "special") doSpecialUndo();
    else doRaffleUndo(currentWheel);
  });

  el("btnSync").addEventListener("click", async () => {
    el("btnSync").disabled = true;
    el("btnSync").textContent = "Syncing...";
    try { await api("/api/sync", { method: "POST" }); await loadState(); showToast("Synced from the Master sheet."); }
    catch (e) { showToast(e.message, true); }
    el("btnSync").disabled = false;
    el("btnSync").textContent = "↻ Sync from Sheet";
  });

  el("btnExport").addEventListener("click", async () => {
    try {
      const r = await api("/api/export", { method: "POST" });
      showToast(`Exported ${r.exported} results to "SPIN RESULTS" tab in the Master sheet.`);
    } catch (e) { showToast(e.message, true); }
  });

  el("btnPrizes").addEventListener("click", openPrizeModal);
  el("btnClosePrizes").addEventListener("click", closePrizeModal);
  el("btnSavePrizes").addEventListener("click", savePrizes);
  document.querySelectorAll("[data-add]").forEach(btn => {
    btn.addEventListener("click", () => addPrizeRow(btn.dataset.add));
  });
  el("btnAddGift").addEventListener("click", addGiftRow);
  el("prizeModal").addEventListener("click", (e) => {
    if (e.target.dataset.remove) {
      const [wheel, idx] = e.target.dataset.remove.split(":");
      prizesCache[wheel].splice(+idx, 1);
      renderPrizeEditor(wheel);
    }
    if (e.target.dataset.removeGift !== undefined) {
      collectSpecialGiftsDraft();
      specialGiftsDraft.splice(+e.target.dataset.removeGift, 1);
      renderSpecialGiftsEditor();
    }
  });

  el("btnEditSpins").addEventListener("click", openSpinsModal);
  el("btnCloseSpins").addEventListener("click", closeSpinsModal);
  el("btnSaveSpins").addEventListener("click", saveSpins);
  el("btnAddParticipant").addEventListener("click", addParticipantRow);
  el("spinsTableBody").addEventListener("click", (e) => {
    if (e.target.dataset.removeIdx !== undefined) {
      collectSpinsDraft();
      spinsDraft.splice(+e.target.dataset.removeIdx, 1);
      renderSpinsTable();
    }
  });

  el("btnGraduation").addEventListener("click", openGraduationModal);
  el("btnCloseGraduation").addEventListener("click", closeGraduationModal);
  el("btnSaveGraduation").addEventListener("click", saveGraduation);
  el("btnAddGraduate").addEventListener("click", addGraduateRow);
  el("btnAddGraduationGift").addEventListener("click", addGraduationGiftRow);
  el("graduationRosterBody").addEventListener("click", (e) => {
    if (e.target.dataset.removeIdx !== undefined) {
      collectGraduationRoster();
      graduationRosterDraft.splice(+e.target.dataset.removeIdx, 1);
      renderGraduationRoster();
    }
  });
  el("graduationGiftsList").addEventListener("click", (e) => {
    if (e.target.dataset.removeGradGift !== undefined) {
      collectGraduationGifts();
      graduationGiftsDraft.splice(+e.target.dataset.removeGradGift, 1);
      renderGraduationGifts();
    }
  });

  el("btnReset").addEventListener("click", openResetModal);
  el("btnCancelReset").addEventListener("click", closeResetModal);
  el("btnConfirmReset").addEventListener("click", confirmReset);
});
