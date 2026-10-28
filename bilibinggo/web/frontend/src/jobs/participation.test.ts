// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { participationSucceeded, renderTripleParticipationResults } from "./index";
import { buildActivityParticipateBtn, renderActivityTableRow, renderActivityCard } from "../activities/index";

describe("participation result safety", () => {
  it("keeps exclusion available and participation visibly disabled while the account is paused", () => {
    const html = buildActivityParticipateBtn({ dynamic_id: "100000000000000001", activity_status: "未参加",
      participation_guard: { state: "ready" }, can_participate: false, participation_block_reason: "账号队列暂停：HTTP 412" });
    expect(html).toContain("参与</button>");
    expect(html).toContain("disabled");
    expect(html).toContain("账号队列暂停：HTTP 412");
    expect(html).toContain('data-guard-exclude="100000000000000001"');
    expect(html).not.toContain('data-action="participate"');
  });
  it("shows exclusion as the main status and ended as separate lifecycle information", () => {
    const item = { dynamic_id: "100000000000000001", activity_status: "已排除", activity_lifecycle_status: "已结束",
      participation_guard: { state: "excluded", reason: "9月30日开奖" }, can_participate: false };
    for (const render of [renderActivityTableRow, renderActivityCard]) {
      const html = render(item);
      expect(html).toContain("已排除");
      expect(html).toContain("活动已结束");
      expect(html).toContain("排除原因：9月30日开奖");
      expect(html).not.toContain('data-action="participate"');
    }
  });
  it("uses the actual required actions and tolerates optional failures", () => {
    expect(participationSucceeded([
      { action: "repost", ok: true, required: true },
      { action: "favorite", ok: false, required: false },
    ], "转发抽奖")).toBe(true);
    expect(participationSucceeded([
      { action: "repost", ok: true, required: true },
      { action: "comment", ok: false, required: true, state: "pending" },
    ], "转发抽奖")).toBe(false);
  });

  it.each([
    ["review", "待核查"], ["account_paused", "账号暂停"],
    ["cancelled", "用户取消"], ["partial", "部分完成"],
  ])("distinguishes %s in batch results", (status, label) => {
    expect(renderTripleParticipationResults({ items: [
      { dynamic_id: "100000000000000001", status, message: "原始原因", actions: [] },
    ] })).toContain(label);
  });

  it("offers verification instead of participation for quarantined activities", () => {
    const html = buildActivityParticipateBtn({ dynamic_id: "100000000000000001", can_participate: false,
      participation_guard: { state: "review", reason: "<原始原因>" } });
    expect(html).toContain("核查恢复");
    expect(html).not.toContain('data-action="participate"');
    expect(html).toContain("&lt;原始原因&gt;");
  });
});
