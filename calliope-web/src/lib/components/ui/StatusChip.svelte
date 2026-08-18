<script lang="ts">
	interface Props {
		status: string;
		label?: string;
	}

	let { status, label }: Props = $props();

	type Tone = 'success' | 'info' | 'muted' | 'error' | 'warning' | 'accent';

	const toneByStatus: Record<string, Tone> = {
		ready: 'success',
		done: 'success',
		completed: 'success',
		running: 'info',
		generating: 'info',
		queued: 'info',
		pending: 'muted',
		idle: 'muted',
		draft: 'muted',
		failed: 'error',
		error: 'error',
		paused: 'warning',
		in_progress: 'accent',
	};

	const key = $derived(status.trim().toLowerCase());
	const tone: Tone = $derived(toneByStatus[key] ?? 'muted');
	const pulsing = $derived(key === 'running');
</script>

<span class="chip chip-{tone}">
	<span class="dot" class:pulsing></span>
	<span class="chip-label">{label ?? status}</span>
</span>

<style>
	.chip {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 3px 9px;
		border: 1px solid;
		border-radius: 999px;
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		line-height: 1.4;
		white-space: nowrap;
	}
	.dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: currentColor;
		flex-shrink: 0;
	}
	.chip-success {
		color: var(--success);
		background: color-mix(in srgb, var(--success) 12%, transparent);
		border-color: color-mix(in srgb, var(--success) 35%, transparent);
	}
	.chip-info {
		color: var(--info);
		background: color-mix(in srgb, var(--info) 12%, transparent);
		border-color: color-mix(in srgb, var(--info) 35%, transparent);
	}
	.chip-error {
		color: var(--error);
		background: color-mix(in srgb, var(--error) 12%, transparent);
		border-color: color-mix(in srgb, var(--error) 35%, transparent);
	}
	.chip-warning {
		color: var(--warning);
		background: color-mix(in srgb, var(--warning) 12%, transparent);
		border-color: color-mix(in srgb, var(--warning) 35%, transparent);
	}
	.chip-accent {
		color: var(--accent);
		background: color-mix(in srgb, var(--accent) 12%, transparent);
		border-color: color-mix(in srgb, var(--accent) 35%, transparent);
	}
	.chip-muted {
		color: var(--text-secondary);
		background: rgba(255, 255, 255, 0.04);
		border-color: var(--border);
	}
	.dot.pulsing {
		animation: chip-pulse 1.2s ease-in-out infinite;
	}
	@keyframes chip-pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.35;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.dot.pulsing {
			animation: none;
		}
	}
</style>
