<script lang="ts">
	/**
	 * SceneScriptDrawer — one-line scene meta + expandable script details.
	 * Default closed so the hero player stays dominant.
	 */
	import type { Scene } from '$lib/api';
	import Icon from '$lib/components/ui/Icon.svelte';
	import StatusChip from '$lib/components/ui/StatusChip.svelte';

	interface Props {
		scene: Scene;
		status: string;
		formatClock: (sec: number) => string;
	}

	let { scene, status, formatClock }: Props = $props();

	let open = $state(false);
</script>

<div class="meta-wrap">
	<div class="row">
		<span class="num mono">#{scene.order_index}</span>
		<span class="sid mono">id {scene.id}</span>
		<span class="heading" title={scene.heading || 'Untitled scene'}>
			{scene.heading || 'Untitled scene'}
		</span>
		<StatusChip {status} />
		<span class="dur mono">{formatClock(scene.duration_sec || 5)}</span>
		<button
			type="button"
			class="script-toggle"
			class:open
			aria-expanded={open}
			onclick={() => (open = !open)}
		>
			<Icon name={open ? 'chevron-up' : 'chevron-down'} size={12} />
			Script
		</button>
	</div>

	{#if open}
		<div class="drawer" role="region" aria-label="Scene script">
			<div class="block">
				<span class="k">Heading</span>
				<p>{scene.heading || 'Untitled scene'}</p>
			</div>
			<div class="block">
				<span class="k">Action / script</span>
				{#if scene.action?.trim()}
					<p>{scene.action}</p>
				{:else}
					<p class="muted">No action text for this scene.</p>
				{/if}
			</div>
			<div class="block">
				<span class="k">Dialog</span>
				{#if scene.dialog?.trim()}
					<pre class="dialog">{scene.dialog}</pre>
				{:else}
					<p class="muted">No dialog for this scene.</p>
				{/if}
			</div>
			{#if (scene.characters ?? []).length}
				<div class="block">
					<span class="k">Characters</span>
					<div class="chips">
						{#each scene.characters as c}
							<span class="chip">{c.name}</span>
						{/each}
					</div>
				</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.meta-wrap {
		flex-shrink: 0;
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		background: var(--bg-surface);
		overflow: hidden;
	}

	.row {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 8px 12px;
		min-height: 40px;
	}

	.num {
		font-size: 12px;
		font-weight: 700;
		color: var(--accent);
		flex-shrink: 0;
	}

	.sid {
		font-size: 11px;
		font-weight: 600;
		color: var(--text-muted);
		flex-shrink: 0;
	}

	.heading {
		flex: 1;
		min-width: 0;
		font-size: 13px;
		font-weight: 600;
		color: var(--text-primary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.dur {
		font-size: 12px;
		color: var(--text-muted);
		flex-shrink: 0;
	}

	.script-toggle {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		height: 28px;
		padding: 0 10px;
		border-radius: 9999px;
		border: 1px solid var(--border);
		background: var(--bg-elevated);
		color: var(--text-secondary);
		font-size: 12px;
		font-weight: 600;
		font-family: var(--font-body);
		cursor: pointer;
		flex-shrink: 0;
	}

	.script-toggle:hover,
	.script-toggle.open {
		color: var(--text-primary);
		border-color: var(--text-muted);
	}

	.drawer {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
		gap: 12px 16px;
		padding: 12px 14px 14px;
		border-top: 1px solid var(--border);
		background: var(--bg-primary);
		max-height: 200px;
		overflow-y: auto;
	}

	.block .k {
		display: block;
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--text-muted);
		margin-bottom: 4px;
	}

	.block p {
		margin: 0;
		font-size: 13px;
		line-height: 1.45;
		color: var(--text-secondary);
		white-space: pre-wrap;
	}

	.muted {
		color: var(--text-muted) !important;
	}

	.dialog {
		margin: 0;
		padding: 8px 10px;
		background: var(--bg-elevated);
		border-radius: var(--radius-sm);
		border: 1px solid var(--border);
		font-family: var(--font-mono);
		font-size: 12px;
		line-height: 1.45;
		white-space: pre-wrap;
		color: var(--text-primary);
		max-height: 100px;
		overflow-y: auto;
	}

	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}

	.chip {
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: 999px;
		padding: 2px 10px;
		font-size: 12px;
		color: var(--text-secondary);
	}

	.mono {
		font-family: var(--font-mono);
	}
</style>
