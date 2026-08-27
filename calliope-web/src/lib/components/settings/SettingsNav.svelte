<script lang="ts">
	import { page } from '$app/stores';

	const items = [
		{ id: 'llm', href: '/settings?tab=llm', label: 'LLM', blurb: 'Model endpoints' },
		{ id: 'comfy', href: '/settings?tab=comfy', label: 'ComfyUI', blurb: 'Render farm' },
		{ id: 'workflows', href: '/settings?tab=workflows', label: 'Workflows', blurb: 'Library' },
		{ id: 'agent', href: '/settings?tab=agent', label: 'Agent', blurb: 'Hardening rules' },
		{ id: 'queue', href: '/settings?tab=queue', label: 'Queue', blurb: 'Concurrency' },
		{ id: 'storage', href: '/settings?tab=storage', label: 'Storage', blurb: 'Paths' },
	] as const;

	interface Props {
		/** Section ids with unsaved edits — shown with a small warning dot. */
		dirty?: ReadonlySet<string>;
	}

	let { dirty }: Props = $props();

	let active = $derived($page.url.searchParams.get('tab') || 'llm');
</script>

<aside class="side">
	<p class="eyebrow">Studio config</p>
	{#each items as item (item.id)}
		<a
			class="item"
			class:active={active === item.id}
			href={item.href}
			aria-current={active === item.id ? 'page' : undefined}
		>
			<span class="label">
				{item.label}
				{#if dirty?.has(item.id)}
					<span class="dirty-dot" title="Unsaved changes" aria-label="Unsaved changes"></span>
				{/if}
			</span>
			<span class="blurb">{item.blurb}</span>
		</a>
	{/each}
</aside>

<style>
	.side {
		width: 220px;
		flex-shrink: 0;
		border-right: 1px solid var(--border);
		background: var(--bg-surface);
		padding: var(--space-lg) var(--space-md);
		display: flex;
		flex-direction: column;
		gap: 4px;
		min-height: calc(100vh - 56px);
	}
	.eyebrow {
		margin: 0 8px 12px;
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
		font-weight: 600;
	}
	.item {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 10px 12px;
		border-radius: var(--radius-md);
		text-decoration: none;
		color: var(--text-secondary);
		border: 1px solid transparent;
		transition: all 0.15s;
	}
	.item:hover {
		background: var(--bg-elevated);
		color: var(--text-primary);
	}
	.item.active {
		background: rgba(139, 92, 246, 0.12);
		border-color: rgba(139, 92, 246, 0.35);
		color: var(--text-primary);
	}
	.item:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.label {
		display: flex;
		align-items: center;
		gap: 6px;
		font-weight: 600;
		font-size: 14px;
		font-family: var(--font-display);
	}
	.dirty-dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: var(--warning);
		flex-shrink: 0;
	}
	.blurb {
		font-size: 11px;
		color: var(--text-muted);
	}
	.item.active .blurb {
		color: var(--text-secondary);
	}
</style>
