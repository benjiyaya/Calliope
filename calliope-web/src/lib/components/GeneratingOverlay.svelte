<script lang="ts">
	import Spinner from '$lib/components/ui/Spinner.svelte';

	interface Props {
		title: string;
		/** Live status line (e.g. latest agent.thinking message) — hidden when null/empty. */
		status?: string | null;
		/** Small secondary copy under the status line. */
		hint?: string;
	}

	let { title, status = null, hint = '' }: Props = $props();
</script>

<div class="overlay" role="status" aria-live="polite">
	<div class="inner">
		<Spinner size="lg" />
		<p class="title">{title}</p>
		{#if status}
			<p class="status">{status}</p>
		{/if}
		{#if hint}
			<p class="hint">{hint}</p>
		{/if}
	</div>
</div>

<style>
	.overlay {
		position: absolute;
		inset: 0;
		z-index: 30;
		background: rgba(18, 18, 24, 0.78);
		backdrop-filter: blur(2px);
		border-radius: var(--radius-md);
		display: flex;
		align-items: flex-start;
		justify-content: center;
		cursor: wait;
		user-select: none;
		animation: overlay-fade 0.2s ease-out;
	}
	.inner {
		position: sticky;
		top: 28%;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 10px;
		padding: 28px 32px;
		max-width: 460px;
		text-align: center;
	}
	.title {
		margin: 4px 0 0;
		font-family: var(--font-display);
		font-size: 17px;
		font-weight: 650;
		letter-spacing: -0.01em;
		color: var(--text-primary);
	}
	.status {
		margin: 0;
		font-family: var(--font-mono);
		font-size: 12px;
		line-height: 1.5;
		color: var(--text-secondary);
		word-break: break-word;
	}
	.hint {
		margin: 0;
		font-size: 12px;
		line-height: 1.5;
		color: var(--text-muted);
	}
	@keyframes overlay-fade {
		from {
			opacity: 0;
		}
		to {
			opacity: 1;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.overlay {
			animation: none;
		}
	}
</style>
