<script lang="ts">
	/**
	 * JobInputsDrawer — per-scene job history + read-only view of what a job
	 * sent to ComfyUI. Reads job.payload (persisted in jobs.payload_json,
	 * served via _job_public): the resolved prompt plus per-node input values.
	 * Ref assignments are labeled by matching node ids against the workflow's
	 * input_schema roles. "Copy settings to form" loads payload.input_values
	 * back into the live form via the caller-provided callback.
	 */
	import type { Job, Workflow } from '$lib/api';
	import Button from '$lib/components/ui/Button.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';

	interface Props {
		open?: boolean;
		/** Latest job for the scene — shown by default. */
		job?: Job | null;
		/** Full job list for the scene, newest first (drives history). */
		jobs?: Job[];
		workflow?: Workflow | null;
		/** Load a job's input_values back into the video form. */
		onCopySettings?: (values: Record<string, string | number>) => void;
		onclose?: () => void;
	}

	let {
		open = $bindable(false),
		job = null,
		jobs = [],
		workflow = null,
		onCopySettings,
		onclose,
	}: Props = $props();

	let selectedJobId = $state<number | null>(null);
	let copied = $state(false);

	const historyJobs = $derived.by(() => {
		const list = (jobs ?? []).filter((j) => j.kind === 'video' && j.payload != null);
		return [...list].sort((a, b) => b.id - a.id).slice(0, 20);
	});

	const activeJob = $derived(
		historyJobs.find((j) => j.id === selectedJobId) ?? job ?? historyJobs[0] ?? null,
	);

	$effect(() => {
		if (open && selectedJobId == null && activeJob) {
			selectedJobId = activeJob.id;
		}
	});

	const prompt = $derived(
		typeof activeJob?.payload?.prompt === 'string' && activeJob.payload.prompt.trim()
			? (activeJob.payload.prompt as string)
			: null,
	);

	interface ValueRow {
		nodeId: string;
		label: string;
		role: string;
		value: string;
	}

	const valueRows = $derived.by((): ValueRow[] => {
		const raw = activeJob?.payload?.input_values;
		if (!raw || typeof raw !== 'object') return [];
		const schema = workflow?.input_schema ?? [];
		return Object.entries(raw as Record<string, unknown>)
			.filter(([, v]) => v !== null && v !== undefined && String(v) !== '')
			.map(([nodeId, v]) => {
				const inp = schema.find((i) => i.nodeId === nodeId);
				return {
					nodeId,
					label: inp?.label ?? nodeId,
					role: inp?.role ?? '',
					value: String(v),
				};
			});
	});

	const refRows = $derived(valueRows.filter((r) => ['character', 'location', 'image', 'video', 'audio'].includes(r.role)));
	const otherRows = $derived(valueRows.filter((r) => !['character', 'location', 'image', 'video', 'audio'].includes(r.role)));

	function roleLabel(role: string): string {
		switch (role) {
			case 'character':
				return 'Character ref';
			case 'location':
				return 'Location ref';
			case 'image':
				return 'Ref image';
			case 'video':
				return 'Video input';
			case 'audio':
				return 'Audio input';
			default:
				return role;
		}
	}

	function baseName(path: string): string {
		const parts = path.replace(/\\/g, '/').split('/');
		return parts[parts.length - 1] || path;
	}

	function copySettings() {
		if (!onCopySettings || !activeJob) return;
		const raw = activeJob.payload?.input_values;
		if (!raw || typeof raw !== 'object') return;
		const values: Record<string, string | number> = {};
		for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
			if (typeof v === 'string' || typeof v === 'number') values[k] = v;
		}
		onCopySettings(values);
		open = false;
	}

	function statusChipClass(status: string): string {
		if (status === 'done') return 'chip-done';
		if (status === 'failed') return 'chip-failed';
		if (status === 'running') return 'chip-running';
		return 'chip-pending';
	}

	function shortTime(iso: string): string {
		const d = new Date(iso);
		if (Number.isNaN(d.getTime())) return '';
		return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}

	async function copyPrompt() {
		if (!prompt) return;
		try {
			await navigator.clipboard.writeText(prompt);
			copied = true;
			setTimeout(() => (copied = false), 1600);
		} catch {
			/* clipboard unavailable — no-op */
		}
	}
</script>

<Modal bind:open {onclose} title="Scene render history" size="lg">
	{#if !activeJob}
		<p class="muted">No render jobs recorded for this scene yet.</p>
	{:else}
		{#if historyJobs.length > 1}
			<div class="history-strip" role="tablist" aria-label="Render history">
				{#each historyJobs as j (j.id)}
					<button
						type="button"
						class="history-chip {j.id === activeJob.id ? 'active' : ''}"
						role="tab"
						aria-selected={j.id === activeJob.id}
						onclick={() => (selectedJobId = j.id)}
					>
						<span class="chip-dot {statusChipClass(j.status)}"></span>
						<span>#{j.id}</span>
						<span class="chip-time">{j.completed_at ? shortTime(j.completed_at) : j.status}</span>
					</button>
				{/each}
			</div>
		{/if}

		<div class="job-head">
			<span class="job-meta">Job #{activeJob.id}</span>
			<span class="job-meta">·</span>
			<span class="job-meta">{activeJob.status}</span>
			{#if activeJob.completed_at}
				<span class="job-meta">·</span>
				<span class="job-meta">{new Date(activeJob.completed_at).toLocaleString()}</span>
			{/if}
			<span class="job-head-actions">
				{#if onCopySettings && activeJob.payload?.input_values}
					<Button size="sm" variant="secondary" onclick={copySettings}>
						<Icon name="upload" size={14} /> Copy settings to form
					</Button>
				{/if}
			</span>
		</div>

		{#if prompt}
			<section class="block">
				<div class="block-head">
					<h3 class="block-title">Prompt</h3>
					<Button size="sm" variant="ghost" onclick={copyPrompt}>
						<Icon name={copied ? 'check' : 'link'} size={14} />
						{copied ? 'Copied' : 'Copy'}
					</Button>
				</div>
				{#if workflow?.prompt_profile === 'minimax_h3_ref'}
					<p class="block-hint">MiniMax H3 six-section rewrite — this is the exact text queued on the (Input:prompt) node.</p>
				{/if}
				<pre class="prompt-pre">{prompt}</pre>
			</section>
		{:else}
			<p class="muted">No prompt recorded on this job.</p>
		{/if}

		{#if refRows.length > 0}
			<section class="block">
				<h3 class="block-title">References</h3>
				<ul class="ref-list">
					{#each refRows as row (row.nodeId)}
						<li class="ref-item">
							<span class="ref-role">{roleLabel(row.role)}</span>
							<span class="ref-name" title={row.value}>{baseName(row.value)}</span>
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		{#if otherRows.length > 0}
			<section class="block">
				<h3 class="block-title">Other inputs</h3>
				<ul class="ref-list">
					{#each otherRows as row (row.nodeId)}
						<li class="ref-item">
							<span class="ref-role">{row.label}</span>
							<span class="ref-name" title={row.value}>{row.value}</span>
						</li>
					{/each}
				</ul>
			</section>
		{/if}
	{/if}
</Modal>

<style>
	.muted {
		margin: 0;
		font-size: 13px;
		color: var(--text-secondary);
	}

	.job-head {
		display: flex;
		align-items: center;
		gap: 6px;
		margin: 0 0 14px;
		font-family: var(--font-mono);
		font-size: 12px;
		color: var(--text-muted);
	}

	.job-head-actions {
		margin-left: auto;
	}

	.history-strip {
		display: flex;
		gap: 6px;
		overflow-x: auto;
		padding-bottom: 6px;
		margin: 0 0 12px;
	}

	.history-chip {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		flex-shrink: 0;
		padding: 4px 10px;
		font: inherit;
		font-size: 12px;
		font-family: var(--font-mono);
		color: var(--text-secondary);
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: 9999px;
		cursor: pointer;
	}

	.history-chip:hover {
		color: var(--text-primary);
		border-color: var(--text-muted);
	}

	.history-chip.active {
		color: var(--text-primary);
		border-color: var(--accent);
		box-shadow: 0 0 0 1px var(--accent);
	}

	.chip-dot {
		width: 7px;
		height: 7px;
		border-radius: 9999px;
	}

	.chip-done {
		background: var(--success);
	}

	.chip-failed {
		background: var(--error);
	}

	.chip-running {
		background: var(--warning);
	}

	.chip-pending {
		background: var(--text-muted);
	}

	.chip-time {
		color: var(--text-muted);
	}

	.job-meta {
		white-space: nowrap;
	}

	.block {
		margin: 0 0 16px;
	}

	.block-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin: 0 0 6px;
	}

	.block-title {
		margin: 0;
		font-size: 13px;
		font-weight: 650;
		color: var(--text-primary);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.block-hint {
		margin: 0 0 6px;
		font-size: 12px;
		color: var(--text-muted);
	}

	.prompt-pre {
		margin: 0;
		padding: 12px;
		max-height: 320px;
		overflow-y: auto;
		font-family: var(--font-mono);
		font-size: 12px;
		line-height: 1.55;
		white-space: pre-wrap;
		word-break: break-word;
		color: var(--text-primary);
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
	}

	.ref-list {
		margin: 0;
		padding: 0;
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.ref-item {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 6px 10px;
		font-size: 13px;
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
	}

	.ref-role {
		flex-shrink: 0;
		min-width: 110px;
		font-size: 12px;
		font-weight: 600;
		color: var(--text-secondary);
	}

	.ref-name {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: var(--font-mono);
		font-size: 12px;
		color: var(--text-primary);
	}
</style>
