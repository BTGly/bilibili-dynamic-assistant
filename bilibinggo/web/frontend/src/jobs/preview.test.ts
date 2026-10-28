// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { state } from "../state";
import { startJob } from "./index";
import { fetchJSON } from "../api/client";
vi.mock("../api/client", () => ({ fetchJSON: vi.fn() }));

describe("unattended participation", () => {
  beforeEach(() => {
    state.account = { logged_in: true };
    state.settings = {};
    vi.mocked(fetchJSON).mockReset();
    vi.mocked(fetchJSON).mockImplementation(async (url) => {
      if (url === "/api/jobs") throw new Error("submitted-stop-before-polling");
      throw new Error(`unexpected request ${url}`);
    });
  });
  afterEach(() => vi.restoreAllMocks());
  it.each(["participate", "participate_triple"])("submits %s without per-message prompts or LLM setup", async (action) => {
    const prompt = vi.spyOn(window, "prompt").mockReturnValue(null);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await expect(startJob(action, { dynamic_id: "1" })).rejects.toThrow("submitted-stop-before-polling");
    expect(fetchJSON).toHaveBeenCalledTimes(1);
    const [url, options] = vi.mocked(fetchJSON).mock.calls[0];
    expect(url).toBe("/api/jobs");
    expect(JSON.parse(options!.body as string)).toEqual({ action, params: { dynamic_id: "1" } });
    expect(prompt).not.toHaveBeenCalled();
    expect(confirm).not.toHaveBeenCalled();
  });
});
