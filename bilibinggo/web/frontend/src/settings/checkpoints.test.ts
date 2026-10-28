// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("../api/client", () => ({ fetchJSON: vi.fn() }));
vi.mock("../jobs/index", () => ({ startJob: vi.fn() }));
import { fetchJSON } from "../api/client";
import { startJob } from "../jobs/index";
import { loadRefreshCheckpoints } from "./index";

describe("refresh checkpoint review", () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="credential-permissions"></div>';
    vi.mocked(fetchJSON).mockReset();
    vi.mocked(startJob).mockReset();
    vi.mocked(fetchJSON).mockResolvedValue({ items: [{ dynamic_id: "123", state: "review", attempts: 3,
      reason: '<img src=x onerror="alert(1)">', retry_at: 0 }] });
  });
  afterEach(() => vi.restoreAllMocks());

  it("renders original errors as plain text and starts the formal resume task", async () => {
    await loadRefreshCheckpoints();
    expect(document.querySelector("#refresh-checkpoints img")).toBeNull();
    expect(document.querySelector("#refresh-checkpoints")?.textContent).toContain("待核查");
    document.querySelector<HTMLButtonElement>("#refresh-checkpoints button")!.click();
    expect(startJob).toHaveBeenCalledWith("refresh_links", { urls: [], batch_size: 20 });
  });

  it("leaves checkpoints unchanged when evidence entry is cancelled", async () => {
    vi.spyOn(window, "prompt").mockReturnValue(null);
    await loadRefreshCheckpoints();
    document.querySelectorAll<HTMLButtonElement>("#refresh-checkpoints button")[1].click();
    expect(fetchJSON).toHaveBeenCalledTimes(1);
  });

  it("sends explicit review evidence before refreshing the list", async () => {
    vi.spyOn(window, "prompt").mockReturnValue("已核查，页面恢复");
    await loadRefreshCheckpoints();
    document.querySelectorAll<HTMLButtonElement>("#refresh-checkpoints button")[1].click();
    await vi.waitFor(() => expect(fetchJSON).toHaveBeenCalledTimes(3));
    const [url, options] = vi.mocked(fetchJSON).mock.calls[1];
    expect(url).toBe("/api/refresh/checkpoints/123/release");
    expect(JSON.parse(options!.body as string)).toEqual({ evidence: "已核查，页面恢复" });
  });
});
