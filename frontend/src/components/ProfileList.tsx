import { Globe, GripVertical, Plus, Search, Shield, X } from "lucide-react";
import { useState } from "react";
import {
  DndContext,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import type { Profile } from "../lib/api";
import { StatusIndicator } from "./StatusIndicator";

interface ProfileListProps {
  profiles: Profile[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onReorder: (orderedIds: string[]) => void;
}

type StatusFilter = "all" | "running" | "stopped";

interface RowProps {
  profile: Profile;
  selected: boolean;
  draggable: boolean;
  onSelect: (id: string) => void;
}

/** Show only host:port — proxy URLs can carry credentials that must not leak. */
function proxyHost(proxy: string | null): string | null {
  if (!proxy) return null;
  try {
    const url = new URL(proxy.includes("://") ? proxy : `http://${proxy}`);
    return url.host;
  } catch {
    return proxy.replace(/^[^@]*@/, "");
  }
}

function SortableProfileRow({ profile, selected, draggable, onSelect }: RowProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: profile.id, disabled: !draggable });

  const host = proxyHost(profile.proxy);

  return (
    <button
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      onClick={() => onSelect(profile.id)}
      {...attributes}
      {...listeners}
      data-selected={selected}
      className={`sidebar-item group mb-1 ${draggable ? "cursor-grab active:cursor-grabbing" : ""} ${
        isDragging ? "opacity-50 scale-[0.98]" : ""
      }`}
    >
      <div className="flex items-center gap-2">
        {draggable && (
          <GripVertical className="h-3.5 w-3.5 -ml-1 flex-shrink-0 text-fg-muted/40 opacity-0 group-hover:opacity-100 transition-opacity" />
        )}
        <StatusIndicator status={profile.status} />
        <span className="truncate text-sm font-medium text-fg group-hover:text-white transition-colors">
          {profile.name}
        </span>
        {profile.last_error && (
          <span
            className="ml-auto h-2 w-2 flex-shrink-0 rounded-full bg-warning ring-2 ring-warning/25"
            title={profile.last_error.message}
          />
        )}
      </div>
      {host && (
        <div className="ml-4 mt-1 flex items-center gap-1.5 truncate font-mono text-2xs text-fg-muted" title="Proxy">
          <Globe className="h-3 w-3 flex-shrink-0 text-fg-muted/70" />
          <span className="truncate">{host}</span>
        </div>
      )}
      {profile.tags.length > 0 && (
        <div className="ml-4 mt-1.5 flex flex-wrap gap-1">
          {profile.tags.map((t) => (
            <span
              key={t.tag}
              className="badge-neutral"
              style={
                t.color
                  ? {
                      backgroundColor: `${t.color}18`,
                      color: t.color,
                      borderColor: `${t.color}35`,
                    }
                  : undefined
              }
            >
              {t.tag}
            </span>
          ))}
        </div>
      )}
    </button>
  );
}

export function ProfileList({ profiles, selectedId, onSelect, onNew, onReorder }: ProfileListProps) {
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");

  const filtered = profiles.filter((p) => {
    if (statusFilter === "running" && p.status !== "running") return false;
    if (statusFilter === "stopped" && p.status !== "stopped") return false;
    return p.name.toLowerCase().includes(search.toLowerCase());
  });

  const runningCount = profiles.filter((p) => p.status === "running").length;

  // Reordering is disabled while any filter is active — dragging within a
  // filtered subset is ambiguous. With no filter, filtered === profiles order.
  const dragEnabled = search === "" && statusFilter === "all";

  // A small activation distance so a plain click still selects (no accidental drag).
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  );

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const ids = profiles.map((p) => p.id);
    const from = ids.indexOf(String(active.id));
    const to = ids.indexOf(String(over.id));
    if (from === -1 || to === -1) return;
    onReorder(arrayMove(ids, from, to));
  };

  const filters: { key: StatusFilter; label: string }[] = [
    { key: "all", label: "Tất cả" },
    { key: "running", label: "Đang chạy" },
    { key: "stopped", label: "Đã dừng" },
  ];

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="border-b border-line p-3.5">
        <div className="mb-3.5 flex items-center gap-2.5">
          <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-brand to-brand-deep text-white shadow-glow">
            <Shield className="h-4 w-4" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <h1 className="truncate text-sm font-semibold tracking-tight text-fg">CloakBrowser</h1>
              <span className="badge-neutral flex-shrink-0">Tùy chỉnh</span>
            </div>
            <p className="truncate text-2xs text-fg-muted">Quản lý danh tính trình duyệt</p>
          </div>
        </div>

        {/* Search */}
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-muted" />
          <input
            type="text"
            placeholder="Tìm hồ sơ..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="input h-8 pl-8 pr-7 text-xs"
          />
          {search && (
            <button
              type="button"
              onClick={() => setSearch("")}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-fg-muted hover:text-fg"
              title="Xóa tìm kiếm"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>

        {/* Status filter */}
        <div className="mt-2.5 flex items-center justify-between gap-1">
          <div className="flex items-center gap-0.5 rounded-lg border border-line bg-surface-0/80 p-0.5">
            {filters.map((f) => (
              <button
                key={f.key}
                onClick={() => setStatusFilter(f.key)}
                aria-pressed={statusFilter === f.key}
                className={`rounded-md px-2 py-1 text-2xs font-medium transition-all ${
                  statusFilter === f.key
                    ? "bg-brand-subtle text-brand shadow-sm"
                    : "text-fg-muted hover:text-fg-secondary"
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>
          <span className="inline-flex items-center gap-1 text-2xs text-fg-muted">
            <span className={`h-1.5 w-1.5 rounded-full ${runningCount > 0 ? "bg-success" : "bg-fg-muted/50"}`} />
            {runningCount} đang chạy
          </span>
        </div>
      </div>

      {/* Profile list */}
      <div className="flex-1 overflow-y-auto p-2.5">
        {filtered.length === 0 && (
          <div className="py-10 text-center text-xs text-fg-muted">
            {profiles.length === 0 ? "Chưa có hồ sơ" : "Không có kết quả"}
          </div>
        )}
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
          <SortableContext items={filtered.map((p) => p.id)} strategy={verticalListSortingStrategy}>
            {filtered.map((profile) => (
              <SortableProfileRow
                key={profile.id}
                profile={profile}
                selected={selectedId === profile.id}
                draggable={dragEnabled}
                onSelect={onSelect}
              />
            ))}
          </SortableContext>
        </DndContext>
      </div>

      {/* New profile button */}
      <div className="border-t border-line p-3 bg-surface-1/80">
        <button onClick={onNew} className="btn-primary w-full">
          <Plus className="h-3.5 w-3.5" />
          <span>Hồ sơ mới</span>
        </button>
      </div>
    </div>
  );
}
