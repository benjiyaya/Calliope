<script lang="ts">
	import type { AgentPlan } from '$lib/api';
	import { agentColor, agentDisplayName } from './agentPalette';
	import Icon from '$lib/components/ui/Icon.svelte';

	interface Props {
		plan: AgentPlan;
	}

	let { plan }: Props = $props();

	const tasks = $derived(plan.tasks ?? []);
	const doneCount = $derived(tasks.filter((t) => t.status === 'done').length);
	const running = $derived(tasks.some((t) => t.status === 'running'));

	function statusIcon(status: string): 'check' | 'close' | 'clock' | 'stop' {
		if (status === 'done') return 'check';
		if (status === 'failed') return 'close';
		if (status === 'running') return 'clock';
		return 'stop';
	}

	function statusLabel(status: string): string {
		if (status === 'running') return 'Running';
		if (status === 'done') return 'Done';
		if (status === 'failed') return 'Failed';
		return 'Queued';
	}
</script>

<div class="plan-panel">
	<header class="head">
		<span class="icon-wrap" class:spinning={running}>
			<Icon name="sparkle" size={13} />
		</span>
		<span class="title">Planner</span>
		{#if plan.note}
			<span class="note">{plan.note}</span>
		{/if}
		<span class="count mono">{doneCount}/{tasks.length}</span>
	</header>

	<ol class="tasks">
		{#each tasks as task, i (i)}
			{@const color = agentColor(`${task.role}-agent`)}
			<li class="task" class:active={task.status === 'running'}>
				<span class="status" class:done={task.status === 'done'} class:failed={task.status === 'failed'} class:running={task.status === 'running'}>
					{#if task.status === 'running'}
						<span class="spin-dot" style:background={color}></span>
					{:else}
						<Icon name={statusIcon(task.status)} size={12} />
					{/if}
				</span>
				<span class="role" style:color={color} style:--role-color={color}>
					{agentDisplayName(`${task.role}-agent`)}
				</span>
				<span class="goal" class:done={task.status === 'done'}>{task.goal}</span>
				<span class="state mono">{statusLabel(task.status)}</span>
			</li>
		{/each}
	</ol>
</div>

<style>
	.plan-panel {
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		background: var(--bg-surface);
		padding: 8px 10px;
		flex-shrink: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.head {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.icon-wrap {
		display: inline-flex;
		color: var(--text-muted);
	}
	.icon-wrap.spinning {
		animation: spin 1.5s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
	.title {
		font-size: 12px;
		font-weight: 700;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--text-secondary);
	}
	.note {
		font-size: 12px;
		color: var(--text-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		flex: 1;
		min-width: 0;
	}
	.count {
		font-size: 11px;
		color: var(--text-muted);
		margin-left: auto;
		flex-shrink: 0;
	}
	.tasks {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.task {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 5px 8px;
		border-radius: var(--radius-sm);
		border: 1px solid transparent;
	}
	.task.active {
		background: var(--bg-elevated);
		border-color: var(--border);
	}
	.status {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		height: 18px;
		flex-shrink: 0;
		color: var(--text-muted);
	}
	.status.done {
		color: var(--success);
	}
	.status.failed {
		color: var(--error);
	}
	.status.running {
		color: var(--warning);
	}
	.spin-dot {
		width: 10px;
		height: 10px;
		border-radius: 9999px;
		animation: pulse 1.1s ease-in-out infinite;
	}
	@keyframes pulse {
		0%,
		100% {
			opacity: 0.35;
		}
		50% {
			opacity: 1;
		}
	}
	.role {
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		flex-shrink: 0;
		border: 1px solid color-mix(in srgb, var(--role-color) 45%, transparent);
		background: color-mix(in srgb, var(--role-color) 14%, transparent);
		padding: 2px 8px;
		border-radius: 999px;
	}
	.goal {
		font-size: 12.5px;
		color: var(--text-primary);
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.goal.done {
		color: var(--text-secondary);
	}
	.state {
		font-size: 10.5px;
		color: var(--text-muted);
		flex-shrink: 0;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
</style>
