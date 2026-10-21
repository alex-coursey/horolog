"use client";

import { useState } from "react";
import { api, subtaskProgress, type Intent } from "@/app/lib/api";
import { Check, Circle, Plus, Trash2 } from "lucide-react";

export function SubtaskChecklist({ intent, day, onChange }: {
  intent: Intent;
  day: string;
  onChange: () => Promise<void>;
}) {
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const date = intent.period_days == null ? undefined : day;
  const progress = subtaskProgress(intent, day);

  async function act(operation: () => Promise<Intent>) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await operation();
      await onChange();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not update the checklist.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 border-t border-border pt-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-[12px] font-semibold text-fg">Goals {progress.total > 0 && `· ${progress.done} of ${progress.total} done`}</span>
        {intent.period_days != null && <span className="text-[11px] text-fg-muted">{day}</span>}
      </div>
      {progress.total > 0 && (
        <div className="mb-2 h-1.5 overflow-hidden rounded-full bg-sunk" role="progressbar" aria-label={`${intent.title} goals`} aria-valuenow={progress.done} aria-valuemin={0} aria-valuemax={progress.total}>
          <div className="h-full rounded-full bg-accent transition-[width]" style={{ width: `${progress.done / progress.total * 100}%` }} />
        </div>
      )}
      <ul className="space-y-1">
        {(intent.subtasks ?? []).filter((item) => !item.for_date || item.for_date === day).map((item) => {
          const done = date ? (item.completed_dates ?? []).includes(date) : Boolean(item.completed_at);
          return (
            <li key={item.id} className="flex items-center gap-1 rounded-lg px-1 py-0.5 transition-colors duration-150 hover:bg-sunk/70 motion-reduce:transition-none">
              <button type="button" disabled={busy} onClick={() => void act(() => api.setSubtaskComplete(intent.id, item.id, !done, date))} aria-label={`${done ? "Mark not done" : "Mark done"}: ${item.title}`} aria-pressed={done} className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-accent transition-[background-color,box-shadow,transform] duration-150 hover:bg-surface hover:shadow-sm active:scale-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-wait disabled:opacity-40 motion-reduce:transition-none">
                {done ? <Check size={16} /> : <Circle size={16} />}
              </button>
              <span className={`min-w-0 flex-1 text-[12.5px] ${done ? "text-fg-muted line-through" : "text-fg"}`}>{item.title}</span>
              <button type="button" disabled={busy} onClick={() => void act(() => api.removeSubtask(intent.id, item.id))} aria-label={`Remove goal: ${item.title}`} className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-fg-subtle transition-[background-color,color,transform] duration-150 hover:bg-red-50 hover:text-danger active:scale-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-danger disabled:cursor-wait disabled:opacity-40 motion-reduce:transition-none"><Trash2 size={13} /></button>
            </li>
          );
        })}
      </ul>
      <form className="mt-2 flex gap-2" onSubmit={(event) => {
        event.preventDefault();
        const value = title.trim();
        if (!value) return;
        void act(async () => {
          const result = await api.addSubtask(intent.id, value, date);
          setTitle("");
          return result;
        });
      }}>
        <input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={200} placeholder="Add a goal..." aria-label={`Add a goal to ${intent.title}`} className="min-w-0 flex-1 rounded-lg border border-border bg-background px-2.5 py-1.5 text-[12.5px] text-fg outline-none transition-[border-color,box-shadow] duration-150 hover:border-accent/50 focus:border-accent focus:ring-2 focus:ring-accent/20 motion-reduce:transition-none" />
        <button type="submit" disabled={!title.trim() || busy} aria-label={`Add goal to ${intent.title}`} className="flex w-8 items-center justify-center rounded-lg bg-sunk text-accent transition-[background-color,box-shadow,transform] duration-150 hover:bg-accent/10 hover:shadow-sm active:scale-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-40 motion-reduce:transition-none"><Plus size={16} /></button>
      </form>
      {error && <p role="alert" className="mt-2 text-[12px] text-danger">{error}</p>}
    </div>
  );
}
