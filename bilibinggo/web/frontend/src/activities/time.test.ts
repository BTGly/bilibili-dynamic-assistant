// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

const fetchJSON = vi.fn();
const startJob = vi.fn();
vi.mock("../api/client", () => ({ fetchJSON: (...args: unknown[]) => fetchJSON(...args) }));
vi.mock("../jobs/index", () => ({
  startJob: (...args: unknown[]) => startJob(...args),
  bindActionButtons: vi.fn(),
  updateJobUI: vi.fn(),
}));

import { buildActivityParticipateBtn, buildActivityReviewHint, nextTripleSlotLabel, renderActivityTableRow, renderActivityCard } from "./index";
import { formatLotteryTime, isLotterySoon } from "../utils/format";

describe("activity time review", () => {
  it("keeps unknown and date precision visible without an invented clock", () => {
    expect(formatLotteryTime(null)).toBe("未知");
    expect(formatLotteryTime("2026-10-15")).toBe("2026-10-15");
    expect(isLotterySoon("未知")).toBe(false);
    expect(isLotterySoon("2026-10-15")).toBe(false);
  });

  it("gives two one-click verdicts for unknown dates", () => {
    const html = buildActivityParticipateBtn({ dynamic_id: "1", activity_status: "待审核", can_participate: false });
    expect(html).toContain("可参加");
    expect(html).toContain("不参加");
    expect(html).toContain("data-review-join");
    expect(html).toContain("data-review-skip");
    expect(html).not.toContain('data-action="participate"');
  });

  it("does not double up 排除/核查恢复 on rows that already have the two verdicts", () => {
    const ready = buildActivityParticipateBtn({ dynamic_id: "1", activity_status: "待审核", can_participate: false,
      participation_guard: { state: "ready" } });
    expect(ready).not.toContain("data-guard-exclude");
    const quarantined = buildActivityParticipateBtn({ dynamic_id: "1", activity_status: "待审核", can_participate: false,
      participation_guard: { state: "review", reason: "历史参与记录需核查：0" } });
    expect(quarantined).not.toContain("data-guard-release");
    expect(quarantined).toContain("可参加");
    expect(quarantined).toContain("不参加");
  });

  it("still offers 核查恢复 for excluded rows", () => {
    const html = buildActivityParticipateBtn({ dynamic_id: "1", activity_status: "已排除", can_participate: false,
      participation_guard: { state: "excluded", reason: "面板人工审核：不参加" } });
    expect(html).toContain("data-guard-release");
  });

  it.each([renderActivityTableRow, renderActivityCard])("separates cutoff and draw and escapes evidence", (render) => {
    const html = render({ dynamic_id: "1", activity_status: "待审核", lottery_time: "未知",
      participation_deadline: "2026-10-15", time_review_required: true,
      lottery_time_evidence: '<img src=x onerror="alert(1)">' });
    expect(html).toContain("开奖：未知");
    expect(html).toContain("参与截止：2026-10-15");
    expect(html).toContain("&lt;img");
    expect(html).not.toContain("<img");
  });

  it("把判据带进按钮，并给被排除的行留一条撤回的路", () => {
    const item = { dynamic_id: "9", activity_status: "待审核", can_participate: false,
      activity_title: "抽 Switch 2", prize: "Switch 2", lottery_time: "未知", participation_deadline: "未知",
      lottery_time_evidence: "关注并转发，<b>10月30日</b>抽1位",
      participation_guard: { state: "excluded", reason: "面板人工审核不参加：标题：抽 Switch 2" } };
    const hint = buildActivityReviewHint(item);
    expect(hint).toContain("正文原句：关注并转发");
    expect(hint).toContain("已被人工排除");
    const html = buildActivityParticipateBtn(item);
    expect(html).toContain('data-review-released="1"');
    expect(html).toContain("&lt;b&gt;");
    expect(html).not.toContain("<b>");
  });

  it("「可参加」只排队：读下一刻度时间，不再手动发起参与任务", async () => {
    const now = Math.floor(Date.now() / 1000);
    fetchJSON.mockResolvedValue({ next_slot: { kind: "triple", at_unix: now + 900 } });
    expect(await nextTripleSlotLabel()).toMatch(/\d{1,2}:\d{2}/);
    fetchJSON.mockResolvedValue({ next_slot: { kind: "refresh", at_unix: now + 900 } });
    expect(await nextTripleSlotLabel()).toBe("");
    fetchJSON.mockRejectedValue(new Error("调度器没应答"));
    expect(await nextTripleSlotLabel()).toBe("");
    expect(startJob).not.toHaveBeenCalled();
  });
});
