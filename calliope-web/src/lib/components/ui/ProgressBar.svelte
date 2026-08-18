<script lang="ts">
	interface Props {
		value?: number;
		indeterminate?: boolean;
		label?: string;
		size?: 'sm' | 'md';
	}

	let {
		value = 0,
		indeterminate = false,
		label,
		size = 'md',
	}: Props = $props();

	const pct = $derived(Math.round(Math.min(100, Math.max(0, value))));
	const showMeta = $derived(Boolean(label) || !indeterminate);
</script>

<div class="progress progress-{size}">
	{#if showMeta}
		<div class="progress-meta">
			{#if label}
				<span class="progress-label">{label}</span>
			{/if}
			{#if !indeterminate}
				<span class="progress-value">{pct}%</span>
			{/if}
		</div>
	{/if}
	<div
		class="track"
		role="progressbar"
		aria-valuemin={0}
		aria-valuemax={100}
		aria-valuenow={indeterminate ? undefined : pct}
		aria-label={label ?? 'Progress'}
	>
		{#if indeterminate}
			<div class="fill indeterminate"></div>
		{:else}
			<div class="fill" style:width={pct + '%'}></div>
		{/if}
	</div>
</div>

<style>
	.progress {
		width: 100%;
	}
	.progress-meta {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 12px;
		margin-bottom: 6px;
	}
	.progress-label {
		font-size: 12px;
		color: var(--text-secondary);
	}
	.progress-value {
		margin-left: auto;
		font-family: var(--font-mono);
		font-size: 11px;
		color: var(--text-muted);
	}
	.track {
		position: relative;
		width: 100%;
		overflow: hidden;
		background: var(--bg-elevated);
		border-radius: 999px;
	}
	.progress-sm .track {
		height: 4px;
	}
	.progress-md .track {
		height: 6px;
	}
	.fill {
		height: 100%;
		background: var(--accent);
		border-radius: 999px;
		transition: width 250ms ease;
	}
	.fill.indeterminate {
		position: absolute;
		top: 0;
		left: 0;
		width: 40%;
		background: linear-gradient(
			90deg,
			transparent,
			var(--accent) 40%,
			var(--accent) 60%,
			transparent
		);
		transition: none;
		animation: progress-slide 1.2s ease-in-out infinite;
	}
	@keyframes progress-slide {
		0% {
			left: -40%;
		}
		100% {
			left: 100%;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.fill {
			transition: none;
		}
		.fill.indeterminate {
			animation: none;
			left: 0;
			width: 100%;
			opacity: 0.4;
		}
	}
</style>
