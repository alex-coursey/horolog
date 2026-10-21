/** Wire types - mirror of the Pydantic models in `horolog/api.py`.
 *
 *  Hand-written for now. Once the API is stable these should be generated from
 *  its OpenAPI schema (`openapi-typescript`) so the two cannot drift; until
 *  then this file is the seam to check when an endpoint changes.
 */

export type IntentKind = "task" | "habit" | "focus" | "buffer" | "meeting";
export type Priority = 1 | 2 | 3 | 4;
export type EnergyLevel = "high" | "medium" | "low";

export interface Block {
  color?: string | null;
  tags?: string[];
  intent_id: string;
  title: string;
  kind: IntentKind;
  priority: Priority;
  energy?: EnergyLevel;
  occurrence: number;
  chunk: number;
  start: string;
  end: string;
  moved_from: string | null;
  deadline?: string | null;
  late_by_minutes?: number;
  completed: boolean;
  recurring: boolean;
}

export interface Unmet {
  intent_id: string;
  title: string;
  priority: Priority;
  occurrence: number;
  shortfall_minutes: number;
}

export function unmetCalendarConflict(plan: Plan, intent: Intent, unmet: Unmet): { day: string; label: string } | null {
  if (intent.period_days !== 7 || intent.max_per_day !== 1 || !intent.allowed_weekdays?.length ||
      intent.min_chunk_minutes !== intent.max_chunk_minutes ||
      intent.minutes_per_period !== intent.min_chunk_minutes * intent.allowed_weekdays.length) return null;
  const origin = plan.origin.slice(0, 10);
  const year = Number(origin.slice(0, 4));
  const month = Number(origin.slice(5, 7));
  const date = Number(origin.slice(8, 10));
  for (let offset = unmet.occurrence * 7; offset < (unmet.occurrence + 1) * 7; offset++) {
    const candidate = new Date(Date.UTC(year, month - 1, date + offset));
    const day = candidate.toISOString().slice(0, 10);
    const weekday = (candidate.getUTCDay() + 6) % 7;
    if (!intent.allowed_weekdays.includes(weekday) ||
        intent.skipped_dates?.includes(day) ||
        plan.blocks.some((block) => block.intent_id === intent.id && block.occurrence === unmet.occurrence && block.start.slice(0, 10) === day)) continue;
    const conflict = plan.busy.find((event) => event.start.slice(0, 10) <= day && event.end.slice(0, 10) > day &&
      event.start.slice(11, 16) === "00:00" && event.end.slice(11, 16) === "00:00");
    if (conflict) return { day, label: conflict.label || "A full-day calendar event" };
  }
  return null;
}

export interface Busy {
  label: string;
  start: string;
  end: string;
  source: string;
}

export interface Plan {
  blocks: Block[];
  unmet: Unmet[];
  busy: Busy[];
  solve_ms: number;
  complete: boolean;
  generated_at: string;
  origin: string;
  horizon_days: number;
}

export interface DailyWindow {
  start_min: number;
  end_min: number;
}

export interface Intent {
  id: string;
  title: string;
  kind: IntentKind;
  priority: Priority;
  energy?: EnergyLevel | null;
  minutes_per_period: number;
  period_days: number | null;
  allowed_weekdays?: number[] | null;
  min_chunk_minutes: number;
  max_chunk_minutes: number;
  max_per_day?: number | null;
  daily_windows?: DailyWindow[];
  earliest_slot?: number | null;
  due_slot?: number | null;
  preferred_start_min?: number | null;
  /** Set once, on a one-shot task only - see `complete`/`uncomplete` below. */
  completed_at?: string | null;
  completed_dates?: string[];
  skipped_dates?: string[];
  subtasks?: Subtask[];
  /** Slot ranges (not clock times) other attendees are busy - present only
   *  on meeting-kind intents. Its length is the useful part for display. */
  blocked_slots?: [number, number][];
  /** Set automatically when HOROLOG_ZOOM_* is configured server-side - a
   *  no-fixed-time meeting link, present only on meeting-kind intents. */
  zoom_join_url?: string | null;
}

export interface Subtask {
  id: string;
  title: string;
  for_date?: string | null;
  completed_at?: string | null;
  completed_dates?: string[];
}

export function subtaskProgress(intent: Intent, day: string): { done: number; total: number } {
  const items = (intent.subtasks ?? []).filter((item) => !item.for_date || item.for_date === day);
  return {
    done: items.filter((item) => intent.period_days == null
      ? Boolean(item.completed_at)
      : (item.completed_dates ?? []).includes(day)).length,
    total: items.length,
  };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    // The API puts a human-readable reason in `detail` for every 4xx it raises
    // deliberately - surface that rather than a bare status code.
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* non-JSON error body; keep the status line */
    }
    throw new Error(detail);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export const api = {
  plan: () => request<Plan>("/api/plan"),
  resolve: () => request<Plan>("/api/plan/solve", { method: "POST" }),
  intents: () => request<Intent[]>("/api/intents"),
  remove: (id: string) => request<void>(`/api/intents/${id}`, { method: "DELETE" }),
  update: (id: string, body: Record<string, unknown>) =>
    request<Intent>(`/api/intents/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  complete: (id: string) => request<Intent>(`/api/intents/${id}/complete`, { method: "POST" }),
  uncomplete: (id: string) =>
    request<Intent>(`/api/intents/${id}/complete`, { method: "DELETE" }),
  completeSession: (id: string, day: string) =>
    request<Intent>(`/api/intents/${id}/completed-dates/${day}`, { method: "PUT" }),
  uncompleteSession: (id: string, day: string) =>
    request<Intent>(`/api/intents/${id}/completed-dates/${day}`, { method: "DELETE" }),
  addSubtask: (id: string, title: string, day?: string) =>
    request<Intent>(`/api/intents/${id}/subtasks${day ? `?day=${day}` : ""}`, {
      method: "POST", body: JSON.stringify({ title }),
    }),
  setSubtaskComplete: (id: string, subtaskId: string, complete: boolean, day?: string) =>
    request<Intent>(`/api/intents/${id}/subtasks/${subtaskId}/complete${day ? `?day=${day}` : ""}`, {
      method: complete ? "PUT" : "DELETE",
    }),
  removeSubtask: (id: string, subtaskId: string) =>
    request<Intent>(`/api/intents/${id}/subtasks/${subtaskId}`, { method: "DELETE" }),
  skipSession: (id: string, day: string) =>
    request<{ task: Intent; plan: Plan }>(`/api/intents/${id}/skips/${day}`, { method: "POST" }),
  capture: (text: string, provider?: string, model?: string, apiKey?: string) =>
    request<{ intent: Intent }>("/api/capture", {
      method: "POST",
      body: JSON.stringify({ text, provider, model, api_key: apiKey }),
    }),
  setBusy: (events: Omit<Busy, "source">[]) =>
    request<{ events: number; blocks: number }>("/api/busy", {
      method: "PUT",
      body: JSON.stringify(events),
    }),
};

export async function toggleBlockCompletion(block: Block): Promise<void> {
  if (block.recurring) {
    const day = block.start.slice(0, 10);
    if (block.completed) await api.uncompleteSession(block.intent_id, day);
    else await api.completeSession(block.intent_id, day);
  } else if (!block.completed) {
    await api.complete(block.intent_id);
  }
}

export function minutesBetween(start: string, end: string): number {
  return Math.round((Date.parse(end) - Date.parse(start)) / 60000);
}

export function localDateKey(date: Date = new Date()): string {
  return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, "0"), String(date.getDate()).padStart(2, "0")].join("-");
}

export function formatDuration(minutes: number): string {
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (!hours) return `${rest}m`;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

// --------------------------------------------------------------- analytics

export interface Slice {
  label: string;
  minutes: number;
  share: number;
}

export interface DayLoad {
  day: number;
  scheduled_minutes: number;
  meeting_minutes: number;
  longest_free_run_minutes: number;
}

export interface Analytics {
  horizon_days: number;
  window_minutes_per_day: number;
  focus_minutes: number;
  meeting_minutes: number;
  scheduled_minutes: number;
  unmet_minutes: number;
  meeting_load: number;
  fragmentation: number;
  longest_focus_run_minutes: number;
  after_hours_minutes: number;
  by_kind: Slice[];
  by_priority: Slice[];
  days: DayLoad[];
}

export const analytics = {
  get: () => request<Analytics>("/api/analytics"),
};

/** Every syncable provider. Google/Outlook are calendars — they land in the
 *  busy mirror; the rest are trackers — they land as tasks. Linear/Todoist/
 *  GitHub are OAuth-connectable (appear in `connections.list()`); Notion/
 *  ClickUp/Jira are pasted-credential only — see their integrations/*.py
 *  docstrings for why — and never appear in that list. */
export type Provider =
  | "google"
  | "outlook"
  | "linear"
  | "todoist"
  | "github"
  | "notion"
  | "clickup"
  | "jira";

export const sync = {
  ics: (url: string) =>
    request<{ events: number; blocks: number }>("/api/sync/ics", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),
  caldav: (url: string, username: string, password: string) =>
    request<{ events: number; blocks: number }>("/api/sync/caldav", {
      method: "POST",
      body: JSON.stringify({ url, username, password }),
    }),
  // A pasted key is optional on all three trackers — omitted, the API falls
  // back to whatever OAuth connection is already stored server-side.
  linear: (apiKey = "") =>
    request<{ issues: number; blocks: number }>("/api/sync/linear", {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey }),
    }),
  todoist: (token = "") =>
    request<{ tasks: number; blocks: number }>("/api/sync/todoist", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
  github: (token = "") =>
    request<{ issues: number; blocks: number }>("/api/sync/github", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
  // These three have no OAuth fallback — a credential is required, not optional.
  notion: (credential: string) =>
    request<{ tasks: number; blocks: number }>("/api/sync/notion", {
      method: "POST",
      body: JSON.stringify({ token: credential }),
    }),
  clickup: (credential: string) =>
    request<{ tasks: number; blocks: number }>("/api/sync/clickup", {
      method: "POST",
      body: JSON.stringify({ token: credential }),
    }),
  jira: (credential: string) =>
    request<{ issues: number; blocks: number }>("/api/sync/jira", {
      method: "POST",
      body: JSON.stringify({ token: credential }),
    }),
  google: () => request<{ events: number; blocks: number }>("/api/sync/google", { method: "POST" }),
  outlook: () =>
    request<{ events: number; blocks: number }>("/api/sync/outlook", { method: "POST" }),
};

export interface CalendarPushResult {
  created: number;
  moved: number;
  removed: number;
}

export const calendarPush = {
  /** Pushes the plan onto the connected provider's "Horolog" calendar right
   *  now. 409s if write-back is off (`HOROLOG_CALENDAR_WRITEBACK_ENABLED`)
   *  or the provider isn't connected; 502 with a reconnect message if the
   *  stored token predates the write scope. */
  push: (provider: "google" | "outlook") =>
    request<CalendarPushResult>("/api/calendar/push", {
      method: "POST",
      body: JSON.stringify({ provider }),
    }),
};

export const connections = {
  /** Which providers have a live, usable OAuth connection right now. */
  list: () => request<Record<Provider, boolean>>("/api/connections"),
  disconnect: (provider: Provider) =>
    request<void>(`/api/connections/${provider}`, { method: "DELETE" }),
};

// --------------------------------------------------------------- booking

export interface FreeSlot {
  start: string;
  end: string;
}

export interface Booked {
  start: string;
  end: string;
  /** How many of your own blocks the solver moved to make room. */
  rescheduled_blocks: number;
}

export const booking = {
  availability: (minutes: number, days: number) =>
    request<FreeSlot[]>(`/api/availability?minutes=${minutes}&days=${days}`),
  book: (body: { name: string; email: string; start: string; minutes: number }) =>
    request<Booked>("/api/book", { method: "POST", body: JSON.stringify(body) }),
};

export const createIntent = (body: Record<string, unknown>) =>
  request<Intent>("/api/intents", { method: "POST", body: JSON.stringify(body) });

/** A span one attendee is busy - the solver treats these as blocked only for
 *  the meeting requirement they're attached to, never the shared calendar. */
export interface AttendeeBusy {
  start: string;
  end: string;
  attendee?: string;
}

export const KIND_ORDER: IntentKind[] = ["focus", "task", "habit", "meeting", "buffer"];

export const PRIORITY_NAME: Record<Priority, string> = {
  1: "Critical",
  2: "High",
  3: "Normal",
  4: "Low",
};

/** Accent tint per priority - one hue, four weights. Shared by every view so
 *  the same block reads the same way on the grid, in the inbox, and in a chart. */
export const PRIORITY_TINT: Record<Priority, string> = {
  1: "color-mix(in srgb, var(--color-accent) 100%, transparent)",
  2: "color-mix(in srgb, var(--color-accent) 62%, transparent)",
  3: "color-mix(in srgb, var(--color-accent) 38%, transparent)",
  4: "color-mix(in srgb, var(--color-accent) 20%, transparent)",
};
