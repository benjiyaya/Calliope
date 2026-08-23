<script lang="ts">
	import Icon from '$lib/components/ui/Icon.svelte';

	interface Props {
		name: string;
		args?: Record<string, unknown> | null;
		result?: unknown;
		phase: 'running' | 'done' | 'error';
	}

	let { name, args = null, result = null, phase }: Props = $props();
	let open = $state(false);

	function fmt(value: unknown): string {
		try {
			return JSON.stringify(value, null, 2) ?? '';
		} catch {
			return String(value);
		}
	}
</script>

<div class="tool-card" class:error={phase === 'error'}>
	<button type="button" class="head" onclick={() => (open = !open)} aria-expanded={open}>
		<span class="dot" class:running={phase === 'running'}></span>
		<span class="name mono">{name}</span>
		{#if phase === 'running'}
			<span class="state">running…</span>
		{:else if phase === 'error'}
			<span class="state err-text">failed</span>
		{:else}
			<span class="state ok-text">done</span>
		{/if}
		<span class="chev"><Icon name="chevron-down" size={13} /></span>
	</button>
	{#if open}
		<div class="body">
			{#if args && Object.keys(args).length > 0}
				<div class="section">
					<span class="label">args</span>
					<pre>{fmt(args)}</pre>
				</div>
			{/if}
			{#if result != null}
				<div class="section">
					<span class="label">result</span>
					<pre>{fmt(result)}</pre>
				</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.tool-card {
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--bg-elevated);
		overflow: hidden;
	}
	.tool-card.error {
		border-color: rgba(239, 68, 68, 0.45);
	}
	.head {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 7px 10px;
		background: transparent;
		border: none;
		cursor: pointer;
		color: var(--text-secondary);
		font-size: 12px;
		text-align: left;
	}
	.head:hover {
		color: var(--text-primary);
		background: rgba(255, 255, 255, 0.03);
	}
	.name {
		flex: 1;
		min-width: 0;
		font-size: 12px;
		color: var(--text-primary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.dot {
		width: 7px;
		height: 7px;
		border-radius: 999px;
		background: var(--text-muted);
		flex-shrink: 0;
	}
	.dot.running {
		background: var(--warning);
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
	.state {
		font-size: 11px;
		color: var(--text-muted);
		margin-left: auto;
		flex-shrink: 0;
	}
	.ok-text {
		color: var(--success);
	}
	.err-text {
		color: var(--error);
	}
	.chev {
		display: inline-flex;
		flex-shrink: 0;
		transition: transform 0.15s;
	}
	.head[aria-expanded='true'] .chev {
		transform: rotate(180deg);
	}
	.body {
		border-top: 1px solid var(--border);
		padding: 8px 10px;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.section {
		display: flex;
		flex-direction: column;
		gap: 4px;
		min-width: 0;
	}
	.label {
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-muted);
	}
	pre {
		margin: 0;
		padding: 8px;
		background: var(--bg-primary);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: 11px;
		line-height: 1.5;
		white-space: pre-wrap;
		word-break: break-word;
		max-height: 220px;
		overflow-y: auto;
	}
</style>
