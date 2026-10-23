const CHANNEL = {
  kakao: { label: "카카오톡", short: "K" },
  naver: { label: "네이버 톡톡", short: "N" },
  instagram: { label: "인스타그램 DM", short: "I" },
  web: { label: "홈페이지 상담", short: "W" },
};
const STATUS = { new: "새 문의", analyzing: "초안 생성 중", drafted: "초안 준비", sent: "발송 완료", escalated: "담당자 확인", error: "오류" };
const URGENCY = { high: "긴급", normal: "보통", low: "낮음" };
const LANG = { ko: "한국어", en: "영어", ja: "일본어", zh: "중국어", other: "기타 언어" };
const OPEN = ["new", "analyzing", "drafted", "error"];
const EMPTY_DETAIL = $("#detail")?.innerHTML;

const state = { threads: [], filter: "open", selected: null, view: null, flags: [], crmName: "", pollTimer: null, checkTimer: null };

function $(sel) { return document.querySelector(sel); }
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (!res.ok) {
    let message = res.statusText;
    try { message = (await res.json()).detail || message; } catch { /* 본문 없는 오류 */ }
    const error = new Error(message);
    error.status = res.status;  // 409: 화면에서 본 뒤로 문의가 바뀌었다
    throw error;
  }
  return res.json();
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body ?? {}) });
const threadPath = (suffix = "") => `/api/threads/${encodeURIComponent(state.selected)}${suffix}`;

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(el.hideTimer);
  el.hideTimer = setTimeout(() => el.classList.remove("show"), 2800);
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const hm = d.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false });
  return d.toDateString() === new Date().toDateString() ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}
function fmtDate(iso) {
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${y}.${m}.${d}`;
}
function maskPhone(phone) {
  const digits = (phone || "").replace(/\D/g, "");
  return digits.length >= 10 ? `${digits.slice(0, 3)}-****-${digits.slice(-4)}` : "-";
}
function maskEmail(email) {
  if (!email) return "-";
  const [user, domain] = email.split("@");
  return `${user.slice(0, 2)}***@${domain}`;
}
const chBadge = (ch) => `<span class="ch ${ch}" title="${CHANNEL[ch].label}">${CHANNEL[ch].short}</span>`;
const isOpen = (status) => OPEN.includes(status);

// ---- 상단 바 --------------------------------------------------------------

async function loadStatus() {
  const s = await api("/api/status");
  state.crmName = s.crm;
  $("#clinic-line").textContent = `${s.clinic} · AI 상담 보조`;
  const mode = $("#mode-badge");
  mode.textContent = s.mode_label;
  mode.classList.toggle("offline", s.mode !== "claude");
  const crm = $("#crm-badge");
  crm.textContent = s.crm;
  crm.title = s.crm_description;

  const m = s.metrics;
  const kept = m.avg_similarity == null ? "-" : `${Math.round(m.avg_similarity * 100)}%`;
  $("#stats").innerHTML = [
    ["처리 필요", m.open, ""],
    ["긴급", m.urgent, "urgent"],
    ["발송 완료", m.sent, ""],
    ["초안 유지율", kept, ""],
  ].map(([label, value, cls]) => `<div class="${cls}"><dt>${label}</dt><dd>${value}</dd></div>`).join("");

  $("#sync-btn").innerHTML = `새 문의 가져오기${s.remaining ? `<span class="count">${s.remaining}</span>` : ""}`;
}

// ---- 문의 목록 ------------------------------------------------------------

function visibleThreads() {
  return state.threads.filter((t) => {
    if (state.filter === "open") return isOpen(t.status);
    if (state.filter === "urgent") return isOpen(t.status) && t.urgency === "high";
    if (state.filter === "done") return !isOpen(t.status);
    return true;
  });
}

function renderList() {
  const items = visibleThreads();
  const list = $("#thread-list");
  if (!items.length) {
    list.innerHTML = `<li class="muted" style="cursor:default">해당하는 문의가 없어요.</li>`;
    return;
  }
  list.innerHTML = items.map((t) => {
    const tags = [];
    if (isOpen(t.status) && t.urgency === "high") tags.push(`<span class="tag urgent">긴급</span>`);
    if (t.status === "analyzing") tags.push(`<span class="tag new">초안 생성 중…</span>`);
    if (!isOpen(t.status) || t.status === "error") tags.push(`<span class="tag status-${t.status}">${STATUS[t.status]}</span>`);
    if (isOpen(t.status) && t.needs_human) tags.push(`<span class="tag human">담당자 확인</span>`);
    if (t.category) tags.push(`<span class="tag">${esc(t.category)}</span>`);
    if (t.intent) tags.push(`<span class="tag">${esc(t.intent)}</span>`);
    if (!t.registered) tags.push(`<span class="tag">CRM 미등록</span>`);
    const classes = [
      t.key === state.selected ? "selected" : "",
      isOpen(t.status) && t.urgency === "high" ? "is-urgent" : "",
      isOpen(t.status) ? "" : "done",
    ].join(" ");
    return `<li data-key="${esc(t.key)}" class="${classes}">
      <div class="row">
        ${t.channels.map(chBadge).join("")}
        <span class="name">${esc(t.name)}</span>
        ${t.message_count > 1 ? `<span class="muted">· ${t.message_count}건</span>` : ""}
        <span class="time">${fmtTime(t.last_at)}</span>
      </div>
      <p class="summary">${esc(t.summary || t.last_text)}</p>
      <div class="tags">${tags.join("")}</div>
    </li>`;
  }).join("");
}

async function loadThreads() {
  state.threads = await api("/api/threads");
  renderList();
  clearTimeout(state.pollTimer);
  if (state.threads.some((t) => t.status === "analyzing")) state.pollTimer = setTimeout(refresh, 1500);
}

async function refresh() {
  await Promise.all([loadThreads(), loadStatus()]);
  if (!state.selected) {
    const first = visibleThreads()[0];
    if (first) await openThread(first.key);
    return;
  }
  const current = state.threads.find((t) => t.key === state.selected);
  // 상태가 바뀐 경우에만 다시 그린다. 그래야 고치던 초안이 날아가지 않는다.
  if (current && state.view && current.status !== state.view.status) await openThread(state.selected);
}

// ---- 상세 ---------------------------------------------------------------

async function openThread(key) {
  state.selected = key;
  renderList();
  const view = await api(`/api/threads/${encodeURIComponent(key)}`);
  if (state.selected !== key) return;
  showView(view);
}

function showView(view) {
  state.view = view;
  renderDetail(view);
  renderCRM(view);
}

function renderFlags(flags) {
  if (!flags.length) return `<span class="ok">✓ 의료광고·환자 유인 관련 위험 표현이 없어요 (참고용 점검)</span>`;
  return flags.map((f) => f.kind === "review"
    ? `<div class="flag review"><strong>AI 초안 검토</strong> <code>${esc(f.phrase)}</code> — ${esc(f.reason)}</div>`
    : `<div class="flag"><code>${esc(f.phrase)}</code> — ${esc(f.reason)}</div>`).join("");
}

// 자동 검토 경고는 AI 초안을 두고 한 말이라, 고쳐 쓰는 동안 실시간 점검 결과와 함께 계속 보여준다.
const withReview = (liveFlags, originalFlags) => [...liveFlags, ...(originalFlags || []).filter((f) => f.kind === "review")];

function renderDetail(v) {
  const inbound = v.messages.filter((m) => m.direction === "in");
  const channels = [...new Set(inbound.map((m) => m.channel))];
  const head = `<div class="detail-head">
      <div>
        <h2>${esc(v.display_name)}</h2>
        <div class="sub">${channels.map((c) => CHANNEL[c].label).join(" · ")} · ${v.customer_id ? `CRM ${esc(v.customer_id)}` : "CRM 미등록"}</div>
      </div>
      <span class="tag status-${v.status}" style="margin-left:auto">${STATUS[v.status]}</span>
    </div>`;
  const conversation = `<div class="conversation">${v.messages.map((m) => `
      <div class="bubble ${m.direction}">
        <div class="meta">${m.direction === "in" ? `${chBadge(m.channel)} ${esc(m.sender_name)}` : `상담팀 → ${CHANNEL[m.channel].label}`} · ${fmtTime(m.at)}</div>
        <div class="body">${esc(m.text)}</div>
      </div>`).join("")}</div>`;

  let card;
  if (v.status === "analyzing" || (v.status === "new" && !v.analysis)) {
    card = `<div class="card"><div class="loading"><div class="spinner"></div>CRM 이력과 안내 자료를 확인해서 초안을 만드는 중이에요…</div></div>`;
  } else if (!v.analysis) {
    card = `<div class="card"><div class="alert urgent"><strong>초안을 만들지 못했어요</strong>${esc(v.note)}</div>
      <div class="buttons"><button id="regen-btn">다시 생성</button></div></div>`;
  } else if (v.status === "sent") {
    card = sentCard(v);
  } else {
    card = draftCard(v);
  }
  $("#detail").innerHTML = head + conversation + card;
  bindDetail(v);
}

// 인스타처럼 답장 기한이 있는 채널: 남은 시간, 24시간 뒤 사람 상담원 답장 구간, 기한 지남을 보여준다.
function windowNote(w) {
  if (!w || w.state === "none") return "";
  if (w.state === "open") return `<div class="alert note">${esc(w.message)}</div>`;
  if (w.state === "human_agent") return `<div class="alert human"><strong>인스타 24시간 지남</strong>${esc(w.message)}</div>`;
  return `<div class="alert urgent"><strong>인스타로는 보낼 수 없어요</strong>${esc(w.message)}</div>`;
}

function draftCard(v) {
  const a = v.analysis;
  const foreign = a.language !== "ko";
  const chips = [a.category, a.intent, `긴급도 ${URGENCY[a.urgency]}`, LANG[a.language]]
    .map((x, i) => `<span class="tag ${i === 2 && a.urgency === "high" ? "urgent" : ""}">${esc(x)}</span>`).join("");
  const note = v.status === "escalated"
    ? `<div class="alert note"><strong>담당자에게 넘김</strong>${esc(v.note)}</div>`
    : v.note ? `<div class="alert note">${esc(v.note)}</div>` : "";
  return `<div class="card">
    <div class="card-head"><h3>AI 분석</h3><span class="mode">${esc(v.mode_label)}</span></div>
    <p class="ai-summary">${esc(a.summary)}</p>
    <div class="tags">${chips}</div>
    ${a.urgency === "high"
      ? `<div class="alert urgent"><strong>응급 가능성 · 메신저 답장과 별개로 바로 전화 확인</strong>${esc(a.needs_human_reason)}</div>`
      : a.needs_human ? `<div class="alert human"><strong>담당자 확인 필요</strong>${esc(a.needs_human_reason)}</div>` : ""}
    ${note}
    ${windowNote(v.reply_window)}
    ${v.live ? `<div class="alert note"><strong>네이버 톡톡 실제 연동</strong>승인하면 보내기 API로 고객에게 바로 나가요. 네이버에서는 보낸 메시지를 회수할 수 없어요.</div>` : ""}
    <label class="field" for="draft">답변 초안${foreign ? ` · ${LANG[a.language]}로 발송` : ""} — 확인하고 고쳐서 보내세요</label>
    <textarea id="draft">${esc(a.draft_reply)}</textarea>
    ${foreign ? `<details open><summary>한국어로 보기</summary><div class="ko-text">${esc(a.draft_reply_ko)}</div></details>` : ""}
    <div class="flags" id="flags">${renderFlags(v.flags)}</div>
    ${v.sources.length ? `<div class="sources">참고한 안내 자료 ${v.sources.map((s) => `<button data-source="${esc(s.id)}">${esc(s.title)}</button>`).join("")}</div>` : ""}
    ${v.mode === "offline" ? "" : `<details><summary>AI에 보낸 내용 보기 · 개인정보 ${v.masked || 0}건 가림</summary><pre class="prompt">${esc(v.prompt)}</pre></details>`}
    <div class="buttons">
      <button class="primary" id="approve-btn" ${v.reply_window?.state === "closed" ? "disabled" : ""}>승인하고 발송</button>
      <button id="regen-btn">다시 생성</button>
      ${v.status !== "escalated" ? `<button id="escalate-btn">담당자에게 넘기기</button>` : ""}
      ${a.intent === "예약·일정" ? `<button id="binjari-btn">빈자리에서 빈 시간 보기</button>` : ""}
    </div>
    <div id="escalate-slot"></div>
  </div>`;
}

function sentCard(v) {
  const similarity = v.edit_ratio == null ? "" : `AI 초안과 ${Math.round(v.edit_ratio * 100)}% 같음`;
  const lastOut = [...v.messages].reverse().find((m) => m.direction === "out");
  return `<div class="card">
    <div class="card-head"><h3>발송 완료</h3><span class="mode">${similarity}</span></div>
    <p class="muted" style="margin:0">${lastOut ? CHANNEL[lastOut.channel].label : "채널"}로 답장을 보냈고, CRM 응대 기록에 남겼어요.</p>
    ${v.analysis ? `<details><summary>AI 초안 원문 보기</summary><div class="ko-text">${esc(v.analysis.draft_reply)}</div></details>` : ""}
  </div>`;
}

function bindDetail(v) {
  state.flags = v.flags || [];
  const draft = $("#draft");
  draft?.addEventListener("input", () => {
    clearTimeout(state.checkTimer);
    state.checkTimer = setTimeout(async () => {
      const { flags } = await post("/api/check", { text: draft.value });
      state.flags = withReview(flags, v.flags);
      $("#flags").innerHTML = renderFlags(state.flags);
    }, 350);
  });
  $("#approve-btn")?.addEventListener("click", approve);
  $("#regen-btn")?.addEventListener("click", regenerate);
  $("#escalate-btn")?.addEventListener("click", showEscalateForm);
  $("#binjari-btn")?.addEventListener("click", () => switchTab("binjari"));
  document.querySelectorAll("[data-source]").forEach((btn) =>
    btn.addEventListener("click", () => showSource(v.sources.find((s) => s.id === btn.dataset.source))));
}

async function approve() {
  const text = $("#draft").value.trim();
  if (!text) return toast("보낼 내용이 비어 있어요.");
  if (state.flags.length && !confirm(`위험 표현이 ${state.flags.length}개 있어요. 그대로 보낼까요?`)) return;
  const btn = $("#approve-btn");
  btn.disabled = true;
  btn.textContent = "보내는 중…";
  try {
    // 화면에서 본 마지막 고객 메시지. 그 뒤로 새 메시지가 왔거나 다른 곳에서 이미 답했으면 서버가 보내지 않는다(409).
    const lastSeen = state.view.messages.filter((m) => m.direction === "in").at(-1)?.id;
    const res = await post(threadPath("/approve"), { text, last_seen: lastSeen });
    showView(res.thread);
    toast(`${CHANNEL[res.sent.channel].label}로 보냈고 CRM(${res.customer_id})에 기록했어요.`);
    await Promise.all([loadThreads(), loadStatus()]);
  } catch (e) {
    toast(e.message);
    if (e.status === 409) {  // 새 메시지를 보여 주되, 고쳐 쓰던 답장은 남긴다
      await Promise.all([loadThreads(), loadStatus()]);
      await openThread(state.selected);
      if ($("#draft")) $("#draft").value = text;
      return;
    }
    btn.disabled = false;
    btn.textContent = "승인하고 발송";
  }
}

async function regenerate() {
  const btn = $("#regen-btn");
  btn.disabled = true;
  btn.textContent = "생성 중…";
  try {
    showView(await post(threadPath("/regenerate")));
    await loadThreads();
    toast("초안을 새로 만들었어요.");
  } catch (e) {
    toast(e.message);
    btn.disabled = false;
    btn.textContent = "다시 생성";
  }
}

function showEscalateForm() {
  $("#escalate-slot").innerHTML = `<div class="escalate-form">
      <input id="escalate-note" placeholder="담당자에게 남길 메모 (예: 간호팀 전화 확인 부탁드려요)">
      <button class="primary" id="escalate-send">넘기기</button>
    </div>`;
  const input = $("#escalate-note");
  input.value = state.view.analysis?.needs_human_reason || "";
  input.focus();
  $("#escalate-send").addEventListener("click", async () => {
    const view = await post(threadPath("/escalate"), { note: input.value });
    showView(view);
    toast(view.customer_id ? "담당자에게 넘기고 CRM에 기록했어요." : "담당자에게 넘겼어요.");
    await Promise.all([loadThreads(), loadStatus()]);
  });
}

function showSource(source) {
  if (!source) return;
  $("#source-title").textContent = source.title;
  $("#source-text").textContent = source.text;
  $("#source-dialog").showModal();
}

// ---- CRM 패널 -------------------------------------------------------------

const CONSENT_LABEL = {
  marketing_consent: ["광고 수신 철회", "광고 수신 동의"],
  kakao_friend: ["카톡 채널 차단", "카톡 채널 추가"],
};

function consentTimeline(events) {
  return [...events].reverse().map((e) => `<li class="${e.value ? "" : "revoked"}">
      <div class="date">${fmtDate(e.at)} ${esc(e.at.slice(11, 16))} · ${esc(e.source)}</div>
      <div>${CONSENT_LABEL[e.field]?.[e.value ? 1 : 0] || esc(e.field)}</div></li>`).join("")
    || `<li class="muted">기록이 없어요.</li>`;
}

// 실제로는 문자 대행사(NHN Cloud)의 080 수신거부 웹훅, 카카오톡 채널 웹훅이 들어온다. 시연 버튼은 같은 모양으로 보낸다.
function optOutButtons(c) {
  const buttons = [];
  if (c.marketing_consent && c.phone) buttons.push(`<button data-optout="080">080 수신거부 들어옴</button>`);
  if (c.channel_ids.kakao) {
    buttons.push(c.kakao_friend
      ? `<button data-optout="kakao_block">카톡 채널 차단함</button>`
      : `<button data-optout="kakao_unblock">카톡 채널 다시 추가함</button>`);
  }
  return buttons.length ? `<div class="demo-actions"><span>시연 · 외부 알림이 온 것처럼</span>${buttons.join("")}</div>` : "";
}

// 전화·대면으로 들은 거부, 자동 반영하지 않은 애매한 답장을 확인한 뒤 상담실장이 직접 반영한다.
function staffOptOut(c) {
  if (!c.marketing_consent) return "";
  return `<div class="staff-actions"><button class="danger" data-staff-optout>수신거부 반영</button>
    <span class="muted">고객이 전화·대면으로 거부했거나, 애매한 답장의 뜻을 확인했을 때</span></div>`;
}

// 문의함 쪽 CRM 패널만 다시 그린다. 초안 영역은 건드리지 않아 고쳐 쓰던 답장이 날아가지 않는다.
async function reloadInboxCRM() {
  if (!state.selected || !state.view) return;
  const key = state.selected;
  const view = await api(`/api/threads/${encodeURIComponent(key)}`);
  if (state.selected !== key) return;
  Object.assign(state.view, { customer: view.customer, consent_history: view.consent_history, contact_logs: view.contact_logs });
  renderCRM(view);
}

function demoWebhook(source, c) {
  const now = new Date().toISOString();
  if (source === "080") {
    return ["/api/demo/webhooks/nhn-sms", {
      event: "UNSUBSCRIBE", productName: "SMS",
      hooks: [{ hookId: `demo-${Date.now()}`, recipientNo: c.phone.replace(/\D/g, ""), unsubscribeNo: "0800000000", createdDateTime: now }],
    }];
  }
  return ["/api/demo/webhooks/kakao-channel", {
    event: source === "kakao_block" ? "blocked" : "added", id: c.channel_ids.kakao, id_type: "app_user_id",
    channel_public_id: "_demo", updated_at: now,
  }];
}

async function handleOptOut(e) {
  const btn = e.target.closest("button[data-optout], button[data-staff-optout]");
  const panel = e.currentTarget;  // await 뒤에는 e.currentTarget이 비므로 미리 잡아 둔다
  const c = panel.customer;
  if (!btn || !c) return;
  if (btn.dataset.staffOptout !== undefined && !confirm(`${c.name}님의 광고 수신 동의를 끌까요? 준비 중인 광고도 멈춰요.`)) return;
  btn.disabled = true;
  try {
    const res = btn.dataset.staffOptout !== undefined
      ? await post(`/api/customers/${encodeURIComponent(c.id)}/opt-out`, {})
      : (await post(...demoWebhook(btn.dataset.optout, c))).results[0];
    toast(res?.message || "반영할 내용이 없어요.");
  } catch (err) {
    toast(err.message);
  }
  if (panel.id === "recall-crm") {
    await loadRecall({ plan: true });  // 다시 계획해야 처리 결과 안내가 바로 큐에 들어온다 (멈춘 광고는 새 항목으로 바뀐다)
    await reselectRecall(c.id);
  } else await reloadInboxCRM();
}

function renderCRM(v, el = $("#crm")) {
  const foot = `<p class="crm-foot">연동 방식: ${esc(state.crmName)}</p>`;
  const c = v.customer;
  el.customer = c;
  if (!c) {
    el.innerHTML = `<h3>CRM 고객 정보</h3>
      <div class="lead-box"><strong>CRM 미등록 문의</strong>
        <p class="muted" style="margin:6px 0 0">답장을 보내면 신규 고객(리드)으로 등록하고 응대 기록을 남겨요.</p></div>${foot}`;
    return;
  }
  const history = [
    ...c.consultations.map((x) => ({ date: x.date, title: `${x.procedure} 상담`, sub: `${x.counselor} · ${x.status}`, note: x.note })),
    ...c.appointments.map((x) => ({ date: x.date, time: x.time, title: `${x.kind} · ${x.procedure}`, sub: x.status, upcoming: x.status === "예정" })),
  ].sort((a, b) => `${a.date} ${a.time || ""}`.localeCompare(`${b.date} ${b.time || ""}`));
  const channels = Object.entries(c.channel_ids).map(([ch, id]) => `${chBadge(ch)} ${esc(id)}`).join("<br>");
  el.innerHTML = `<h3>CRM 고객 정보</h3>
    <div class="who">${esc(c.name)}<small>${esc(c.id)}</small></div>
    <div class="tags" style="margin-top:6px">${c.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>
    <dl>
      <dt>연락처</dt><dd>${esc(maskPhone(c.phone))}</dd>
      <dt>이메일</dt><dd>${esc(maskEmail(c.email))}</dd>
      <dt>언어</dt><dd>${esc(LANG[c.language] || c.language)}</dd>
      <dt>연결 채널</dt><dd>${channels || "-"}</dd>
      <dt>광고 수신</dt><dd>${c.marketing_consent ? "동의" : "동의 안 함"}</dd>
      <dt>카톡 채널</dt><dd>${c.kakao_friend ? "친구 추가함" : "친구 아님"}</dd>
    </dl>
    <h3>수신 동의 이력</h3>
    <ul class="timeline consent">${consentTimeline(v.consent_history || [])}</ul>
    ${staffOptOut(c)}
    ${optOutButtons(c)}
    <h3>상담 · 예약 이력</h3>
    <ul class="timeline">${history.map((i) => `<li class="${i.upcoming ? "upcoming" : ""}">
        <div class="date">${fmtDate(i.date)}${i.time ? ` ${esc(i.time)}` : ""}</div><div>${esc(i.title)}</div>
        <div class="note">${esc(i.sub)}${i.note ? ` · ${esc(i.note)}` : ""}</div></li>`).join("") || `<li class="muted">이력이 없어요.</li>`}</ul>
    <h3>응대 기록</h3>
    <ul class="timeline">${v.contact_logs.map((l) => `<li>
        <div class="date">${fmtTime(l.at)} · ${esc(l.channel)} · ${esc(l.handled_by)}</div><div>${esc(l.summary)}</div></li>`).join("") || `<li class="muted">아직 없어요.</li>`}</ul>
    ${foot}`;
}

// ---- 이벤트 ---------------------------------------------------------------

$("#crm").addEventListener("click", handleOptOut);
$("#recall-crm").addEventListener("click", handleOptOut);

$("#thread-list").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-key]");
  if (li) openThread(li.dataset.key);
});

$("#filters").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-filter]");
  if (!btn) return;
  state.filter = btn.dataset.filter;
  document.querySelectorAll("#filters button").forEach((b) => b.classList.toggle("active", b === btn));
  renderList();
});

$("#sync-btn").addEventListener("click", async () => {
  const res = await post("/api/sync");
  toast(res.fetched ? `새 문의 ${res.fetched}건을 가져왔어요. 초안을 만드는 중이에요.` : "새 문의가 없어요.");
  await refresh();
});

$("#reset-btn").addEventListener("click", async () => {
  if (!confirm("데모 데이터를 처음 상태로 되돌릴까요? 보낸 답장과 CRM 기록이 지워져요.")) return;
  await post("/api/reset");
  state.selected = null;
  state.view = null;
  $("#detail").innerHTML = EMPTY_DETAIL;
  $("#crm").innerHTML = "";
  await refresh();
  window.onDemoReset?.();
  toast("처음 상태로 되돌렸어요.");
});

$("#views").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-view]");
  if (!btn) return;
  switchTab(btn.dataset.view);
});

function switchTab(view) {
  document.body.className = `view-${view}`;
  document.querySelectorAll("#views button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  if (view === "recall") window.openRecallView?.();
  else if (view === "binjari") {
    // 처음에만 불러온다. 그 뒤로는 새로 고치지 않고 '다시 읽어' 신호만 보낸다 — 빈자리가 feed.js(지금 시각·CRM 예약)를
    // 다시 읽어 값만 바꾸므로 통화 중 고른 안건·칸이 남는다. (빈자리는 열어 둔 동안에도 1분마다 스스로 다시 읽는다.)
    const frame = $("#binjari-frame");
    const w = frame.contentWindow;
    if (!frame.getAttribute("src") || !w || w.location.href === "about:blank") frame.setAttribute("src", "/binjari/");
    else w.dispatchEvent(new Event("binjari-refresh"));
  } else refresh().then(reloadInboxCRM);  // 리콜 탭에서 바뀐 동의가 문의함 CRM 패널에도 보이게
}

refresh();
