import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { StatusIndicator } from "./StatusIndicator";
import type { ProfileLifecycle } from "../lib/api";

// Every lifecycle state must be visually distinguishable. Sharing a colour
// between "starting" and "stopping" would make a profile that is going away
// look like one that is coming up, which is the difference between "wait" and
// "click Launch again".
const EXPECTED_DOT: Record<ProfileLifecycle, string> = {
  running: "bg-emerald-400",
  starting: "bg-yellow-400",
  stopping: "bg-orange-400",
  stopped: "bg-gray-500",
};

describe("StatusIndicator", () => {
  it.each(Object.entries(EXPECTED_DOT))("renders %s with its own dot colour", (status, cls) => {
    const { container } = render(
      <StatusIndicator status={status as ProfileLifecycle} />,
    );
    const dots = Array.from(container.querySelectorAll("span"));
    expect(dots.some((d) => d.className.includes(cls))).toBe(true);
    // and no other state's colour leaked in
    for (const [other, otherCls] of Object.entries(EXPECTED_DOT)) {
      if (other === status) continue;
      expect(dots.some((d) => d.className.includes(otherCls))).toBe(false);
    }
  });

  it("animates every non-terminal state and only those", () => {
    const pings = (status: ProfileLifecycle) => {
      const { container } = render(<StatusIndicator status={status} />);
      return container.querySelector(".animate-ping") !== null;
    };
    expect(pings("running")).toBe(true);
    expect(pings("starting")).toBe(true);
    expect(pings("stopping")).toBe(true);
    expect(pings("stopped")).toBe(false);
  });
});
