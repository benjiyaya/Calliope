<script lang="ts">
	import { useQueryClient } from '@tanstack/svelte-query';
	import { jobsApi, type Job } from '$lib/api';
	import { progressFor } from '$lib/jobProgress';
	import { toast } from '$lib/toast';
	import Button from './ui/Button.svelte';
	import Icon from './ui/Icon.svelte';
	import type { IconName } from './ui/icons';
	import ProgressBar from './ui/ProgressBar.svelte';
	import StatusChip from './ui/StatusChip.svelte';

	interface Props {
		job: Job;
		/** Optional override for the leading label (defaults to "Kind #id"). */
		label?: string;
	}

	let { job, label }: Props = $props();

	const client = useQueryClient();
	let busy = $state(false);

	const kindIcon: IconName = $derived(
		job.kind === 'video'
			? 'video'
			: job.kind === 'image'
				? 'image'
				: job.kind === 'export'
					? 'film'
					: 'queue',
	);
	const kindLabel = $derived(
		job.kind === 'export'
			? 'Export film'
			: job.kind.charAt(0).toUpperCase() + job.kind.slice(1),
	);
	const title = $derived(label ?? `${kindLabel} #${job.id}`);
	const active = $derived(job.status === 'pending' || job.status === 'running');
	const entry = $derived(progressFor(job.id));

	async function refresh() {
		await client.invalidateQueries({ queryKey: ['jobs'] });
		await client.invalidateQueries({ queryKey: ['playground-jobs'] });
	}

	async function cancel() {
		busy = true;
		try {
			await jobsApi.cancel(job.id);
			await refresh();
			toast.info(`Cancelled #${job.id}`);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Cancel failed');
		} finally {
			busy = false;
		}
	}

	async function retry() {
		busy = true;
		try {
			await jobsApi.retry(job.id);
			await refresh();
			toast.success(`Re-queued #${job.id}`);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Retry failed');
		} finally {
			busy = false;
		}
	}
</script>

<div class="job-row">
	<span class="kind" aria-hidden="true"><Icon name={kindIcon} size={14} /></span>
	<div class="main">
		<div class="top">
			<span class="label" {title}>{title}</span>
			<StatusChip status={job.status} />
		</div>
		{#if active}
			<div class="bar">
				<ProgressBar
					size="sm"
					value={entry?.progress ?? 0}
					indeterminate={job.status === 'running' && entry == null}
					label={entry?.message}
				/>
			</div>
		{:else if job.status === 'failed' && job.error}
			<p class="err" title={job.error}>{job.error}</p>
		{/if}
	</div>
	<div class="actions">
		{#if active}
			<Button size="sm" variant="ghost" loading={busy} title="Cancel job" onclick={cancel}>
				<Icon name="close" size={13} />
			</Button>
		{:else if job.status === 'failed'}
			<Button size="sm" variant="ghost" loading={busy} title="Retry job" onclick={retry}>
				<Icon name="retry" size={13} />
			</Button>
		{/if}
	</div>
</div>

<style>
	.job-row {
		display: grid;
		grid-template-columns: 20px 1fr auto;
		gap: 8px;
		align-items: start;
		padding: 8px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--bg-elevated);
	}
	.kind {
		display: flex;
		align-items: center;
		justify-content: center;
		height: 20px;
		color: var(--text-muted);
	}
	.main {
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.top {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		min-height: 20px;
	}
	.label {
		font-size: 12px;
		font-weight: 600;
		color: var(--text-primary);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.bar {
		padding-bottom: 2px;
	}
	.err {
		margin: 0;
		font-size: 11px;
		line-height: 1.4;
		color: var(--error);
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
	.actions {
		display: flex;
		align-items: center;
	}
</style>
