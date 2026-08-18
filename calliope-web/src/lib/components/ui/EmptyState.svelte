<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		title: string;
		body?: string;
		icon?: Snippet;
		action?: Snippet;
	}

	let { title, body, icon, action }: Props = $props();
</script>

<div class="empty-state">
	{#if icon}
		<div class="empty-icon">
			{@render icon()}
		</div>
	{/if}
	<h3 class="empty-title">{title}</h3>
	{#if body}
		<p class="empty-body">{body}</p>
	{/if}
	{#if action}
		<div class="empty-action">
			{@render action()}
		</div>
	{/if}
</div>

<style>
	.empty-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		padding: 40px 24px;
		text-align: center;
		background: var(--bg-surface);
		border: 1px dashed var(--border);
		border-radius: var(--radius-lg);
	}
	.empty-icon {
		display: flex;
		margin-bottom: 12px;
		color: var(--text-muted);
	}
	.empty-title {
		margin: 0;
		font-family: var(--font-display);
		font-size: 15px;
		font-weight: 600;
		color: var(--text-primary);
	}
	.empty-body {
		margin: 6px 0 0;
		max-width: 420px;
		font-size: 13px;
		line-height: 1.5;
		color: var(--text-secondary);
	}
	.empty-action {
		margin-top: 16px;
	}
</style>
