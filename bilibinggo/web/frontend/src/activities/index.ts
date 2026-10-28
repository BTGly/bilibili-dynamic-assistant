// @ts-nocheck
/* eslint-disable */
/** Migrated from web/static/app.js — logic preserved. */

import { state } from "../state";
import { fetchJSON } from "../api/client";
import { isLoggedIn } from "../account/index";
import { activitiesBody, activitiesCards, filterDrawWindowHint, filterResultSummary, pagination, statsGrid } from "../dom";
import { bindActionButtons, updateJobUI } from "../jobs/index";
import { showToast } from "../shell/toast";
import { activityStatusTone, badgeClass, formatFilterSummary, formatHeat, formatLastParticipation, formatLotteryTime, isLotterySoon, lotteryTypeTone } from "../utils/format";
import { animateStatValue, flashFilterPill, playActivityListEnter, pulseFilterSummary, setButtonLoading } from "../utils/motion";
import { escapeHtml, truncateText } from "../utils/text";
import { renderSources } from "../watch/index";

export function renderStats(summary) {
  if (!statsGrid) return;
  const counts = summary.user_status_counts || {};
  const drawCounts = summary.counts || {};
  const cards = [
    { key: "total", label: "活动总数", value: summary.total_count || 0 },
    { key: "pending", label: "未参加", value: counts["未参加"] || 0 },
    { key: "review", label: "时间未知·自动参加", value: counts["待审核"] || 0 },
    { key: "joined", label: "已参加", value: counts["已参加"] || 0 },
    { key: "ended", label: "已结束", value: counts["已结束"] || 0 },
    { key: "excluded", label: "已排除", value: counts["已排除"] || 0 },
    { key: "active", label: "进行中", value: drawCounts.active || 0 },
    { key: "new", label: "上次新入库", value: summary.new_count ?? 0 },
  ];
  const previous = state.statValues || {};
  statsGrid.innerHTML = cards
    .map(
      (card, index) => `
      <article class="stat-card is-entering" style="--card-delay:${index * 55}ms" data-stat-key="${card.key}">
        <p class="stat-label">${card.label}</p>
        <p class="stat-value">${previous[card.key] ?? card.value}</p>
      </article>`
    )
    .join("");
  cards.forEach((card) => {
    const valueEl = statsGrid.querySelector(`[data-stat-key="${card.key}"] .stat-value`);
    animateStatValue(valueEl, previous[card.key] ?? card.value, card.value);
  });
  state.statValues = Object.fromEntries(cards.map((card) => [card.key, card.value]));
}

export function buildActivityReviewHint(item) {
  const guard = item.participation_guard || {};
  const review = item.time_review || {};
  const decision = review.decision === "allow_manual" ? "上次判定：可参加"
    : review.decision === "defer" ? "上次判定：暂缓" : "";
  return [
    item.activity_title ? `标题：${truncateText(String(item.activity_title), 36)}` : "",
    item.prize && item.prize !== "—" ? `奖品：${truncateText(String(item.prize), 28)}` : "",
    `开奖：${item.lottery_time || "未知"}`,
    `参与截止：${item.participation_deadline || "未知"}`,
    item.lottery_time_evidence ? `正文原句：${truncateText(String(item.lottery_time_evidence), 52)}` : "",
    decision,
    guard.state === "excluded" ? `已被人工排除：${truncateText(String(guard.reason || ""), 52)}` : "",
  ].filter(Boolean).join("\n");
}

export async function nextTripleSlotLabel() {
  const status = await fetchJSON("/api/auto/status").catch(() => null);
  const slot = status?.next_slot;
  const slotAt = Number(slot?.at_unix || 0);
  if (!slot || slot.kind !== "triple" || !slotAt) return "";
  return new Date(slotAt * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

export function buildActivityParticipateBtn(item) {
  if (item.can_participate) {
    const queued = item.activity_status === "待审核"
      ? ' <span class="caption" title="开奖时间未知：已按新政策直接进三连队列，排在有开奖时间的活动之后">时间未知·已排队列尾部</span>'
      : "";
    return `<button class="btn btn-primary btn-compact btn-pill" data-action="participate" data-dynamic-id="${escapeHtml(item.dynamic_id)}">参与</button> <button class="btn btn-compact" data-guard-exclude="${escapeHtml(item.dynamic_id)}">排除</button>${queued}`;
  }
  const guard = item.participation_guard;
  const hint = escapeHtml(buildActivityReviewHint(item));
  const released = guard?.state === "excluded" ? ' data-review-released="1"' : "";
  const timeButton = item.activity_status === "待审核"
    ? `<button class="btn btn-primary btn-compact btn-pill" data-review-join="${escapeHtml(item.dynamic_id)}"${released} data-review-hint="${hint}">可参加</button>` +
      `<button class="btn btn-compact" data-review-skip="${escapeHtml(item.dynamic_id)}" data-review-hint="${hint}">不参加</button>`
    : "";
  const excludeButton = guard?.state === "excluded" || timeButton
    ? ""
    : `<button class="btn btn-compact" data-guard-exclude="${escapeHtml(item.dynamic_id)}">排除</button>`;
  if (guard && guard.state !== "ready") {
    const labels = { review: "待核查", deferred: "暂缓", excluded: "人工排除" };
    if (timeButton) {
      return `<span class="caption" title="${escapeHtml(guard.reason || '')}">${labels[guard.state] || "已隔离"}</span>${timeButton}`;
    }
    return `<span class="caption" title="${escapeHtml(guard.reason || '')}">${labels[guard.state] || "已隔离"}</span>
      <button class="btn btn-compact" data-guard-release="${escapeHtml(item.dynamic_id)}">核查恢复</button>${timeButton}${excludeButton}`;
  }
  const disabledParticipate = item.activity_status === "未参加" ? `<button class="btn btn-primary btn-compact btn-pill" disabled title="${escapeHtml(item.participation_block_reason || '当前暂不能参与，请查看账号队列提示')}">参与</button><span class="caption">暂不能参与</span>` : "";
  return `${disabledParticipate}${timeButton}${excludeButton}`;
}

export function buildActivityLastNote(item) {
  const guard = item.participation_guard;
  const progressNote = item.activity_status === "已排除" && item.activity_lifecycle_status === "已结束" ? `<div class="caption">活动已结束</div>` : "";
  const guardNote = progressNote + (guard?.state !== "ready" && guard?.reason ? `<div class="caption">${guard.state === 'excluded' ? '排除' : '隔离'}原因：${escapeHtml(guard.reason)}</div>` : "");
  if (!item.last_participation) return guardNote;

  return `<div class="last-result ${escapeHtml(item.last_participation.status || "")}">上次：${escapeHtml(formatLastParticipation(item.last_participation))}</div>${guardNote}`;
}

export function buildActivityLink(item) {
  if (item.source_url) {
    return `<a class="activity-link" href="${escapeHtml(item.source_url)}" target="_blank" rel="noopener">打开动态</a>`;
  }
  return `<span class="caption">—</span>`;
}

export function renderActivityTableRow(item, index = 0) {
  const title = escapeHtml(item.activity_title || item.prize || "未知活动");
  const statusTone = activityStatusTone(item.activity_status);
  const soon = isLotterySoon(item.lottery_time);
  const typeTone = lotteryTypeTone(item.lottery_type);
  return `
    <tr class="activity-row is-${statusTone}${soon ? " is-soon" : ""}" data-dynamic-id="${escapeHtml(item.dynamic_id || "")}" style="--row-delay:${Math.min(index, 12) * 28}ms">
      <td class="activity-cell">
        <div class="activity-title">${item.seq ? `<span class="seq-tag">#${item.seq}</span> ` : ""}${item.sender_uid ? `<span class="seq-tag">UP${item.sender_uid}·${item.up_seq || "?"}</span> ` : ""}${title}</div>
        ${buildActivityLastNote(item)}
      </td>
      <td class="link-cell">${buildActivityLink(item)}</td>
      <td class="chip-cell"><span class="type-chip type-chip--${typeTone}">${escapeHtml(item.lottery_type)}</span></td>
      <td class="heat-cell"><span class="heat-pill${item.heat_missing ? " heat-pill-missing" : ""}">${formatHeat(item)}</span></td>
      <td class="chip-cell"><span class="${badgeClass(item.activity_status)}">${escapeHtml(item.activity_status)}</span>${item.archived ? `<div class="caption">已归档 ${escapeHtml(item.archived_reason || "")}</div>` : ""}</td>
      <td class="time-cell"><span class="time-pill${soon ? " is-soon" : ""}">开奖：${escapeHtml(formatLotteryTime(item.lottery_time))}</span>
        <div class="caption" title="${escapeHtml(item.participation_deadline_evidence || '')}">参与截止：${escapeHtml(formatLotteryTime(item.participation_deadline))}</div>
        ${buildTimeCaption(item)}</td>
      <td class="chip-cell action-cell">${buildActivityParticipateBtn(item)}</td>
    </tr>`;
}

export function renderActivityCard(item, index = 0) {
  const title = escapeHtml(item.activity_title || item.prize || "未知活动");
  const statusTone = activityStatusTone(item.activity_status);
  const ended = statusTone === "ended" ? " is-ended" : "";
  const soon = isLotterySoon(item.lottery_time);
  const typeTone = lotteryTypeTone(item.lottery_type);
  return `
    <article class="activity-card is-${statusTone}${ended}${soon ? " is-soon" : ""}" data-dynamic-id="${escapeHtml(item.dynamic_id || "")}" style="--row-delay:${Math.min(index, 12) * 28}ms">
      <div class="activity-card-head">
        <h3 class="activity-card-title">${item.seq ? `<span class="seq-tag">#${item.seq}</span> ` : ""}${title}</h3>
        <span class="${badgeClass(item.activity_status)}">${escapeHtml(item.activity_status)}</span>
      </div>
      ${buildActivityLastNote(item)}
      <div class="activity-card-meta">
        <span class="type-chip type-chip--${typeTone}">${escapeHtml(item.lottery_type)}</span>
        <span class="heat-pill${item.heat_missing ? " heat-pill-missing" : ""}">${formatHeat(item)}</span>
        <span class="time-pill${soon ? " is-soon" : ""}">开奖：${escapeHtml(formatLotteryTime(item.lottery_time))}</span>
        <div class="caption" title="${escapeHtml(item.participation_deadline_evidence || '')}">参与截止：${escapeHtml(formatLotteryTime(item.participation_deadline))}</div>
        ${buildTimeCaption(item)}
      </div>
      <div class="activity-card-actions">
        ${buildActivityLink(item)}
        ${buildActivityParticipateBtn(item)}
      </div>
    </article>`;
}

export function renderActivities(payload) {
  const items = payload.items || [];

  if (filterResultSummary) {
    filterResultSummary.innerHTML = formatFilterSummary(payload);
    filterResultSummary.hidden = false;
    pulseFilterSummary();
  }

  if (!items.length) {
    activitiesBody.innerHTML = `<tr class="empty-row"><td colspan="7"><div class="activity-empty">没有匹配的活动<span class="activity-empty-hint">试试调整筛选或换个关键词</span></div></td></tr>`;
    if (activitiesCards) {
      activitiesCards.innerHTML = `<div class="activity-empty">没有匹配的活动<span class="activity-empty-hint">试试调整筛选或换个关键词</span></div>`;
    }
  } else {
    activitiesBody.innerHTML = items.map((item, index) => renderActivityTableRow(item, index)).join("");
    if (activitiesCards) {
      activitiesCards.innerHTML = items.map((item, index) => renderActivityCard(item, index)).join("");
    }
    playActivityListEnter();
  }

  const page = Number(payload.page) || 1;
  const pages = Math.max(1, Number(payload.pages) || 1);
  const total = Number(payload.total) || 0;
  pagination.innerHTML = `
    <span class="caption pagination-summary">第 ${page} / ${pages} 页 · 本页 ${items.length} 条 · 共 ${total} 条</span>
    <div class="action-row pagination-actions">
      <button class="btn btn-secondary btn-compact btn-pill" id="page-prev" ${page <= 1 ? "disabled" : ""}>上一页</button>
      <button class="btn btn-secondary btn-compact btn-pill" id="page-next" ${page >= pages ? "disabled" : ""}>下一页</button>
    </div>`;

  document.getElementById("page-prev")?.addEventListener("click", () => {
    state.page = Math.max(1, state.page - 1);
    loadActivities();
  });
  document.getElementById("page-next")?.addEventListener("click", () => {
    state.page += 1;
    loadActivities();
  });
  bindActionButtons();
  document.querySelectorAll("[data-review-join]").forEach((button) => {
    button.addEventListener("click", async () => {
      const dynamicId = button.dataset.reviewJoin;
      const hint = button.dataset.reviewHint || "";
      const wasExcluded = button.dataset.reviewReleased === "1";
      const note = `把这一条排进三连队列（不会立刻转发，由下一刻度按每轮 3 条的额度参加）。\n\n${hint}\n\n活动 ${dynamicId}${wasExcluded ? "\n\n这条此前被人工排除，点确定会先解除排除。" : ""}`;
      if (!window.confirm(note)) return;
      setButtonLoading(button, true, { label: "排队中…" });
      try {
        const stamp = new Date().toLocaleString();
        if (wasExcluded) {
          await fetchJSON(`/api/participation/guards/${dynamicId}/release`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ evidence: `面板人工改判 ${stamp}：点「可参加」解除人工排除` })
          });
        }
        await fetchJSON(`/api/activities/${dynamicId}/time-review`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision: "allow_manual", evidence: `面板人工审核 ${stamp}：确认可参加` })
        });
        const slotAt = await nextTripleSlotLabel();
        showToast(slotAt ? `已排进三连队列，下一刻度 ${slotAt} 自动参加` : "已批准并进队列，等调度器恢复后参加", "success");
        await loadActivities();
      } catch (error) { setButtonLoading(button, false); showToast(error.message, "error"); }
    });
  });
  document.querySelectorAll("[data-review-skip]").forEach((button) => {
    button.addEventListener("click", async () => {
      const dynamicId = button.dataset.reviewSkip;
      const hint = button.dataset.reviewHint || "";
      const who = hint.split("\n")[0] || `活动 ${dynamicId}`;
      setButtonLoading(button, true, { label: "已排除" });
      try {
        await fetchJSON(`/api/participation/guards/${dynamicId}/exclude`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: `面板人工审核不参加：${who.slice(0, 60)}` })
        });
        showToast("已标记不参加，随时点「可参加」撤回", "success");
        await loadActivities();
      } catch (error) { setButtonLoading(button, false); showToast(error.message, "error"); }
    });
  });
  // 人工审核为「不参加」后该行 guard=excluded，走上面的分支渲染「核查恢复」，无需额外按钮
  document.querySelectorAll("[data-guard-exclude]").forEach((button) => {
    button.addEventListener("click", async () => {
      const reason = window.prompt("填写人工排除原因，排除后不会自动入选：");
      if (!reason?.trim()) return;
      try {
        await fetchJSON(`/api/participation/guards/${button.dataset.guardExclude}/exclude`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason }) });
        await loadActivities();
      } catch (error) { showToast(error.message, "error"); }
    });
  });
  document.querySelectorAll("[data-guard-release]").forEach((button) => {
    button.addEventListener("click", () => releaseParticipationGuard(button.dataset.guardRelease));
  });
  const preview = resolveTripleTargets(payload);
  if (preview) applyTripleTargets(preview);
}

export async function loadSummary() {
  const summary = await fetchJSON("/api/summary");
  renderStats(summary);
  const guards = await fetchJSON("/api/participation/guards");
  let notice = document.getElementById("participation-account-pause");
  if (!notice && statsGrid) { notice = document.createElement("div"); notice.id = "participation-account-pause"; statsGrid.after(notice); }
  if (notice) {
    const account = guards.items?.[""];
    notice.innerHTML = account?.state === "account_paused" ? `<p>账号队列已暂停：${escapeHtml(account.reason || '')} <button class="btn btn-compact" id="release-account-queue">核查后恢复</button></p>` : "";
    document.getElementById("release-account-queue")?.addEventListener("click", async () => { await releaseParticipationGuard("account"); await loadSummary(); });
  }
  renderSources(summary.sources);
  updateJobUI(summary.job || { state: "idle" });
  return summary.job;
}

export function setFilterPillGroup(selector, activeButton) {
  document.querySelectorAll(selector).forEach((item) => {
    const active = item === activeButton;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", active ? "true" : "false");
  });
  flashFilterPill(activeButton);
}

export function setStatusFilter(value) {
  state.filters.status = value || "";
  const buttons = document.querySelectorAll("[data-filter-status]");
  let matched = false;
  buttons.forEach((button) => {
    const isActive = (button.getAttribute("data-filter-status") || "") === state.filters.status;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-pressed", isActive ? "true" : "false");
    if (isActive) matched = true;
  });
  if (!matched && buttons[0]) {
    buttons[0].classList.add("active");
    buttons[0].setAttribute("aria-pressed", "true");
  }
}

export function setDrawWindowFilter(value) {
  state.filters.drawWindow = value || "";
  document.querySelectorAll("[data-filter-draw-window]").forEach((button) => {
    const isActive = (button.getAttribute("data-filter-draw-window") || "") === state.filters.drawWindow;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-pressed", isActive ? "true" : "false");
    if (isActive) flashFilterPill(button);
  });
  if (filterDrawWindowHint) {
    filterDrawWindowHint.hidden = !state.filters.drawWindow;
  }
}

export function updateDrawWindowHint() {
  if (!filterDrawWindowHint || !state.filters.drawWindow) return;
  filterDrawWindowHint.textContent = "仅筛选你已参加、且 3 天内即将开奖的活动";
}

export function buildActivityFilterQueryParams() {
  const params = new URLSearchParams();
  if (state.filters.draw) params.set("draw", state.filters.draw);
  if (state.filters.q) params.set("q", state.filters.q);
  if (state.filters.type) params.set("type", state.filters.type);
  if (state.filters.status) params.set("status", state.filters.status);
  params.set("archive", state.filters.archive || "active");
  if (state.filters.drawWindow) params.set("draw_window", state.filters.drawWindow);
  if (state.filters.sort) params.set("sort", state.filters.sort);
  if (state.filters.order) params.set("order", state.filters.order);
  return params;
}

export function renderTripleParticipateBar(data) {
  const bar = document.getElementById("triple-participate-bar");
  const descEl = document.getElementById("triple-participate-desc");
  const targetsEl = document.getElementById("triple-participate-targets");
  const btn = document.getElementById("triple-participate-btn");
  const labelEl = document.getElementById("triple-participate-btn-label");
  if (!btn || !targetsEl || !labelEl) return;

  const count = Number(data?.count) || 0;
  const items = data?.items || [];
  const jobRunning = document.body.classList.contains("job-running");
  const setupOk = isLoggedIn();

  let tone = "blocked";
  if (setupOk && jobRunning) tone = "running";
  else if (setupOk && count > 0) tone = "ready";
  else if (setupOk) tone = "empty";

  if (bar) {
    bar.dataset.tone = tone;
    bar.classList.toggle("is-ready", tone === "ready");
    bar.classList.toggle("is-empty", tone === "empty");
    bar.classList.toggle("is-blocked", tone === "blocked");
    bar.classList.toggle("is-running", tone === "running");
  }

  if (!setupOk) {
    if (descEl) descEl.textContent = "登录后，可一键依次参与最多 3 个活动";
    targetsEl.innerHTML = `<span class="triple-participate-empty">需先登录</span>`;
    btn.disabled = true;
    labelEl.textContent = "三连参与";
    return;
  }

  if (count <= 0) {
    if (descEl) descEl.textContent = "当前筛选列表下没有可参与的未参加活动";
    targetsEl.innerHTML = `<span class="triple-participate-empty">暂无可参与目标</span>`;
    btn.disabled = true;
    labelEl.textContent = "三连参与";
    return;
  }

  if (descEl) {
    const sortHint =
      state.filters.sort === "heat"
        ? state.filters.order === "asc"
          ? "（按热度升序）"
          : "（按热度降序）"
        : state.filters.sort === "lottery_time" ? "（按开奖时间排序，未知时间已排除）" : "";
    descEl.textContent =
      count === 1
        ? `将依次参与当前列表最前面的 1 个未参加活动${sortHint}`
        : `将依次参与当前列表最前面的 ${Math.min(count, 3)} 个未参加活动${sortHint}`;
  }
  targetsEl.innerHTML = items
    .map(
      (item, index) => `
      <span class="triple-target-chip is-entering type-${lotteryTypeTone(item.lottery_type)}" style="--chip-delay:${index * 55}ms" title="${escapeHtml(item.activity_title || item.dynamic_id)}">
        <span class="triple-target-chip-index">${index + 1}</span>
        <span class="triple-target-chip-title">${item.seq ? `#${item.seq} ` : ""}${escapeHtml(truncateText(item.activity_title || item.dynamic_id))}</span>
        <span class="triple-target-chip-type">${escapeHtml(item.lottery_type || "")}</span>
      </span>`
    )
    .join("");
  btn.disabled = jobRunning;
  labelEl.textContent = count >= 3 ? "三连参与 (3)" : `三连参与 (${count})`;
}

export function buildActivityFilterJobParams() {
  const params = {};
  if (state.filters.draw) params.draw = state.filters.draw;
  if (state.filters.q) params.q = state.filters.q;
  if (state.filters.type) params.lottery_type = state.filters.type;
  if (state.filters.status) params.status = state.filters.status;
  params.archive = state.filters.archive || "active";
  if (state.filters.drawWindow) params.draw_window = state.filters.drawWindow;
  if (state.filters.sort) params.sort = state.filters.sort;
  if (state.filters.order) params.order = state.filters.order;
  return params;
}

export function getActiveFilterKey() {
  return JSON.stringify(state.filters);
}

export function buildTimeCaption(item) {
  const evidence = String(item.lottery_time_evidence || "");
  if (item.lottery_time_unix) {
    return `<div class="caption" title="${escapeHtml(evidence)}">${evidence ? "开奖时间有原文依据" : "开奖时间来自平台抽奖卡"}</div>`;
  }
  if (item.participation_deadline_unix) {
    return `<div class="caption" title="${escapeHtml(String(item.participation_deadline_evidence || ""))}">正文只写了参与截止，开奖在截止之后 · 已排队列尾部</div>`;
  }
  return `<div class="caption">${item.time_review_required ? "正文没写开奖时间 · 已排队列尾部" : "历史日期，原文依据未保存"}</div>`;
}


export function computeTripleTargetsFromItems(items) {
  // 10-26 政策：开奖时间未知（状态「待审核」）的行也进三连队列，排在有时间的之后
  const eligible = (items || []).filter(
    (item) => item?.can_participate && (item.activity_status === "未参加" || item.activity_status === "待审核")
  );
  const picked = eligible.slice(0, 3);
  return {
    count: picked.length,
    limit: 3,
    items: picked.map((item) => ({
      dynamic_id: String(item.dynamic_id || ""),
      activity_title: String(item.activity_title || item.prize || "未知活动"),
      lottery_type: String(item.lottery_type || ""),
      activity_status: String(item.activity_status || ""),
    })),
  };
}

export function resolveTripleTargets(payload) {
  if (payload?.triple_targets && Array.isArray(payload.triple_targets.items)) {
    return payload.triple_targets;
  }
  if (state.page === 1) {
    return computeTripleTargetsFromItems(payload?.items || []);
  }
  return null;
}

export function applyTripleTargets(data) {
  state.tripleTargets = data || { count: 0, limit: 3, items: [] };
  state.tripleFilterKey = getActiveFilterKey();
  renderTripleParticipateBar(state.tripleTargets);
}

export async function loadTripleTargets() {
  try {
    const query = buildActivityFilterQueryParams().toString();
    const url = query ? `/api/activities/triple-targets?${query}` : "/api/activities/triple-targets";
    const data = await fetchJSON(url);
    applyTripleTargets(data);
  } catch {
    applyTripleTargets({ count: 0, limit: 3, items: [] });
  }
}

export async function loadActivities() {
  const filterKey = getActiveFilterKey();
  const params = new URLSearchParams({
    page: String(state.page),
    page_size: String(state.pageSize),
  });
  buildActivityFilterQueryParams().forEach((value, key) => params.set(key, value));
  try {
    const payload = await fetchJSON(`/api/activities?${params.toString()}`);
    renderActivities(payload);
    const preview = resolveTripleTargets(payload);
    if (preview) {
      applyTripleTargets(preview);
    } else {
      await loadTripleTargets();
    }
    if (state.tripleFilterKey !== filterKey) {
      await loadTripleTargets();
    }
  } catch (error) {
    showToast(String(error.message || error), "error");
  }
}

export function bindFilterPills() {
  document.querySelectorAll("[data-filter-type]").forEach((button) => {
    button.addEventListener("click", () => {
      setFilterPillGroup("[data-filter-type]", button);
      state.filters.type = button.getAttribute("data-filter-type") || "";
      state.page = 1;
      loadActivities();
    });
  });
  document.querySelectorAll("[data-filter-status]").forEach((button) => {
    button.addEventListener("click", () => {
      if (state.filters.drawWindow) {
        setDrawWindowFilter("");
      }
      setFilterPillGroup("[data-filter-status]", button);
      state.filters.status = button.getAttribute("data-filter-status") || "";
      state.page = 1;
      loadActivities();
    });
  });
  document.querySelectorAll("[data-filter-archive]").forEach((button) => {
    button.addEventListener("click", () => {
      setFilterPillGroup("[data-filter-archive]", button);
      state.filters.archive = button.getAttribute("data-filter-archive") || "active";
      state.page = 1;
      loadActivities();
    });
  });
  document.querySelectorAll("[data-filter-sort]").forEach((button) => {
    button.addEventListener("click", () => {
      setFilterPillGroup("[data-filter-sort]", button);
      state.filters.sort = button.getAttribute("data-filter-sort") || "";
      state.filters.order = button.getAttribute("data-filter-order") || "";
      state.page = 1;
      loadActivities();
    });
  });
  document.querySelectorAll("[data-filter-draw-window]").forEach((button) => {
    button.addEventListener("click", () => {
      const value = button.getAttribute("data-filter-draw-window") || "";
      const isActive = button.classList.contains("active");
      if (isActive) {
        setDrawWindowFilter("");
      } else {
        setStatusFilter("");
        setDrawWindowFilter(value);
        updateDrawWindowHint();
      }
      state.page = 1;
      loadActivities();
    });
  });
  const filterQ = document.getElementById("filter-q");
  if (!filterQ) return;
  let searchTimer = null;
  filterQ.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.page = 1;
      state.filters.q = filterQ.value.trim();
      loadActivities();
    }, 320);
  });
}


export async function releaseParticipationGuard(target) {
  try {
    const payload = await fetchJSON("/api/participation/guards");
    const guard = payload.items[target === "account" ? "" : target] || {};
    const resolved_actions = {};
    for (const [name, item] of Object.entries(guard.progress || {})) {
      if (!["unknown", "in_flight"].includes(item.state)) continue;
      const answer = window.prompt(`${name} 的请求结果未知。请到 B 站核查后输入“成功”或“失败”；取消将保留隔离状态。`);
      if (answer !== "成功" && answer !== "失败") return;
      resolved_actions[name] = answer === "成功" ? "succeeded" : "failed";
    }
    const evidence = window.prompt("填写核查依据（例如核查的动态、评论或账号验证结果）。恢复仅允许再次排队，已确认成功的动作仍会跳过。");
    if (!evidence?.trim()) return;
    await fetchJSON(`/api/participation/guards/${target}/release`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ evidence, resolved_actions }) });
    await loadActivities();
    showToast("核查结果已保存，队列状态已恢复", "info");
  } catch (error) { showToast(error.message, "error"); }
}
