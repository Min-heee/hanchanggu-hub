// 리콜·팔로업 화면. app.js의 공용 함수($, esc, api, post, toast, renderFlags, renderCRM, fmtDate, fmtTime)를 쓴다.
const recall = { offset: 0, data: null, selected: null, selectedCustomer: null, edits: {}, pollTimer: null, checkTimers: {} };
const WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];
const EMPTY_RECALL_CRM = `<h3>CRM 고객 정보</h3><p class="muted">카드를 고르면 고객 이력과 응대 기록이 보여요.</p>`;

function weekday(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return WEEKDAYS[new Date(y, m - 1, d).getDay()];
}

async function loadRecall({ plan = false } = {}) {
  if (plan) await post("/api/recall/plan", { offset_days: recall.offset });
  recall.data = await api(`/api/recall?offset_days=${recall.offset}`);
  renderRecall();
  clearTimeout(recall.pollTimer);
  if (recall.data.items.some((i) => i.status === "drafting")) recall.pollTimer = setTimeout(() => loadRecall(), 1500);
}

function statusTag(item) {
  const labels = {
    drafting: ["new", "작성 중"],
    ready: ["new", "승인 대기"],
    scheduled: ["status-scheduled", item.send_label],
    sent: ["status-sent", item.send_label],
    skipped: ["status-escalated", item.note === "예약 취소" ? "예약 취소" : "건너뜀"],
    error: ["status-error", "오류"],
  };
  const [cls, label] = labels[item.status] || ["", item.status];
  return `<span class="tag status ${cls}">${label}</span>`;
}

function recallCard(item) {
  const kindTag = `<span class="tag ${item.kind === "광고성" ? "ad" : "info"}">${item.kind}</span>`;
  const deliveryTag = `<span class="tag">${item.delivery === "template" ? "알림톡" : "AI 개인화"}</span>`;
  // 보낼 시각은 서버가 계산한 값(광고 야간 제한·지난 예정 시각 반영)을 그대로 보여준다.
  const when = item.status === "ready" && item.send_label ? (item.send_now ? "승인하면 바로 발송" : `${item.send_label} 발송 예정`) : "";
  let body;
  if (item.status === "drafting") {
    body = `<div class="loading"><div class="spinner"></div>CRM 이력을 보고 메시지를 쓰는 중이에요…</div>`;
  } else if (item.status === "error") {
    body = `<div class="alert urgent">${esc(item.note)}</div>`;
  } else if (item.delivery === "template" || item.status !== "ready") {
    body = `<div class="fixed-text">${esc(item.text)}</div>`;
    if (item.delivery === "template" && item.status === "ready") {
      body += `<p class="hint">알림톡은 검수된 문구에 이름·시술·날짜 같은 값만 바뀌어 들어가요. 문구를 바꾸려면 템플릿을 다시 검수받아야 해요.</p>`;
    }
  } else {
    body = `<textarea data-edit="${item.id}">${esc(recall.edits[item.id] ?? item.text)}</textarea>
      ${item.notes ? `<div class="alert human"><strong>발송 전 확인</strong>${esc(item.notes)}</div>` : ""}
      ${item.note ? `<div class="alert note">${esc(item.note)}</div>` : ""}
      ${item.prompt ? `<details><summary>AI에 보낸 내용 보기 · 개인정보 ${item.masked || 0}건 가림</summary><pre class="prompt">${esc(item.prompt)}</pre></details>` : ""}`;
  }
  const flags = ["ready", "sent", "scheduled"].includes(item.status) ? `<div class="flags" data-flags="${item.id}">${renderFlags(item.flags)}</div>` : "";
  const hasBlocking = (item.flags || []).some((f) => f.blocking);
  let buttons = "";
  if (item.status === "ready") {
    buttons = `<div class="buttons">
      <button class="primary" data-approve="${item.id}" ${hasBlocking ? "disabled" : ""}>${item.send_now ? "승인하고 발송" : `승인 (${esc(item.send_label)} 예약 발송)`}</button>
      ${item.delivery === "ai" ? `<button data-regen="${item.id}">다시 쓰기</button>` : ""}
      <button data-skip="${item.id}">건너뛰기</button>
    </div>${hasBlocking ? `<p class="hint">형식 의무를 고쳐야 승인할 수 있어요.</p>` : ""}`;
  } else if (item.status === "scheduled") {
    buttons = `<div class="buttons"><button data-cancel="${item.id}">예약 취소</button></div>
      <p class="hint">보내기 직전에 수신 동의·채널 차단·답하지 않은 문의를 다시 확인해요.</p>`;
  }
  return `<article class="recall-card ${item.status} ${item.id === recall.selected ? "selected" : ""}" data-id="${item.id}">
    <header><span class="name">${esc(item.customer_name)}</span><span class="muted">· ${esc(item.rule_name)}</span>${kindTag}${deliveryTag}${statusTag(item)}</header>
    <p class="reason">${esc(item.reason)} · ${esc(item.channel_label)} → ${esc(item.recipient_display)}${when ? ` · ${when}` : ""}</p>
    ${body}${flags}${buttons}
  </article>`;
}

function renderRecall() {
  const d = recall.data;
  const active = d.items.filter((i) => i.status !== "blocked");
  const blocked = d.items.filter((i) => i.status === "blocked");
  const count = (status) => active.filter((i) => i.status === status).length;

  $("#recall-date").textContent = `${fmtDate(d.date)} (${weekday(d.date)})${recall.offset ? ` · 오늘 +${recall.offset}일` : " · 오늘"}`;
  $("#day-prev").disabled = recall.offset === 0;
  $("#recall-summary").textContent = `승인 대기 ${count("ready")} · 작성 중 ${count("drafting")} · 예약 ${count("scheduled")} · 발송 완료 ${count("sent")} · 보류 ${blocked.length}`;
  const infoReady = active.filter((i) => i.status === "ready" && i.delivery === "template" && i.kind === "정보성" && !i.flags.length).length;
  const bulk = $("#approve-info");
  bulk.textContent = `정보성 알림 일괄 승인 (${infoReady})`;
  bulk.disabled = !infoReady;

  $("#rule-list").innerHTML = d.rules.map((r) => `<li>
      <div class="rule-name">${esc(r.name)}<span class="count ${r.count ? "" : "zero"}">${r.count}</span></div>
      <div class="timing">${esc(r.timing)}</div>
      <div class="tags"><span class="tag ${r.kind === "광고성" ? "ad" : "info"}">${r.kind}</span><span class="tag">${r.delivery === "template" ? "알림톡" : "AI 개인화"}</span></div>
    </li>`).join("");

  $("#recall-queue").innerHTML = active.length
    ? active.map(recallCard).join("")
    : `<div class="empty-queue">이 날짜에 보낼 리콜 메시지가 없어요. ▶로 다른 날짜를 확인해 보세요.</div>`;
  $("#blocked-title").style.display = blocked.length ? "" : "none";
  $("#recall-blocked").innerHTML = blocked.map((i) => `<div class="blocked-row" data-id="${i.id}">
      <span class="name">${esc(i.customer_name)}</span>
      <span>${esc(i.rule_name)} · ${esc(i.reason)}</span>
      <span class="why">${esc(i.block_reason)}</span>
    </div>`).join("");
}

async function selectRecall(id) {
  recall.selected = id;
  recall.selectedCustomer = recall.data?.items.find((i) => i.id === id)?.customer_id ?? recall.selectedCustomer;
  document.querySelectorAll(".recall-card").forEach((el) => el.classList.toggle("selected", Number(el.dataset.id) === id));
  const view = await api(`/api/recall/items/${id}`);
  if (recall.selected === id) renderCRM(view, $("#recall-crm"));
}

async function changeDay(offset) {
  recall.offset = Math.max(0, offset);
  recall.selected = null;
  $("#recall-crm").innerHTML = EMPTY_RECALL_CRM;
  await loadRecall({ plan: true });
}

// ---- 이벤트 (카드는 다시 그려지므로 부모에 한 번만 건다) ----------------------

$("#recall-queue").addEventListener("click", async (e) => {
  const approveId = e.target.dataset.approve;
  const regenId = e.target.dataset.regen;
  const skipId = e.target.dataset.skip;
  const cancelId = e.target.dataset.cancel;
  if (cancelId) {
    try {
      await post(`/api/recall/items/${cancelId}/cancel`);
      toast("예약을 취소했어요.");
      await loadRecall();
    } catch (err) {
      toast(err.message);
    }
  } else if (approveId) {
    const item = recall.data.items.find((i) => i.id === Number(approveId));
    const medicalFlags = item.flags.filter((f) => !f.blocking);  // 형식 의무 위반은 서버가 막는다
    if (medicalFlags.length && !confirm(`위험 표현이 ${medicalFlags.length}개 있어요. 그대로 보낼까요?`)) return;
    e.target.disabled = true;
    try {
      const view = await post(`/api/recall/items/${approveId}/approve`, { text: recall.edits[approveId] ?? null });
      delete recall.edits[approveId];
      toast(view.status === "scheduled"
        ? `${item.customer_name}님 메시지를 승인했어요 · ${view.send_label}. 보낼 때 CRM 응대 기록에 남겨요.`
        : `${item.customer_name}님 메시지를 보냈고 CRM 응대 기록에 남겼어요.`);
      await loadRecall();
      if (recall.selected === item.id) selectRecall(item.id);
    } catch (err) {
      toast(err.message);
      e.target.disabled = false;
    }
  } else if (regenId) {
    e.target.disabled = true;
    e.target.textContent = "쓰는 중…";
    delete recall.edits[regenId];
    try {
      await post(`/api/recall/items/${regenId}/regenerate`);
      await loadRecall();
      toast("메시지를 다시 썼어요.");
    } catch (err) {
      toast(err.message);
    }
  } else if (skipId) {
    try {
      await post(`/api/recall/items/${skipId}/skip`, { reason: "실장이 건너뜀" });
    } catch (err) {
      toast(err.message);
    }
    await loadRecall();
  } else {
    const card = e.target.closest(".recall-card");
    if (card && !e.target.closest("textarea, details, button")) selectRecall(Number(card.dataset.id));
  }
});

$("#recall-queue").addEventListener("input", (e) => {
  const id = e.target.dataset.edit;
  if (!id) return;
  recall.edits[id] = e.target.value;
  const item = recall.data.items.find((i) => i.id === Number(id));
  clearTimeout(recall.checkTimers[id]);
  recall.checkTimers[id] = setTimeout(async () => {
    // 승인 때와 같은 규칙(발송 경로별 수신거부 문구·위치·글자 수)으로 서버가 점검한다.
    const { flags } = await post(`/api/recall/items/${id}/check`, { text: e.target.value });
    item.flags = withReview(flags, item.flags);
    const box = document.querySelector(`[data-flags="${id}"]`);
    if (box) box.innerHTML = renderFlags(item.flags);
    const approveBtn = document.querySelector(`[data-approve="${id}"]`);
    if (approveBtn) approveBtn.disabled = item.flags.some((f) => f.blocking);
  }, 350);
});

$("#recall-blocked").addEventListener("click", (e) => {
  const row = e.target.closest(".blocked-row");
  if (row) selectRecall(Number(row.dataset.id));
});

$("#day-prev").addEventListener("click", () => changeDay(recall.offset - 1));
$("#day-next").addEventListener("click", () => changeDay(recall.offset + 1));
$("#day-today").addEventListener("click", () => changeDay(0));

$("#approve-info").addEventListener("click", async () => {
  const { approved } = await post("/api/recall/approve-info", { offset_days: recall.offset });
  toast(`정보성 알림 ${approved}건을 승인했어요.`);
  await loadRecall();
});

// 다시 계획하면 멈춘 항목이 새 항목으로 바뀐다. 같은 고객의 지금 항목을 다시 고르고, 없으면 패널을 비운다.
async function reselectRecall(customerId) {
  const same = recall.data.items.filter((i) => i.customer_id === customerId);
  const next = same.find((i) => i.id === recall.selected) || same.find((i) => i.status !== "blocked") || same[0];
  if (next) return selectRecall(next.id);
  recall.selected = null;
  $("#recall-crm").innerHTML = EMPTY_RECALL_CRM;
}

window.openRecallView = async () => {
  if (!recall.selected) $("#recall-crm").innerHTML = EMPTY_RECALL_CRM;
  await loadRecall({ plan: true });
  if (recall.selected) await reselectRecall(recall.selectedCustomer);  // 문의함에서 바뀐 동의가 반영되게 다시 읽는다
};

window.onDemoReset = () => {
  recall.offset = 0;
  recall.selected = null;
  recall.selectedCustomer = null;
  recall.edits = {};
  $("#recall-crm").innerHTML = EMPTY_RECALL_CRM;
  if (document.body.classList.contains("view-recall")) loadRecall({ plan: true });
};
