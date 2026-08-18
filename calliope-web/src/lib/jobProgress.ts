import { SvelteMap } from 'svelte/reactivity';
import type { CalliopeEvent } from './events';

/**
 * Live per-job render progress, fed by the `/api/events` SSE stream.
 *
 * Backend reality (see calliope-backend/src/calliope/queue/worker.py):
 * - `job.created`   { job_id, kind, ... }
 * - `job.started`   { job_id, kind, message }          ← message = job label
 * - `job.progress`  { prompt_id, message }             ← NO job_id, NO percent
 * - `job.completed` { job_id, outputs, message }
 * - `job.failed`    { job_id, error, message }
 * - `job.deleted`   { job_id }
 *
 * Because progress ticks carry neither a job id nor a percent, the store:
 * 1. Attributes a tick to the currently running job (tracked from job.started;
 *    explicit `job_id` wins if a future backend sends one).
 * 2. Honors a numeric payload field (`progress` / `percent` / `value`, 0–1 or
 *    0–100) when present, otherwise synthesizes an asymptotic value that
 *    approaches TICK_CAP but never reaches it — only job.completed sets 100.
 *
 * Entries for completed jobs are kept briefly (progress pinned to 100) and for
 * failed jobs a bit longer (last known progress + error), then removed, so the
 * UI can flash the final state before the jobs query becomes the source of
 * truth again. Consumers should only render the live bar while the job's
 * queried status is still pending/running.
 */

export type JobProgressEntry = {
	/** 0–100. Reaches 100 only via job.completed. */
	progress: number;
	/** Latest human-readable status line from the worker. */
	message?: string;
	/** True once the job reached a terminal state (done/failed). */
	final: boolean;
	/** Terminal status when final: 'done' | 'failed'. */
	status?: 'done' | 'failed';
	/** Last update, ms epoch. */
	updatedAt: number;
};

/** Synthesized ticks never exceed this; only job.completed reports 100. */
const TICK_CAP = 95;
/** How long terminal entries linger so the UI can show the final state. */
const DONE_KEEP_MS = 6_000;
const FAILED_KEEP_MS = 10_000;

function num(v: unknown): number | null {
	if (typeof v === 'number' && Number.isFinite(v)) return v;
	if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v);
	return null;
}

function str(v: unknown): string {
	return typeof v === 'string' ? v : '';
}

/** Numeric progress from a payload, normalized to 0–100 (capped below 100). */
function progressValue(d: Record<string, unknown>): number | null {
	const raw = num(d.progress ?? d.percent ?? d.value);
	if (raw == null) return null;
	const pct = raw <= 1 ? raw * 100 : raw;
	return Math.min(TICK_CAP, Math.max(0, pct));
}

class JobProgressStore {
	private map = new SvelteMap<number, JobProgressEntry>();
	/** Jobs seen via job.started and not yet terminal — tick attribution. */
	private running = new Set<number>();
	private lastStarted: number | null = null;
	private cleanupTimers = new Map<number, ReturnType<typeof setTimeout>>();

	/** Live entry for a job id (tracked — safe inside $derived / markup). */
	get(jobId: number | null | undefined): JobProgressEntry | undefined {
		if (jobId == null) return undefined;
		return this.map.get(jobId);
	}

	handleEvent(ev: CalliopeEvent): void {
		const d = ev.data ?? {};
		switch (ev.type) {
			case 'job.created': {
				const id = num(d.job_id);
				if (id == null) return;
				this.cancelCleanup(id);
				this.map.set(id, {
					progress: 0,
					message: str(d.message) || 'Queued',
					final: false,
					updatedAt: Date.now(),
				});
				break;
			}
			case 'job.started': {
				const id = num(d.job_id);
				if (id == null) return;
				this.cancelCleanup(id);
				this.running.add(id);
				this.lastStarted = id;
				const prev = this.map.get(id);
				this.map.set(id, {
					progress: prev?.progress ?? 0,
					message: str(d.message) || 'Running',
					final: false,
					updatedAt: Date.now(),
				});
				break;
			}
			case 'job.progress': {
				const id = this.resolveJobId(d);
				if (id == null) return;
				const prev = this.map.get(id);
				const progress = progressValue(d) ?? this.synthesize(prev?.progress ?? 0);
				this.map.set(id, {
					progress,
					message: str(d.message) || prev?.message,
					final: false,
					updatedAt: Date.now(),
				});
				break;
			}
			case 'job.completed':
			case 'job.failed': {
				const id = num(d.job_id);
				if (id == null) return;
				this.running.delete(id);
				if (this.lastStarted === id) this.lastStarted = null;
				const failed = ev.type === 'job.failed';
				const prev = this.map.get(id);
				this.map.set(id, {
					progress: failed ? (prev?.progress ?? 0) : 100,
					message: str(d.error) || str(d.message) || prev?.message,
					final: true,
					status: failed ? 'failed' : 'done',
					updatedAt: Date.now(),
				});
				this.scheduleCleanup(id, failed ? FAILED_KEEP_MS : DONE_KEEP_MS);
				break;
			}
			case 'job.deleted': {
				const id = num(d.job_id);
				if (id == null) return;
				this.running.delete(id);
				if (this.lastStarted === id) this.lastStarted = null;
				this.cancelCleanup(id);
				this.map.delete(id);
				break;
			}
		}
	}

	/**
	 * Today's job.progress payload is {prompt_id, message} with no job id.
	 * Attribute the tick to the only running job, else the most recently
	 * started one. Explicit job_id wins when a backend sends it.
	 */
	private resolveJobId(d: Record<string, unknown>): number | null {
		const explicit = num(d.job_id);
		if (explicit != null) return explicit;
		if (this.running.size === 1) return [...this.running][0];
		if (this.lastStarted != null && this.running.has(this.lastStarted)) return this.lastStarted;
		return null;
	}

	/** Asymptotic approach to TICK_CAP — feels live, never falsely completes. */
	private synthesize(prev: number): number {
		return prev + (TICK_CAP - prev) * 0.08;
	}

	private scheduleCleanup(jobId: number, delayMs: number): void {
		this.cancelCleanup(jobId);
		this.cleanupTimers.set(
			jobId,
			setTimeout(() => {
				this.cleanupTimers.delete(jobId);
				this.map.delete(jobId);
			}, delayMs),
		);
	}

	private cancelCleanup(jobId: number): void {
		const timer = this.cleanupTimers.get(jobId);
		if (timer) {
			clearTimeout(timer);
			this.cleanupTimers.delete(jobId);
		}
	}
}

export const jobProgress = new JobProgressStore();

/** Feed one SSE event into the store. Safe to call for every event type. */
export function handleJobEvent(ev: CalliopeEvent): void {
	jobProgress.handleEvent(ev);
}

/** Read the live entry for a job id (reactive via SvelteMap). */
export function progressFor(jobId: number | null | undefined): JobProgressEntry | undefined {
	return jobProgress.get(jobId);
}
