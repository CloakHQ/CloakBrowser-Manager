import { Plus, Search, Monitor } from "lucide-react";
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

interface RowProps {
  profile: Profile;
  selected: boolean;
  draggable: boolean;
  onSelect: (id: string) => void;
}

function SortableProfileRow({ profile, selected, draggable, onSelect }: RowProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: profile.id, disabled: !draggable });

  return (
    <button
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      onClick={() => onSelect(profile.id)}
      {...attributes}
      {...listeners}
      className={`w-full text-left px-3 py-2.5 rounded-md mb-1 transition-colors ${
        draggable ? "cursor-grab active:cursor-grabbing" : ""
      } ${isDragging ? "opacity-50" : ""} ${
        selected
          ? "bg-surface-3 border border-border-hover"
          : "hover:bg-surface-2 border border-transparent"
      }`}
    >
      <div className="flex items-center gap-2">
        <StatusIndicator status={profile.status} />
        <span className="text-sm font-medium truncate">{profile.name}</span>
      </div>
      <div className="flex items-center gap-2 mt-1 ml-4">
        {profile.proxy && <span className="text-xs text-gray-500">Proxy</span>}
      </div>
      {profile.tags.length > 0 && (
        <div className="flex gap-1 mt-1.5 ml-4 flex-wrap">
          {profile.tags.map((t) => (
            <span
              key={t.tag}
              className="text-[10px] px-1.5 py-0.5 rounded-full bg-surface-4 text-gray-400"
              style={t.color ? { backgroundColor: `${t.color}20`, color: t.color } : undefined}
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

  const filtered = profiles.filter((p) =>
    p.name.toLowerCase().includes(search.toLowerCase()),
  );

  const runningCount = profiles.filter((p) => p.status === "running").length;

  // Reordering is disabled while a search filter is active — dragging within a
  // filtered subset is ambiguous. Empty search means filtered === profiles order.
  const dragEnabled = search === "";

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

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="p-4 border-b border-border">
        <div className="flex items-center gap-2 mb-3">
          <Monitor className="h-4 w-4 text-accent" />
          <h1 className="text-sm font-semibold tracking-tight">CloakBrowser Manager</h1>
        </div>
        {runningCount > 0 && (
          <div className="text-xs text-gray-500 mb-3">
            {runningCount} running
          </div>
        )}
        {/* Search */}
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-gray-500" />
          <input
            type="text"
            placeholder="Search profiles..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="input pl-8 py-1.5 text-xs"
          />
        </div>
      </div>

      {/* Profile list */}
      <div className="flex-1 overflow-y-auto p-2">
        {filtered.length === 0 && (
          <div className="text-center text-gray-500 text-xs py-8">
            {profiles.length === 0 ? "No profiles yet" : "No matches"}
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
      <div className="p-3 border-t border-border">
        <button onClick={onNew} className="btn-secondary w-full flex items-center justify-center gap-1.5">
          <Plus className="h-3.5 w-3.5" />
          <span>New Profile</span>
        </button>
      </div>
    </div>
  );
}
