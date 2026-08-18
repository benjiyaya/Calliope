<script lang="ts">
	interface Props {
		active?: 'projects' | 'playground' | 'settings';
		crumb?: string;
		/** Optional status content rendered between the crumb and the nav. */
		status?: import('svelte').Snippet;
		children?: import('svelte').Snippet;
	}

	let { active, crumb, status, children }: Props = $props();
</script>

<header class="header">
	<div class="header-left">
		<a class="logo" href="/" title="Projects">
			Calliope <span>Lab</span>
		</a>
		{#if crumb}
			<span class="crumb">{crumb}</span>
		{/if}
		{#if status}
			<div class="header-status">
				{@render status()}
			</div>
		{/if}
		<nav class="top-nav" aria-label="Primary">
			<a class="nav-link" class:active={active === 'projects'} href="/">Projects</a>
			<a class="nav-link" class:active={active === 'playground'} href="/playground">Playground</a>
			<a class="nav-link" class:active={active === 'settings'} href="/settings">Settings</a>
		</nav>
	</div>
	<div class="header-right">
		{#if children}
			{@render children()}
		{/if}
	</div>
</header>

<style>
	.header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-md);
		padding: 14px 24px;
		border-bottom: 1px solid var(--border);
		background: var(--bg-surface);
		flex-shrink: 0;
	}
	.header-left {
		display: flex;
		align-items: center;
		gap: var(--space-md);
		min-width: 0;
		flex-wrap: wrap;
	}
	.header-right {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-shrink: 0;
	}
	.logo {
		font-family: var(--font-display);
		font-size: 18px;
		font-weight: 700;
		color: var(--text-primary);
		text-decoration: none;
		letter-spacing: -0.02em;
		white-space: nowrap;
	}
	.logo span {
		color: var(--accent);
		font-weight: 500;
	}
	.crumb {
		color: var(--text-muted);
		font-size: 13px;
	}
	.header-status {
		display: flex;
		align-items: center;
		flex-shrink: 0;
	}
	.top-nav {
		display: flex;
		align-items: center;
		gap: 4px;
		margin-left: 8px;
	}
	.nav-link {
		padding: 8px 12px;
		border-radius: var(--radius-sm);
		color: var(--text-secondary);
		text-decoration: none;
		font-size: 13px;
		font-weight: 500;
		transition: color 0.15s, background 0.15s;
		min-height: 36px;
		display: inline-flex;
		align-items: center;
		cursor: pointer;
	}
	.nav-link:hover {
		color: var(--text-primary);
		background: var(--bg-elevated);
	}
	.nav-link.active {
		color: var(--text-primary);
		background: var(--bg-elevated);
		box-shadow: inset 0 -2px 0 var(--accent);
	}
	.nav-link:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	@media (max-width: 640px) {
		.header {
			padding: 12px 16px;
			flex-direction: column;
			align-items: stretch;
		}
		.top-nav {
			margin-left: 0;
			width: 100%;
		}
		.nav-link {
			flex: 1;
			justify-content: center;
		}
	}
</style>
