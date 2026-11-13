<script lang="ts">
	import { Handle, Position, type Node, type NodeProps } from '@xyflow/svelte';
	import { t } from '$lib/i18n.svelte';

	interface WorkflowNodeData extends Record<string, unknown> {
		canvasNodeId: number;
		/** Workflow name resolved from the workflows list. */
		title: string;
		workflowId: number;
		status: string;
		/** Data-edge roles wired into this node, in edge order. */
		roles: string[];
		/** Disabled while the tracked job is queued/running. */
		busy: boolean;
		onRun?: () => void;
	}

	type WorkflowNodeType = Node<WorkflowNodeData, 'workflow'>;

	let { data }: NodeProps<WorkflowNodeType> = $props();

	const running = $derived(data.status === 'running' || data.status === 'pending');
</script>

<div class={['workflow-node', { running }]}>
	<Handle type="target" position={Position.Left} id="in" />
	<header>
		<span class="type-chip">workflow</span>
		<span class="title" title={data.title}>{data.title}</span>
		{#if running}
			<span class="status running-badge">{t('canvas.running')}</span>
		{:else if data.status === 'failed'}
			<span class="status failed-badge">{t('canvas.failed')}</span>
		{/if}
	</header>
	<div class="body">
		{#if data.roles.length > 0}
			<ul class="roles">
				{#each data.roles as role (role)}
					<li><span class="role-chip">{role}</span></li>
				{/each}
			</ul>
		{:else}
			<span class="empty">{t('canvas.noRefsWired')}</span>
		{/if}
		<button
			type="button"
			class="run-btn"
			disabled={data.busy}
			onclick={() => data.onRun?.()}
		>
			{data.busy ? t('canvas.running') : t('canvas.runWorkflow')}
		</button>
	</div>
</div>

<style>
	.workflow-node {
		width: 210px;
		background: var(--bg-surface);
		border: 1px solid rgba(125, 211, 252, 0.4);
		border-radius: 12px;
		overflow: hidden;
	}
	.workflow-node.running {
		border-color: rgba(125, 211, 252, 0.8);
		box-shadow: 0 0 18px rgba(125, 211, 252, 0.3);
	}
	:global(.svelte-flow__node.selected) .workflow-node {
		border-color: var(--accent);
		box-shadow:
			0 0 0 1px var(--accent),
			0 0 30px var(--accent-glow);
	}
	.workflow-node :global(.svelte-flow__handle) {
		background: rgba(125, 211, 252, 0.65);
		border: none;
		width: 8px;
		height: 8px;
	}
	header {
		display: flex;
		align-items: center;
		gap: 7px;
		padding: 8px 10px;
	}
	.type-chip {
		font-size: 9px;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: #bae6fd;
		background: rgba(12, 74, 110, 0.55);
		border: 1px solid rgba(125, 211, 252, 0.35);
		border-radius: 999px;
		padding: 2px 6px;
		flex-shrink: 0;
	}
	.title {
		flex: 1;
		min-width: 0;
		font-size: 12px;
		font-weight: 500;
		color: var(--text-primary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.status {
		font-size: 9.5px;
		padding: 2px 6px;
		border-radius: 999px;
		flex-shrink: 0;
	}
	.running-badge {
		color: #bae6fd;
		background: rgba(12, 74, 110, 0.6);
	}
	.failed-badge {
		color: #fecaca;
		background: rgba(127, 29, 29, 0.5);
	}
	.body {
		padding: 0 10px 10px;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.roles {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}
	.role-chip {
		font-size: 9.5px;
		color: var(--text-secondary);
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: 999px;
		padding: 1px 7px;
	}
	.empty {
		font-size: 10px;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.run-btn {
		font-size: 11.5px;
		font-weight: 600;
		color: #eaf6ff;
		background: color-mix(in srgb, var(--accent) 26%, transparent);
		border: 1px solid color-mix(in srgb, var(--accent) 55%, transparent);
		border-radius: 8px;
		padding: 5px 0;
		width: 100%;
		cursor: pointer;
	}
	.run-btn:hover:not(:disabled) {
		background: color-mix(in srgb, var(--accent) 38%, transparent);
	}
	.run-btn:disabled {
		opacity: 0.55;
		cursor: default;
	}
</style>
