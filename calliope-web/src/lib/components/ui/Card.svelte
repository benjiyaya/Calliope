<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		hoverable?: boolean;
		title?: string;
		actions?: Snippet;
		header?: Snippet;
		children: Snippet;
		class?: string;
	}

	let {
		hoverable = false,
		title,
		actions,
		header,
		children,
		class: klass = '',
	}: Props = $props();
</script>

<div class="card {klass}" class:hoverable>
	{#if header}
		<div class="card-header">
			{@render header()}
		</div>
	{:else if title || actions}
		<div class="card-header">
			{#if title}
				<h3 class="card-title">{title}</h3>
			{/if}
			{#if actions}
				<div class="card-actions">
					{@render actions()}
				</div>
			{/if}
		</div>
	{/if}
	<div class="card-body">
		{@render children()}
	</div>
</div>

<style>
	.card {
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
	}
	.card.hoverable {
		transition:
			border-color 150ms ease,
			transform 150ms ease;
	}
	.card.hoverable:hover {
		border-color: #3f3f46;
		transform: translateY(-1px);
	}
	.card-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 14px 16px;
		border-bottom: 1px solid var(--border);
	}
	.card-title {
		margin: 0;
		font-family: var(--font-display);
		font-size: 14px;
		font-weight: 600;
		color: var(--text-primary);
	}
	.card-actions {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-left: auto;
	}
	.card-body {
		padding: 16px;
	}
</style>
