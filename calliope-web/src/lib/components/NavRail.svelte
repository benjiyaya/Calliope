<script lang="ts">
	import { goto } from '$app/navigation';
	import Icon from '$lib/components/ui/Icon.svelte';
	import type { IconName } from '$lib/components/ui/icons';

	const items: { id: string; label: string; icon: IconName }[] = [
		{ id: 'story', label: 'Story', icon: 'story' },
		{ id: 'assets', label: 'Assets', icon: 'assets' },
		{ id: 'script', label: 'Script', icon: 'script' },
		{ id: 'video', label: 'Video', icon: 'video' },
	];

	interface Props {
		active?: string;
		onSelect?: (id: string) => void;
		/** Optional per-stage counts — a nonzero value shows a small pill on the icon. */
		badges?: Record<string, number>;
	}

	let { active = 'story', onSelect, badges = {} }: Props = $props();
</script>

<nav class="nav" aria-label="Project stages">
	<button
		class="nav-item home"
		title="Projects"
		aria-label="Back to projects"
		onclick={() => goto('/')}
	>
		<span class="icon"><Icon name="home" size={20} /></span>
		<span class="label">Home</span>
	</button>
	<div class="divider" aria-hidden="true"></div>
	{#each items as item (item.id)}
		{@const count = badges[item.id] ?? 0}
		<button
			class="nav-item"
			class:active={active === item.id}
			title={item.label}
			aria-current={active === item.id ? 'page' : undefined}
			onclick={() => onSelect?.(item.id)}
		>
			<span class="icon">
				<Icon name={item.icon} size={20} />
				{#if count > 0}
					<span class="badge" aria-label="{count} active">
						{count > 9 ? '9+' : count}
					</span>
				{/if}
			</span>
			<span class="label">{item.label}</span>
		</button>
	{/each}
</nav>

<style>
	.nav {
		width: 64px;
		background: var(--bg-surface);
		border-right: 1px solid var(--border);
		display: flex;
		flex-direction: column;
		align-items: center;
		padding: var(--space-md) 0;
		gap: var(--space-sm);
		flex-shrink: 0;
	}
	.divider {
		width: 32px;
		height: 1px;
		background: var(--border);
		margin: 2px 0 4px;
	}
	.nav-item {
		width: 56px;
		border: none;
		background: transparent;
		border-radius: var(--radius-md);
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 4px;
		padding: 8px 0;
		cursor: pointer;
		color: var(--text-muted);
		font-size: 11px;
		font-family: inherit;
		transition: all 0.15s;
	}
	.nav-item:hover {
		background: var(--bg-elevated);
		color: var(--text-primary);
	}
	.nav-item.active {
		background: var(--accent);
		color: white;
		box-shadow: 0 0 16px var(--accent-glow);
	}
	.nav-item.home:hover {
		color: var(--accent);
	}
	.nav-item:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.icon {
		position: relative;
		display: inline-flex;
		line-height: 1;
	}
	.badge {
		position: absolute;
		top: -6px;
		right: -12px;
		min-width: 15px;
		height: 15px;
		padding: 0 4px;
		box-sizing: border-box;
		border-radius: 999px;
		background: var(--accent);
		color: #fff;
		font-size: 9px;
		font-weight: 700;
		line-height: 15px;
		text-align: center;
		box-shadow: 0 0 0 2px var(--bg-surface);
	}
	.nav-item.active .badge {
		background: #fff;
		color: var(--accent);
		box-shadow: 0 0 0 2px var(--accent);
	}
</style>
