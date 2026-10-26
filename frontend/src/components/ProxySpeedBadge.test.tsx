import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const proxyHealth = vi.fn();

vi.mock("../lib/api", () => ({
  api: { proxyHealth: () => proxyHealth() },
}));

import { ProxySpeedBadge, describe as describeSpeed, pickRow } from "./ProxySpeedBadge";
import type { ProxyHealthRow } from "../lib/api";

const row = (over: Partial<ProxyHealthRow> = {}): ProxyHealthRow => ({
  profile: "facebook-013",
  profile_id: "id-13",
  status: "running",
  proxy: "floppydata-facebook-013",
  server: "http://country-de.g-w.info:10080",
  ip: "107.77.215.36",
  family: "v6",
  drifted: [],
  country: "US",
  city: "Gainesville",
  timezone: "America/New_York",
  profile_timezone: "America/New_York",
  place_match: true,
  network: "AT&T",
  kbps: 12,
  tls_sec: 0.86,
  ratio: 0.12,
  verdict: "slow",
  measured_at: "2026-10-26T15:00:00Z",
  reputation: "green",
  ...over,
});

const health = (profiles: ProxyHealthRow[]) => ({ expectation: { kbps: 100, tls_sec: 2.5 }, at: "", profiles });

describe("ProxySpeedBadge", () => {
  beforeEach(() => proxyHealth.mockReset());
  // Each badge polls; an unmounted one would keep calling the next test's mock.
  afterEach(() => cleanup());

  it("shows the speed against the expectation and names it too slow", async () => {
    proxyHealth.mockResolvedValue(health([row()]));
    render(<ProxySpeedBadge nativeName={null} sidebarOpen={false} />);

    await waitFor(() => expect(screen.getByText("12")).toBeTruthy());
    expect(screen.getByText("/ 100 kbps expected")).toBeTruthy();
    expect(screen.getByText(/too slow to draw/)).toBeTruthy();
    expect(screen.getByText("TLS 0.86s")).toBeTruthy();
  });

  it("prefers the selected profile over the worst line", async () => {
    proxyHealth.mockResolvedValue(
      health([
        row({ profile: "facebook-005", kbps: 1, ratio: 0.01, verdict: "slow" }),
        row({ profile: "facebook-013", kbps: 140, ratio: 1.4, verdict: "good" }),
      ]),
    );
    render(<ProxySpeedBadge nativeName="facebook-013" sidebarOpen />);

    await waitFor(() => expect(screen.getByText("140")).toBeTruthy());
    expect(screen.getByText("140% of expectation")).toBeTruthy();
  });

  it("falls back to the worst line when the selected profile is not proxied", async () => {
    proxyHealth.mockResolvedValue(health([row({ profile: "facebook-005", kbps: 1, ratio: 0.01 })]));
    render(<ProxySpeedBadge nativeName="facebook-010" sidebarOpen />);
    await waitFor(() => expect(screen.getByText("1")).toBeTruthy());
  });

  it("says a line is unmeasured instead of implying it is healthy", async () => {
    proxyHealth.mockResolvedValue(health([row({ kbps: null, ratio: null, tls_sec: null, verdict: "unmeasured" })]));
    render(<ProxySpeedBadge nativeName={null} sidebarOpen />);

    await waitFor(() => expect(screen.getByText(/no measurement yet/)).toBeTruthy());
    expect(screen.getByText(/cloak proxy check facebook-013 --speed/)).toBeTruthy();
  });

  it("hides for the session and is back on the next load", async () => {
    proxyHealth.mockResolvedValue(health([row()]));
    const first = render(<ProxySpeedBadge nativeName={null} sidebarOpen={false} />);
    await waitFor(() => expect(screen.getByTestId("proxy-speed-badge")).toBeTruthy());
    fireEvent.click(screen.getByLabelText("Hide the proxy-speed add-on"));
    expect(screen.queryByTestId("proxy-speed-badge")).toBeNull();
    fireEvent.click(screen.getByTestId("proxy-speed-restore"));
    expect(screen.getByTestId("proxy-speed-badge")).toBeTruthy();
    first.unmount();
    // Every load starts with the line in view: the hide is not remembered.
    render(<ProxySpeedBadge nativeName={null} sidebarOpen={false} />);
    expect(screen.getByTestId("proxy-speed-badge")).toBeTruthy();
  });

  it("shows where the exit is and what is expected of it", async () => {
    proxyHealth.mockResolvedValue(health([row()]));
    render(<ProxySpeedBadge nativeName={null} sidebarOpen={false} />);
    await waitFor(() => expect(screen.getByText("Gainesville, US")).toBeTruthy());
    expect(screen.getByText("107.77.215.36")).toBeTruthy();
    expect(screen.getByText("expect ≥100 kbps · TLS ≤2.5s")).toBeTruthy();
    expect(screen.queryByText("place")).toBeNull();
  });

  it("flags an exit that sits in a different place than the profile expects", async () => {
    proxyHealth.mockResolvedValue(
      health([row({ city: "Paris", country: "FR", timezone: "Europe/Paris", profile_timezone: "America/New_York", place_match: false })]),
    );
    render(<ProxySpeedBadge nativeName={null} sidebarOpen={false} />);
    await waitFor(() => expect(screen.getByText("Paris, FR")).toBeTruthy());
    expect(screen.getByText("place")).toBeTruthy();
  });

  it("says nothing is proxied instead of inventing a number", async () => {
    proxyHealth.mockResolvedValue(health([]));
    render(<ProxySpeedBadge nativeName={null} sidebarOpen />);
    await waitFor(() => expect(screen.getByText(/No native profile with a proxy/)).toBeTruthy());
  });
});

describe("speed maths", () => {
  it("describes a shortfall and a surplus the same way", () => {
    expect(describeSpeed(row({ kbps: 12, ratio: 0.12 }), 100).delta).toBe("12% of expectation · 88% short");
    expect(describeSpeed(row({ kbps: 140, ratio: 1.4 }), 100).delta).toBe("140% of expectation");
  });

  it("never divides by a missing ratio", () => {
    const out = describeSpeed(row({ kbps: 50, ratio: null }), 100);
    expect(out.value).toBe("50 / 100 kbps");
    expect(out.fill).toBeCloseTo(0.5);
  });

  it("keeps an unmeasured row at zero fill", () => {
    expect(describeSpeed(row({ kbps: null, ratio: null }), 100).fill).toBe(0);
  });

  it("picks the selected row, else the first", () => {
    const rows = [row({ profile: "a" }), row({ profile: "b" })];
    expect(pickRow(rows, "b")?.profile).toBe("b");
    expect(pickRow(rows, "zzz")?.profile).toBe("a");
    expect(pickRow([], "a")).toBeNull();
  });
});
