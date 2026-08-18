<script lang="ts">
	import { createQuery } from '@tanstack/svelte-query';
	import { toStore } from 'svelte/store';
	import { jobsApi, type Job } from '$lib/api';
	import type { CalliopeEvent, EventConnectionState } from '$lib/events';
	import JobRow from './JobRow.svelte';
	import Button from './ui/Button.svelte';
	import Icon from './ui/Icon.svelte';

	interface Props {
		logs: CalliopeEvent[];
		projectId: number;
		/** Real SSE connection state from events.ts — drives the live pip. */
		connState: EventConnectionState;
		collapsed?: boolean;
		onToggle?: () => void;
	}

	let { logs, projectId, connState, collapsed = false, onToggle }: Props = $props();

	type Tone = 'info' | 'ok' | 'warn' | 'err' | 'work';

	type LogEntry = {
		key: string;
		ts: string;
		title: string;
		detail: string;
		tone: Tone;
		type: string;
	};

	function str(v: unknown): string {
		if (v == null) return '';
		if (typeof v === 'string') return v;
		if (typeof v === 'number' || typeof v === 'boolean') return String(v);
		return '';
	}

	function formatEvent(ev: CalliopeEvent, index: number): LogEntry | null {
		// Poll spam — progress lives in the jobProgress store, not the log
		if (ev.type === 'job.progress') return null;

		const d = ev.data ?? {};
		const msg = str(d.message).trim();
		const kind = str(d.kind);
		const jobId = d.job_id != null ? `#${d.job_id}` : '';
		const err = str(d.error).trim();
		const paths = Array.isArray(d.paths) ? d.paths.length : 0;
		const outputs = Array.isArray(d.outputs) ? d.outputs.length : 0;
		const time = ev.ts?.includes('T') ? ev.ts.slice(11, 19) : ev.ts?.slice(0, 8) || '';

		let title = ev.type;
		let detail = msg;
		let tone: Tone = 'info';

		switch (ev.type) {
			case 'agent.thinking':
				title = 'Agent';
				detail = msg || 'Working…';
				tone = 'work';
				break;
			case 'story.ready':
				title = 'Story ready';
				detail = msg || 'Storyline drafted';
				tone = 'ok';
				break;
			case 'job.created':
				title = 'Queued';
				detail = msg || [kind, jobId].filter(Boolean).join(' ');
				tone = 'info';
				break;
			case 'job.started':
				title = 'Running';
				detail = msg || [kind, jobId].filter(Boolean).join(' ');
				tone = 'work';
				break;
			case 'job.completed':
				title = 'Finished';
				detail =
					msg ||
					[kind, jobId, outputs ? `${outputs} file(s)` : ''].filter(Boolean).join(' · ');
				tone = 'ok';
				break;
			case 'job.failed':
				title = 'Failed';
				detail = err || msg || [kind, jobId].filter(Boolean).join(' ');
				tone = 'err';
				break;
			case 'asset.ready':
				title = 'Asset ready';
				detail = msg || (paths ? `${paths} file(s) saved` : 'Output saved');
				tone = 'ok';
				break;
			case 'job.deleted':
				title = 'Removed';
				detail = msg || jobId || 'Job deleted';
				tone = 'warn';
				break;
			default:
				title = ev.type.replace(/\./g, ' · ');
				detail = msg || err || kind;
				tone = 'info';
		}

		return {
			key: `${ev.ts}-${ev.type}-${index}`,
			ts: time,
			title,
			detail,
			tone,
			type: ev.type,
		};
	}

	const entries = $derived.by(() => {
		const raw = logs
			.slice(-120)
			.map((ev, i) => formatEvent(ev, i))
			.filter((e): e is LogEntry => e != null);

		// Collapse consecutive identical agent lines
		const collapsed: LogEntry[] = [];
		for (const e of raw) {
			const prev = collapsed[collapsed.length - 1];
			if (prev && prev.type === e.type && prev.detail === e.detail) {
				continue;
			}
			collapsed.push(e);
		}
		return collapsed.slice(-60).reverse();
	});

	// Same query key as QueueStage — the cache (and its refetch) is shared.
	const jobsQuery = createQuery(
		toStore(() => ({
			queryKey: ['jobs', projectId],
			queryFn: () => jobsApi.list(projectId),
			refetchInterval: 5000,
		})),
	);

	const allJobs = $derived(($jobsQuery.data ?? []) as Job[]);

	function activeWeight(job: Job): number {
		if (job.status === 'running') return 0;
		if (job.status === 'pending') return 1;
		return 2;
	}

	// Active jobs first, then most recent — capped so the log keeps room.
	const queueRows = $derived(
		[...allJobs].sort((a, b) => activeWeight(a) - activeWeight(b) || b.id - a.id).slice(0, 6),
	);

	const stats = $derived({
		running: allJobs.filter((j) => j.status === 'running').length,
		queued: allJobs.filter((j) => j.status === 'pending').length,
		done: allJobs.filter((j) => j.status === 'done').length,
		failed: allJobs.filter((j) => j.status === 'failed').length,
	});
	const busyCount = $derived(stats.running + stats.queued);

	const connected = $derived(connState === 'open');
</script>

<aside class="activity" class:collapsed aria-label="Activity">
	{#if collapsed}
		<button
			type="button"
			class="rail-toggle"
			onclick={onToggle}
			aria-expanded="false"
			title="Show activity"
		>
			<span class="pip rail-pip" class:live={connected} class:reconnecting={connState === 'reconnecting'} aria-hidden="true"></span>
			{#if busyCount > 0}
				<span class="rail-count" title="{busyCount} job(s) running or queued">{busyCount}</span>
			{/if}
			<span class="rail-label">Activity</span>
			<span class="rail-chevron" aria-hidden="true">‹</span>
		</button>
	{:else}
		<header class="head">
			<div class="head-title">
				<span class="pip" class:live={connected} class:reconnecting={connState === 'reconnecting'} aria-hidden="true"></span>
				<strong>Activity</strong>
				{#if connState === 'reconnecting'}
					<span class="conn-state warn">Reconnecting…</span>
				{:else if connState === 'connecting'}
					<span class="conn-state">Connecting…</span>
				{/if}
			</div>
			<Button variant="ghost" size="sm" onclick={onToggle} title="Hide activity">
				Hide <Icon name="chevron-right" size={13} />
			</Button>
		</header>

		<div class="body">
			<section class="queue" aria-label="Queue">
				<div class="section-head">
					<p class="eyebrow">Queue</p>
				</div>
				{#if queueRows.length === 0}
					<p class="queue-empty">Queue idle — renders and generations land here.</p>
				{:else}
					<div class="job-list">
						{#each queueRows as job (job.id)}
							<JobRow {job} />
						{/each}
					</div>
				{/if}
				<div class="stats-strip" aria-label="Queue stats">
					<span><span class="sd sd-running" aria-hidden="true"></span>{stats.running} running</span>
					<span><span class="sd sd-queued" aria-hidden="true"></span>{stats.queued} queued</span>
					<span><span class="sd sd-done" aria-hidden="true"></span>{stats.done} done</span>
					<span><span class="sd sd-failed" aria-hidden="true"></span>{stats.failed} failed</span>
				</div>
			</section>

			<section class="log-section" aria-label="Agent log">
				<p class="eyebrow">Agent log</p>
				{#if entries.length === 0}
					<div class="empty">
						<p>Quiet for now.</p>
						<p class="hint">Draft a story, generate assets, or regenerate the script — progress shows up here.</p>
					</div>
				{:else}
					<ul class="log">
						{#each entries as ev (ev.key)}
							<li class="entry tone-{ev.tone}">
								<span class="dot" aria-hidden="true"></span>
								<div class="meta">
									<div class="row">
										<span class="title">{ev.title}</span>
										<time class="ts" datetime={ev.ts}>{ev.ts}</time>
									</div>
									{#if ev.detail}
										<p class="detail">{ev.detail}</p>
									{/if}
								</div>
							</li>
						{/each}
					</ul>
				{/if}
			</section>
		</div>
	{/if}
</aside>

<style>
	.activity {
		--panel-w: 320px;
		--rail-w: 44px;
		width: var(--panel-w);
		flex-shrink: 0;
		display: flex;
		flex-direction: column;
		min-height: 0;
		height: 100%;
		border-left: 1px solid var(--border);
		background:
			linear-gradient(180deg, rgba(139, 92, 246, 0.06) 0%, transparent 120px),
			var(--bg-surface);
		overflow: hidden;
		transition: width 0.2s ease;
	}

	.activity.collapsed {
		width: var(--rail-w);
		background: var(--bg-surface);
	}

	.rail-toggle {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: flex-start;
		gap: 12px;
		width: 100%;
		height: 100%;
		margin: 0;
		padding: 16px 0;
		border: none;
		background: transparent;
		color: var(--text-secondary);
		cursor: pointer;
		overflow: hidden;
	}

	.rail-toggle:hover {
		background: rgba(139, 92, 246, 0.08);
		color: var(--text-primary);
	}

	.rail-toggle:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: -2px;
	}

	.rail-label {
		writing-mode: vertical-rl;
		transform: rotate(180deg);
		font-family: var(--font-display);
		font-size: 12px;
		font-weight: 600;
		letter-spacing: 0.12em;
		text-transform: uppercase;
	}

	.rail-chevron {
		font-size: 18px;
		line-height: 1;
		color: var(--text-muted);
	}

	.rail-count {
		display: flex;
		align-items: center;
		justify-content: center;
		min-width: 20px;
		height: 20px;
		padding: 0 5px;
		border-radius: 999px;
		background: var(--accent);
		color: #fff;
		font-family: var(--font-mono);
		font-size: 11px;
		font-weight: 700;
	}

	.pip {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--text-muted);
		flex-shrink: 0;
	}

	.pip.live {
		background: var(--accent);
		box-shadow: 0 0 0 4px var(--accent-glow);
		animation: live-pulse 1.6s ease-in-out infinite;
	}

	.pip.reconnecting {
		background: var(--warning);
		box-shadow: 0 0 0 4px rgba(245, 158, 11, 0.18);
	}

	@keyframes live-pulse {
		0%,
		100% {
			opacity: 1;
			transform: scale(1);
		}
		50% {
			opacity: 0.65;
			transform: scale(0.92);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.activity {
			transition: none;
		}
		.pip.live {
			animation: none;
		}
	}

	.head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		padding: 14px 14px 10px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.head-title {
		display: flex;
		align-items: center;
		gap: 10px;
		min-width: 0;
	}

	.head-title strong {
		font-family: var(--font-display);
		font-size: 15px;
		font-weight: 650;
		letter-spacing: -0.02em;
	}

	.conn-state {
		font-size: 11px;
		color: var(--text-muted);
		white-space: nowrap;
	}

	.conn-state.warn {
		color: var(--warning);
	}

	.body {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		padding: 12px 10px 16px;
		overflow: hidden;
	}

	.queue {
		flex-shrink: 0;
		display: flex;
		flex-direction: column;
		min-height: 0;
		margin-bottom: 14px;
	}

	.section-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		flex-shrink: 0;
	}

	.job-list {
		display: flex;
		flex-direction: column;
		gap: 6px;
		max-height: 216px;
		overflow-y: auto;
		padding-right: 4px;
	}

	.queue-empty {
		margin: 0 6px 4px;
		padding: 12px 14px;
		border: 1px dashed var(--border);
		border-radius: var(--radius-md);
		background: rgba(0, 0, 0, 0.2);
		font-size: 12px;
		line-height: 1.45;
		color: var(--text-muted);
	}

	.stats-strip {
		display: flex;
		flex-wrap: wrap;
		gap: 4px 12px;
		margin: 10px 6px 0;
		padding-top: 10px;
		border-top: 1px solid var(--border);
		font-family: var(--font-mono);
		font-size: 10px;
		color: var(--text-secondary);
		flex-shrink: 0;
	}

	.stats-strip > span {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		white-space: nowrap;
	}

	.sd {
		width: 6px;
		height: 6px;
		border-radius: 50%;
	}

	.sd-running {
		background: var(--info);
	}
	.sd-queued {
		background: var(--text-muted);
	}
	.sd-done {
		background: var(--success);
	}
	.sd-failed {
		background: var(--error);
	}

	.log-section {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}

	.eyebrow {
		margin: 0 6px 10px;
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-muted);
		flex-shrink: 0;
	}

	.empty {
		margin: 8px 6px;
		padding: 16px;
		border: 1px dashed var(--border);
		border-radius: var(--radius-md);
		background: rgba(0, 0, 0, 0.2);
	}

	.empty p {
		margin: 0;
		font-size: 13px;
		color: var(--text-secondary);
	}

	.empty .hint {
		margin-top: 8px;
		font-size: 12px;
		color: var(--text-muted);
		line-height: 1.45;
	}

	.log {
		list-style: none;
		margin: 0;
		padding: 0 4px 8px 0;
		overflow-y: auto;
		overflow-x: hidden;
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.entry {
		display: grid;
		grid-template-columns: 14px 1fr;
		gap: 8px;
		padding: 8px 8px 8px 4px;
		border-radius: var(--radius-sm);
	}

	.entry:hover {
		background: rgba(255, 255, 255, 0.03);
	}

	.dot {
		width: 8px;
		height: 8px;
		margin-top: 5px;
		border-radius: 50%;
		background: var(--text-muted);
		justify-self: center;
	}

	.tone-ok .dot {
		background: var(--success);
	}
	.tone-err .dot {
		background: var(--error);
	}
	.tone-warn .dot {
		background: var(--warning);
	}
	.tone-work .dot {
		background: var(--accent);
	}
	.tone-info .dot {
		background: var(--info);
	}

	.meta {
		min-width: 0;
	}

	.row {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 8px;
	}

	.title {
		font-size: 12px;
		font-weight: 650;
		color: var(--text-primary);
	}

	.ts {
		font-family: var(--font-mono);
		font-size: 10px;
		color: var(--text-muted);
		flex-shrink: 0;
	}

	.detail {
		margin: 3px 0 0;
		font-size: 12px;
		line-height: 1.4;
		color: var(--text-secondary);
		word-break: break-word;
	}

	.tone-err .detail {
		color: #fca5a5;
	}
</style>
