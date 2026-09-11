import { Plus, Search, Monitor } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
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

function getProfileSearchText(profile: Profile): string {
  return [profile.name, profile.proxy, profile.notes]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

interface TagFilter {
  tag: string;
  color: string | null;
  count: number;
}

type FilterMode = "all" | "ungrouped" | "tags";

function getTagFilters(profiles: Profile[]): TagFilter[] {
  const tags = new Map<string, TagFilter>();

  profiles.forEach((profile) => {
    profile.tags.forEach((tag) => {
      const current = tags.get(tag.tag);
      if (current) {
        current.count += 1;
        current.color = current.color ?? tag.color;
        return;
      }
      tags.set(tag.tag, { tag: tag.tag, color: tag.color, count: 1 });
    });
  });

  return Array.from(tags.values()).sort((a, b) =>
    a.tag.localeCompare(b.tag, undefined, { numeric: true, sensitivity: "base" }),
  );
}

function filterButtonClass(active: boolean): string {
  return [
    "shrink-0 rounded-full border px-2 py-1 text-[11px] font-medium transition-colors",
    "focus:outline-none focus:ring-1 focus:ring-accent/50",
    active
      ? "border-accent bg-accent text-white"
      : "border-border bg-surface-2 text-gray-400 hover:bg-surface-3 hover:text-gray-200",
  ].join(" ");
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
      className={`w-full text-left px-3 py-1 rounded-md mb-1 transition-colors ${
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
      {(profile.tags.length > 0 || profile.notes) && (
        <div className="mt-1 ml-4 flex min-w-0 items-center gap-1.5">
          {profile.tags.map((t) => (
            <span
              key={t.tag}
              className="shrink-0 max-w-24 truncate text-[10px] px-1.5 py-0.5 rounded-full bg-surface-4 text-gray-400"
              style={t.color ? { backgroundColor: `${t.color}20`, color: t.color } : undefined}
              title={t.tag}
            >
              {t.tag}
            </span>
          ))}
          {profile.notes && (
            <span className="min-w-0 truncate text-xs text-gray-500" title={profile.notes}>
              {profile.notes}
            </span>
          )}
        </div>
      )}
    </button>
  );
}

export function ProfileList({ profiles, selectedId, onSelect, onNew, onReorder }: ProfileListProps) {
  const [search, setSearch] = useState("");
  const [filterMode, setFilterMode] = useState<FilterMode>("all");
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const normalizedSearch = search.trim().toLowerCase();
  const tagFilters = useMemo(() => getTagFilters(profiles), [profiles]);
  const selectedTagSet = useMemo(() => new Set(selectedTags), [selectedTags]);
  const ungroupedCount = useMemo(
    () => profiles.filter((profile) => profile.tags.length === 0).length,
    [profiles],
  );

  useEffect(() => {
    const availableTags = new Set(tagFilters.map((tag) => tag.tag));
    setSelectedTags((current) => {
      const next = current.filter((tag) => availableTags.has(tag));
      return next.length === current.length ? current : next;
    });
  }, [tagFilters]);

  useEffect(() => {
    if (filterMode === "tags" && selectedTags.length === 0) {
      setFilterMode("all");
    }
  }, [filterMode, selectedTags.length]);

  const selectAll = () => {
    setFilterMode("all");
    setSelectedTags([]);
  };

  const selectUngrouped = () => {
    setFilterMode("ungrouped");
    setSelectedTags([]);
  };

  const toggleTagFilter = (tag: string) => {
    const nextTags = selectedTagSet.has(tag)
      ? selectedTags.filter((selected) => selected !== tag)
      : [...selectedTags, tag];
    setSelectedTags(nextTags);
    setFilterMode(nextTags.length === 0 ? "all" : "tags");
  };

  const filtered = profiles.filter((profile) => {
    const matchesSearch = getProfileSearchText(profile).includes(normalizedSearch);
    const matchesTag = (() => {
      if (filterMode === "all") return true;
      if (filterMode === "ungrouped") return profile.tags.length === 0;
      if (selectedTags.length === 0) return true;

      const profileTags = new Set(profile.tags.map((tag) => tag.tag));
      return selectedTags.every((tag) => profileTags.has(tag));
    })();
    return matchesSearch && matchesTag;
  });

  const runningCount = profiles.filter((p) => p.status === "running").length;

  // Reordering is disabled while any filter is active because dragging within
  // a filtered subset is ambiguous. All + empty search preserves list order.
  const dragEnabled = normalizedSearch === "" && filterMode === "all";

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
        {profiles.length > 0 && (
          <div
            role="group"
            aria-label="Profile tag filters"
            className="-mx-1 mt-3 flex flex-wrap gap-1 px-1 pb-1"
          >
            <button
              type="button"
              aria-label={`All ${profiles.length}`}
              aria-pressed={filterMode === "all"}
              onClick={selectAll}
              className={filterButtonClass(filterMode === "all")}
            >
              <span>All</span>
              <span className="ml-1 opacity-70">{profiles.length}</span>
            </button>
            <button
              type="button"
              aria-label={`Ungrouped ${ungroupedCount}`}
              aria-pressed={filterMode === "ungrouped"}
              onClick={selectUngrouped}
              className={filterButtonClass(filterMode === "ungrouped")}
            >
              <span>Ungrouped</span>
              <span className="ml-1 opacity-70">{ungroupedCount}</span>
            </button>
            {tagFilters.map((tag) => (
              <button
                key={tag.tag}
                type="button"
                aria-label={`${tag.tag} ${tag.count}`}
                aria-pressed={filterMode === "tags" && selectedTagSet.has(tag.tag)}
                onClick={() => toggleTagFilter(tag.tag)}
                className={filterButtonClass(filterMode === "tags" && selectedTagSet.has(tag.tag))}
                title={tag.tag}
              >
                <span>{tag.tag}</span>
                <span className="ml-1 opacity-70">{tag.count}</span>
              </button>
            ))}
          </div>
        )}
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
