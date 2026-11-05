import type { Profile } from "./api";

export const PROFILE_STATUS_LABEL: Record<Profile["status"], string> = {
  running: "Đang chạy",
  stopped: "Đã dừng",
  initializing: "Đang khởi động",
};

export const KEY_STATE_LABEL: Record<string, string> = {
  valid: "hợp lệ",
  invalid: "không hợp lệ",
  unreachable: "không kết nối được",
  unknown: "chưa kiểm tra",
};

export const TIER_LABEL: Record<string, string> = {
  pro: "Pro",
  free: "Key miễn phí",
  none: "Chưa có key",
};

export function timeAgo(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "-";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return "vừa xong";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} phút trước`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} giờ trước`;
  return `${Math.round(hours / 24)} ngày trước`;
}
