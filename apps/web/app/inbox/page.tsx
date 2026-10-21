"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Glyph, KIND_LABEL } from "@/app/components/Glyph";
import { Shell } from "@/app/components/Shell";
import { SubtaskChecklist } from "@/app/components/SubtaskChecklist";
import { Skeleton } from "@/app/components/Skeleton";
import Link from "next/link";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  PRIORITY_NAME,
  PRIORITY_TINT,
  api,
  formatDuration,
  localDateKey,
  minutesBetween,
  unmetCalendarConflict,
  toggleBlockCompletion,
  type Intent,
  type Block,
  type Plan,
  type Priority,
} from "@/app/lib/api";
import { Trash2, AlertCircle, Clock, CheckCircle, Circle, Sparkles, Pencil } from "lucide-react";

const CHECK_BUTTON = "mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-fg-subtle transition-[background-color,color,box-shadow,transform] duration-150 hover:bg-sunk hover:text-accent hover:shadow-sm active:scale-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-wait disabled:opacity-40 motion-reduce:transition-none";
const ICON_BUTTON = "mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-fg-subtle opacity-70 transition-[background-color,color,box-shadow,transform,opacity] duration-150 hover:opacity-100 hover:shadow-sm active:scale-90 focus-visible:opacity-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-wait disabled:opacity-40 motion-reduce:transition-none";

/** Ultra-Luxury Task Inbox View for Horolog.
 *  Displays all scheduling intents, their placed time progress bars, and unmet demand callouts.
 */
export default function Inbox() {
  const reducedMotion = useReducedMotion();
  const [view, setView] = useState<"today" | "all">("today");
  const [todayKey, setTodayKey] = useState(() => localDateKey());
  const [intents, setIntents] = useState<Intent[]>([]);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editMinutes, setEditMinutes] = useState(60);
  const [editPriority, setEditPriority] = useState<Priority>(3);

  const load = useCallback(async () => {
    try {
      const [i, p] = await Promise.all([api.intents(), api.plan()]);
      setIntents(i);
      setPlan(p);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not reach the scheduler.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const timer = setInterval(() => {
      const day = localDateKey();
      if (day !== todayKey) {
        setTodayKey(day);
        void load();
      }
    }, 60_000);
    return () => clearInterval(timer);
  }, [todayKey, load]);

  const rows = useMemo(() => {
    const blocks = plan?.blocks ?? [];
    return intents
      .map((intent) => {
        const unmet = plan?.unmet.filter((item) => item.intent_id === intent.id) ?? [];
        const mine = blocks
          .filter((b) => b.intent_id === intent.id)
          .sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
        const scheduled = mine.reduce((sum, b) => sum + minutesBetween(b.start, b.end), 0);
        const demand = intent.minutes_per_period * (intent.period_days
          ? Math.max(1, Math.floor((plan?.horizon_days ?? intent.period_days) / intent.period_days))
          : 1);
        const todayBlocks = mine.filter((b) => b.start.slice(0, 10) === todayKey);
        const today = todayBlocks[0] ?? null;
        const dueToday = intent.period_days == null && !intent.completed_at && intent.due_slot != null && plan != null &&
          localDateKey(new Date(Date.parse(plan.origin) + intent.due_slot * 15 * 60_000)) === todayKey;
        const completedToday = intent.completed_at != null && localDateKey(new Date(intent.completed_at)) === todayKey;
        const sessionDone = intent.period_days != null && (intent.completed_dates ?? []).includes(todayKey);
        const hasGoalsToday = Boolean(intent.subtasks?.some((item) => item.for_date === todayKey));
        return {
          intent,
          blocks: mine,
          scheduled,
          demand,
          short: unmet.reduce((sum, item) => sum + item.shortfall_minutes, 0),
          unmet,
          next: mine.find((b) => !b.completed) ?? null,
          today,
          todayBlocks,
          dueToday,
          completedToday,
          sessionDone,
          hasGoalsToday,
          percent: Math.min(100, Math.round((scheduled / demand) * 100)),
        };
      })
      .sort((a, b) => {
        if (a.short !== b.short) return b.short - a.short;
        if (a.intent.priority !== b.intent.priority) return a.intent.priority - b.intent.priority;
        return (a.next ? Date.parse(a.next.start) : Infinity) -
          (b.next ? Date.parse(b.next.start) : Infinity);
      });
  }, [intents, plan, todayKey]);

  const todayRows = useMemo(() => rows
    .filter((row) => row.intent.kind !== "meeting" && row.intent.kind !== "buffer" &&
      (row.todayBlocks.length > 0 || row.dueToday || row.completedToday || row.sessionDone || row.hasGoalsToday))
    .sort((a, b) => {
      const aDone = a.completedToday || a.sessionDone;
      const bDone = b.completedToday || b.sessionDone;
      if (aDone !== bDone) return aDone ? 1 : -1;
      if (a.today && b.today) return Date.parse(a.today.start) - Date.parse(b.today.start);
      return a.today ? -1 : b.today ? 1 : a.intent.priority - b.intent.priority;
    }), [rows, todayKey]);
  const visibleRows = view === "today" ? todayRows : rows;

  async function remove(id: string) {
    setBusy(id);
    try {
      await api.remove(id);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not remove that.");
    } finally {
      setBusy(null);
    }
  }

  async function toggleComplete(intent: Intent) {
    setBusy(intent.id);
    try {
      if (intent.completed_at) {
        await api.uncomplete(intent.id);
      } else {
        await api.complete(intent.id);
      }
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not update that.");
    } finally {
      setBusy(null);
    }
  }

  async function toggleSession(block: Block) {
    setBusy(block.intent_id);
    try {
      await toggleBlockCompletion(block);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not update that session.");
    } finally {
      setBusy(null);
    }
  }

  async function toggleTodaySession(intent: Intent) {
    setBusy(intent.id);
    try {
      if ((intent.completed_dates ?? []).includes(todayKey)) await api.uncompleteSession(intent.id, todayKey);
      else await api.completeSession(intent.id, todayKey);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not update today's session.");
    } finally {
      setBusy(null);
    }
  }

  async function skipBlockedSession(intentId: string, day: string) {
    setBusy(intentId);
    try {
      await api.skipSession(intentId, day);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not skip that session.");
    } finally {
      setBusy(null);
    }
  }

  function startEdit(intent: Intent) {
    setEditing(intent.id);
    setEditTitle(intent.title);
    setEditMinutes(intent.minutes_per_period);
    setEditPriority(intent.priority);
  }

  async function saveEdit(intent: Intent) {
    if (!plan) return;
    setBusy(intent.id);
    try {
      await api.update(intent.id, {
        title: editTitle.trim() || intent.title,
        minutes_per_period: editMinutes,
        priority: editPriority,
      });
      setEditing(null);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save that edit.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Shell onPlanChange={load}>
      <main className="mx-auto max-w-[920px] px-6 py-8">
        <header className="mb-8 flex flex-wrap items-baseline justify-between gap-4">
          <div>
            <h1 className="text-[28px] font-bold text-fg">Task Inbox</h1>
            <p className="mt-1 text-[13.5px] text-fg-muted">
              {plan ? (
                view === "today" ? (
                  <>{new Date(`${todayKey}T12:00:00`).toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" })} · {todayRows.length} {todayRows.length === 1 ? "thing" : "things"} today · {formatDuration(todayRows.reduce((sum, row) => sum + row.todayBlocks.reduce((minutes, block) => minutes + minutesBetween(block.start, block.end), 0), 0))} scheduled</>
                ) : (
                  <>{rows.length} {rows.length === 1 ? "task" : "tasks"} · {formatDuration(rows.reduce((sum, row) => sum + row.scheduled, 0))} scheduled{!plan.complete && <span className="font-semibold text-danger"> · {plan.unmet.length} did not fit</span>}</>
                )
              ) : (
                "Reading the plan..."
              )}
            </p>
          </div>
        </header>

        <div className="mb-6 flex gap-1 rounded-xl border border-border bg-sunk p-1 shadow-inner" role="tablist" aria-label="Task Inbox views">
          {(["today", "all"] as const).map((option) => (
            <button key={option} id={`inbox-tab-${option}`} type="button" role="tab" aria-controls="inbox-task-panel" aria-selected={view === option} tabIndex={view === option ? 0 : -1} onClick={() => { setEditing(null); setView(option); }} onKeyDown={(event) => {
              const next = event.key === "ArrowRight" || event.key === "End" ? "all" : event.key === "ArrowLeft" || event.key === "Home" ? "today" : null;
              if (next) {
                event.preventDefault();
                setEditing(null);
                setView(next);
                document.getElementById(`inbox-tab-${next}`)?.focus();
              }
            }} className={`relative flex-1 rounded-lg px-4 py-2 text-[13px] font-semibold transition-[color,transform] duration-150 hover:text-fg active:scale-[0.98] focus-visible:z-10 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent motion-reduce:transition-none ${view === option ? "text-fg" : "text-fg-muted"}`}>
              {view === option && <motion.span layoutId="inbox-active-tab" className="absolute inset-0 rounded-lg border border-border/60 bg-surface shadow-sm" transition={reducedMotion ? { duration: 0 } : { type: "spring", stiffness: 500, damping: 42 }} aria-hidden />}
              <span className="relative z-10">{option === "today" ? "Today" : "All tasks"}</span>
            </button>
          ))}
        </div>

        {error && (
          <div className="mb-6 rounded-card border border-red-200 bg-red-50/70 p-4 text-[13.5px] text-danger shadow-xs">
            {error}
          </div>
        )}

        <AnimatePresence mode="wait" initial={false}>
        <motion.div key={view} id="inbox-task-panel" role="tabpanel" aria-labelledby={`inbox-tab-${view}`} initial={reducedMotion ? false : { opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={reducedMotion ? undefined : { opacity: 0, y: -5 }} transition={reducedMotion ? { duration: 0 } : { duration: 0.18, ease: "easeOut" }}>
        {plan && visibleRows.length === 0 && !error && (
          <div className="rounded-card border border-black/[0.06] bg-surface p-12 text-center shadow-sm">
            <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-secondary text-indigo-600">
              <Sparkles size={22} />
            </div>
            <p className="text-[16px] font-semibold text-fg">{view === "today" ? "Nothing on today's task list" : "Nothing scheduled yet"}</p>
            <p className="mx-auto mt-1.5 max-w-sm text-[13.5px] leading-relaxed text-fg-muted">
              {view === "today" ? <>Your schedule has no tasks for today. <button type="button" onClick={() => setView("all")} className="rounded px-1 font-semibold text-accent underline underline-offset-2 transition-colors hover:bg-sunk hover:text-fg active:bg-border focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">View all tasks</button> or press <kbd className="rounded-md border border-border bg-sunk px-1.5 py-0.5 font-mono text-[11px]">⌘K</kbd> to add one.</> : <>Press <kbd className="tabular rounded-md border border-black/5 bg-sunk px-2 py-0.5 text-[11.5px] font-mono">⌘K</kbd> and describe what you need time for.</>}
            </p>
          </div>
        )}

        {!plan && !error && (
          <ul aria-hidden className="space-y-3">
            {Array.from({ length: 4 }).map((_, i) => (
              <li key={i} className="rounded-card border border-black/[0.06] bg-surface p-4.5 shadow-sm">
                <div className="flex items-start gap-4">
                  <Skeleton className="mt-1 h-10 w-1 shrink-0 rounded-full" />
                  <Skeleton className="mt-1 h-[18px] w-[18px] shrink-0 rounded-full" />
                  <div className="min-w-0 flex-1 space-y-2.5">
                    <Skeleton className="h-4 w-2/5" />
                    <Skeleton className="h-3 w-3/5" />
                    <Skeleton className="h-1.5 w-full rounded-full" />
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}

        <ul className="space-y-3">
          {visibleRows.map(({ intent, blocks, scheduled, demand, unmet, next, today, todayBlocks, dueToday, completedToday, sessionDone, hasGoalsToday, percent }) => (
            <li
              key={intent.id}
              className="group relative overflow-hidden rounded-card border border-black/[0.06] bg-surface p-4.5 shadow-sm transition-all duration-200 ease-spring hover:border-black/15 hover:shadow-md"
            >
              <div className="flex items-start gap-4">
                {/* Left Priority Bar Indicator */}
                <div
                  className="mt-1 h-10 w-1 shrink-0 rounded-full"
                  style={{ background: PRIORITY_TINT[intent.priority as Priority] }}
                  aria-hidden
                />

                {intent.period_days == null ? (
                  <button
                    type="button"
                    onClick={() => toggleComplete(intent)}
                    disabled={busy === intent.id}
                    aria-label={intent.completed_at ? "Mark not done" : "Mark done"}
                    aria-pressed={Boolean(intent.completed_at)}
                    className={CHECK_BUTTON}
                  >
                    {intent.completed_at ? (
                      <CheckCircle size={18} className="text-accent" />
                    ) : (
                      <Circle size={18} />
                    )}
                  </button>
                ) : today ? (
                  <button
                    type="button"
                    onClick={() => toggleSession(today)}
                    disabled={busy === intent.id}
                    aria-label={`${today.completed ? "Mark not done" : "Mark done"}: ${intent.title} today`}
                    aria-pressed={today.completed}
                    className={CHECK_BUTTON}
                  >
                    {today.completed ? <CheckCircle size={18} className="text-accent" /> : <Circle size={18} />}
                  </button>
                ) : view === "today" && (hasGoalsToday || sessionDone) ? (
                  <button type="button" onClick={() => void toggleTodaySession(intent)} disabled={busy === intent.id} aria-label={`${sessionDone ? "Mark not done" : "Mark done"}: ${intent.title} today`} aria-pressed={sessionDone} className={CHECK_BUTTON}>
                    {sessionDone ? <CheckCircle size={18} className="text-accent" /> : <Circle size={18} />}
                  </button>
                ) : (
                  <div className="mt-1 shrink-0 text-accent">
                    <Glyph kind={intent.kind} size={18} />
                  </div>
                )}

                {editing === intent.id ? (
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <input
                        autoFocus
                        value={editTitle}
                        onChange={(e) => setEditTitle(e.target.value)}
                        className="min-w-0 flex-1 rounded-lg border border-black/10 bg-background px-2.5 py-1.5 text-[14px] font-medium text-fg outline-none focus:border-accent"
                      />
                      <input
                        type="number"
                        min={15}
                        step={15}
                        value={editMinutes}
                        onChange={(e) => setEditMinutes(Number(e.target.value))}
                        aria-label="Minutes"
                        className="tabular w-20 rounded-lg border border-black/10 bg-background px-2.5 py-1.5 text-[13px] text-fg outline-none focus:border-accent"
                      />
                      <select
                        value={editPriority}
                        onChange={(e) => setEditPriority(Number(e.target.value) as Priority)}
                        aria-label="Priority"
                        className="rounded-lg border border-black/10 bg-background px-2 py-1.5 text-[13px] text-fg outline-none focus:border-accent"
                      >
                        {([1, 2, 3, 4] as Priority[]).map((p) => (
                          <option key={p} value={p}>
                            {PRIORITY_NAME[p]}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="mt-2 flex gap-2">
                      <button
                        type="button"
                        onClick={() => saveEdit(intent)}
                        disabled={busy === intent.id}
                        className="rounded-lg bg-accent px-3 py-1 text-[12.5px] font-semibold text-on-accent shadow-sm transition-[filter,box-shadow,transform] duration-150 hover:brightness-110 hover:shadow-md active:scale-95 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-wait disabled:opacity-50 motion-reduce:transition-none"
                      >
                        Save
                      </button>
                      <button
                        type="button"
                        onClick={() => setEditing(null)}
                        className="rounded-lg px-3 py-1 text-[12.5px] font-medium text-fg-muted transition-[background-color,color,transform] duration-150 hover:bg-sunk hover:text-fg active:scale-95 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent motion-reduce:transition-none"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                      <span
                        className={`truncate text-[15px] font-semibold ${intent.completed_at || (view === "today" && sessionDone) ? "text-fg-subtle line-through" : "text-fg"}`}
                      >
                        {intent.title}
                      </span>
                      <span className="tabular text-[12px] font-medium text-fg-muted">
                        {KIND_LABEL[intent.kind]} · {PRIORITY_NAME[intent.priority as Priority]}
                        {intent.period_days ? ` · every ${intent.period_days}d` : ""}
                      </span>
                    </div>

                    <div className="tabular mt-1.5 flex flex-wrap items-center gap-2 text-[12.5px] text-fg-muted">
                      {view === "today" ? (
                        <>
                          {(completedToday || sessionDone) && <span className="font-semibold text-accent">Done today</span>}
                          {today && <>
                            <span className="inline-flex items-center gap-1 font-medium text-fg"><Clock size={13} className="text-accent" />{new Date(today.start).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}–{new Date(todayBlocks[todayBlocks.length - 1]!.end).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span>
                            <span>· {formatDuration(todayBlocks.reduce((sum, block) => sum + minutesBetween(block.start, block.end), 0))} today</span>
                            <Link href={`/time?date=${todayKey}`} className="rounded px-1 font-semibold text-accent transition-colors hover:bg-sunk hover:underline active:bg-border focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">View on timeline</Link>
                          </>}
                          {!today && dueToday && !completedToday && <span className="font-semibold text-danger">Due today · no time scheduled</span>}
                          {!today && hasGoalsToday && !sessionDone && <span>Goals for today · no time scheduled</span>}
                        </>
                      ) : intent.completed_at ? (
                        <span className="font-medium text-accent">Done</span>
                      ) : today?.completed ? (
                        <span className="font-medium text-accent">Done today · {blocks.length} blocks placed</span>
                      ) : next ? (
                        <>
                          <span className="inline-flex items-center gap-1 font-medium text-fg">
                            <Clock size={13} className="text-accent" />
                            {new Date(next.start).toLocaleString([], {
                              weekday: "short",
                              hour: "numeric",
                              minute: "2-digit",
                            })}
                          </span>
                          <span>·</span>
                          <span>{blocks.length} {blocks.length === 1 ? "block" : "blocks"}</span>
                          <span>·</span>
                          <span>{formatDuration(scheduled)} of {formatDuration(demand)} in the plan</span>
                        </>
                      ) : (
                        <span className="font-semibold text-danger">Not placed</span>
                      )}
                    </div>

                    {view === "all" && !intent.completed_at && (
                      <>
                        {/* Progress Bar */}
                        <div className="mt-3 flex items-center gap-3">
                          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-sunk">
                            <div
                              className="h-full rounded-full bg-accent transition-[width] duration-300"
                              style={{ width: `${percent}%` }}
                            />
                          </div>
                          <span className="tabular text-[11px] font-medium text-fg-muted">{percent}% placed</span>
                        </div>

                        {unmet.map((item) => {
                          const conflict = plan ? unmetCalendarConflict(plan, intent, item) : null;
                          return (
                            <div key={item.occurrence} className="mt-2.5 rounded-lg bg-red-50 px-2.5 py-2 text-[12px] font-medium text-danger">
                              <div className="flex items-start gap-1.5">
                                <AlertCircle size={13} className="mt-0.5 shrink-0" />
                                <span>{conflict
                                  ? `${formatDuration(item.shortfall_minutes)} cannot fit because ${conflict.label} blocks ${new Date(`${conflict.day}T12:00:00`).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}.`
                                  : `${formatDuration(item.shortfall_minutes)} could not fit within this routine's allowed days and hours.`}</span>
                              </div>
                              {conflict && conflict.day >= localDateKey() && (
                                <button type="button" disabled={busy === intent.id} onClick={() => void skipBlockedSession(intent.id, conflict.day)} className="ml-[19px] mt-1 rounded px-1 font-semibold underline underline-offset-2 transition-colors hover:bg-red-100 active:bg-red-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-danger disabled:cursor-wait disabled:opacity-40 motion-reduce:transition-none">
                                  Skip this session
                                </button>
                              )}
                            </div>
                          );
                        })}
                      </>
                    )}
                    {intent.kind !== "meeting" && intent.kind !== "buffer" && (
                      <SubtaskChecklist intent={intent} day={localDateKey()} onChange={load} />
                    )}
                  </div>
                )}

                {view === "all" && editing !== intent.id && intent.kind !== "meeting" && (
                  <button
                    type="button"
                    onClick={() => startEdit(intent)}
                    aria-label={`Edit ${intent.title}`}
                    className={`${ICON_BUTTON} hover:bg-sunk hover:text-fg`}
                  >
                    <Pencil size={14} />
                  </button>
                )}

                {view === "all" && editing !== intent.id && (
                  <button
                    type="button"
                    onClick={() => remove(intent.id)}
                    disabled={busy === intent.id}
                    aria-label={`Remove ${intent.title}`}
                    className={`${ICON_BUTTON} hover:bg-red-50 hover:text-danger`}
                  >
                    <Trash2 size={15} />
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
        </motion.div>
        </AnimatePresence>
      </main>
    </Shell>
  );
}
