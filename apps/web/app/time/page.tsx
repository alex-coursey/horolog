"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Grid } from "@/app/components/Grid";
import { Glyph, KIND_LABEL } from "@/app/components/Glyph";
import { Shell } from "@/app/components/Shell";
import { SubtaskChecklist } from "@/app/components/SubtaskChecklist";
import { Skeleton } from "@/app/components/Skeleton";
import { api, formatDuration, minutesBetween, subtaskProgress, toggleBlockCompletion, type Block, type Intent, type Plan } from "@/app/lib/api";
import { ArrowRight } from "lucide-react";

function dayKey(iso: string): string {
  return iso.slice(0, 10);
}

function localDayKey(date: Date): string {
  return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, "0"), String(date.getDate()).padStart(2, "0")].join("-");
}

function dateFromKey(key: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(key)) return null;
  const year = Number(key.slice(0, 4));
  const month = Number(key.slice(5, 7));
  const day = Number(key.slice(8, 10));
  const date = new Date(year, month - 1, day, 12);
  return localDayKey(date) === key ? date : null;
}

/** Live "today" view: a single-day timeline with a moving now-line, plus
 *  what's in progress and what's next. Complements Planner (week grid) and
 *  Analytics (aggregate stats) rather than duplicating either. */
export default function TimePage() {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [intents, setIntents] = useState<Intent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [completing, setCompleting] = useState<string | null>(null);
  const [selectedDayKey, setSelectedDayKey] = useState<string | null>(null);
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("date");
    setSelectedDayKey(requested && dateFromKey(requested) ? requested : null);
  }, []);

  const load = useCallback(async () => {
    try {
      const [nextPlan, nextIntents] = await Promise.all([api.plan(), api.intents()]);
      setPlan(nextPlan);
      setIntents(nextIntents);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not load the schedule.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  const todayKey = localDayKey(now);
  const viewedDayKey = selectedDayKey ?? todayKey;
  const viewedDay = dateFromKey(viewedDayKey) ?? now;
  const viewingToday = viewedDayKey === todayKey;

  const todaysBlocks = useMemo(
    () => (plan ? plan.blocks.filter((b) => dayKey(b.start) === viewedDayKey) : []),
    [plan, viewedDayKey],
  );

  // Blocks come back with the server's local UTC offset (e.g. `-05:00`), not
  // necessarily `Z` — comparing those strings against now.toISOString()
  // (always `Z`) lexicographically does not agree with chronological order.
  // Parse both sides to epoch ms instead.
  const nowMs = now.getTime();
  const current = todaysBlocks.find((b) => Date.parse(b.start) <= nowMs && nowMs < Date.parse(b.end));
  const next = todaysBlocks
    .filter((b) => !b.completed && Date.parse(b.start) > nowMs)
    .sort((a, b) => Date.parse(a.start) - Date.parse(b.start))[0];
  const selectedBlock = todaysBlocks.find((block) => `${block.intent_id}:${block.occurrence}:${block.chunk}` === selected);
  const selectedIntent = intents.find((intent) => intent.id === selectedBlock?.intent_id);

  async function toggleComplete(block: Block) {
    const key = `${block.intent_id}:${block.occurrence}:${block.chunk}`;
    setCompleting(key);
    try {
      await toggleBlockCompletion(block);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not update completion.");
    } finally {
      setCompleting(null);
    }
  }

  return (
    <Shell onPlanChange={load}>
      <main className="mx-auto max-w-[1440px] px-6 py-8">
        <header className="mb-7 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-[28px] font-bold text-fg">Time</h1>
            <p className="mt-1 text-[13.5px] text-fg-muted">
              {viewedDay.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric", year: "numeric" })}
            </p>
          </div>
          {viewingToday ? (
            <div className="tabular text-[32px] font-semibold leading-none text-fg">
              {now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}
            </div>
          ) : (
            <a href="/time" className="rounded-lg border border-border bg-surface px-3 py-1.5 text-[13px] font-medium text-fg transition-colors hover:bg-sunk">
              Today
            </a>
          )}
        </header>

        {error && (
          <div className="mb-6 rounded-card border border-red-200 bg-red-50/70 p-4 text-[13.5px] text-danger shadow-xs">
            {error}
          </div>
        )}

        {viewingToday && <div className="mb-7 grid gap-4 sm:grid-cols-2">
          <StatusCard
            label="Right now"
            block={current}
            intent={intents.find((intent) => intent.id === current?.intent_id)}
            empty="Nothing scheduled — this time is open."
            loading={!plan}
          />
          <StatusCard
            label="Up next"
            block={next}
            intent={intents.find((intent) => intent.id === next?.intent_id)}
            empty="Nothing else scheduled for today."
            untilNow={now}
            loading={!plan}
          />
        </div>}

        <Grid
          days={[viewedDay]}
          blocks={plan?.blocks ?? []}
          busy={plan?.busy ?? []}
          intents={intents}
          selected={selected}
          onSelect={setSelected}
          onToggleComplete={toggleComplete}
          completing={completing}
          focusNow={viewingToday}
        />
        {selectedIntent && selectedBlock && selectedIntent.kind !== "meeting" && selectedIntent.kind !== "buffer" && (
          <section className="mt-4 rounded-card border border-border bg-surface p-4 shadow-sm" aria-label={`${selectedIntent.title} goals`}>
            <h2 className="text-[15px] font-semibold text-fg">{selectedIntent.title}</h2>
            <SubtaskChecklist intent={selectedIntent} day={dayKey(selectedBlock.start)} onChange={load} />
          </section>
        )}
      </main>
    </Shell>
  );
}

function StatusCard({
  label,
  block,
  intent,
  empty,
  untilNow,
  loading = false,
}: {
  label: string;
  block: Block | undefined;
  intent?: Intent;
  empty: string;
  untilNow?: Date;
  loading?: boolean;
}) {
  return (
    <div className="rounded-card border border-black/[0.08] bg-surface p-5 shadow-sm">
      <div className="text-[11px] font-semibold uppercase tracking-wider text-fg-muted">{label}</div>
      {loading ? (
        <div aria-hidden className="mt-2 flex items-center gap-2.5">
          <Skeleton className="h-8 w-8 shrink-0 rounded-full" />
          <div className="min-w-0 flex-1 space-y-1.5">
            <Skeleton className="h-4 w-2/5" />
            <Skeleton className="h-3 w-3/5" />
          </div>
        </div>
      ) : block ? (
        <div className="mt-2 flex items-center gap-2.5">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-sunk text-fg">
            <Glyph kind={block.kind} size={15} />
          </span>
          <div className="min-w-0">
            <div className={`truncate text-[15px] font-semibold text-fg ${block.completed ? "line-through opacity-60" : ""}`}>{block.title}</div>
            <div className="tabular mt-0.5 flex items-center gap-1.5 text-[12px] text-fg-muted">
              {block.completed && <><span className="font-semibold text-accent">Done</span><span>·</span></>}
              <span>{KIND_LABEL[block.kind]}</span>
              {intent && subtaskProgress(intent, dayKey(block.start)).total > 0 && (
                <><span>·</span><span>{subtaskProgress(intent, dayKey(block.start)).done}/{subtaskProgress(intent, dayKey(block.start)).total} goals</span></>
              )}
              <span>·</span>
              <span>
                {new Date(block.start).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}
                <ArrowRight size={10} className="mx-1 inline align-middle" />
                {new Date(block.end).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}
              </span>
              {untilNow && (
                <>
                  <span>·</span>
                  <span>in {formatDuration(minutesBetween(untilNow.toISOString(), block.start))}</span>
                </>
              )}
            </div>
          </div>
        </div>
      ) : (
        <p className="mt-2.5 text-[13.5px] text-fg-muted">{empty}</p>
      )}
    </div>
  );
}
