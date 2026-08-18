<script lang="ts">
	/**
	 * PillStepper — a pill with − / + buttons for numeric values.
	 *
	 * Used for duration (3–10s) and seed in the control bar.
	 * The value display sits between the two buttons, all inside one pill.
	 */
	import Icon from '$lib/components/ui/Icon.svelte';

	interface Props {
		label: string;
		value: number | string;
		min?: number;
		max?: number;
		step?: number;
		/** If true, value is hidden behind a dice/randomize icon (for seed). */
		obscured?: boolean;
		onchange: (value: number) => void;
	}

	let {
		label,
		value,
		min = 0,
		max = 999999,
		step = 1,
		obscured = false,
		onchange,
	}: Props = $props();

	function clamp(n: number): number {
		return Math.min(max, Math.max(min, n));
	}

	function decrement() {
		const current = Number(value) || min;
		onchange(clamp(current - step));
	}

	function increment() {
		const current = Number(value) || min;
		onchange(clamp(current + step));
	}
</script>

<div class="stepper-pill" title={label}>
	<button type="button" class="step-btn" onclick={decrement} aria-label="Decrease {label}">
		<Icon name="close" size={12} />
	</button>
	<span class="step-value">
		{#if obscured && !value}
			<Icon name="sparkle" size={12} />
		{:else}
			{value}{label === 'Duration' ? 's' : ''}
		{/if}
	</span>
	<button type="button" class="step-btn" onclick={increment} aria-label="Increase {label}">
		<Icon name="plus" size={12} />
	</button>
</div>

<style>
	.stepper-pill {
		display: inline-flex;
		align-items: center;
		gap: 0;
		height: var(--pill-height, 36px);
		border-radius: var(--pill-radius, 9999px);
		background: var(--pill-bg, var(--bg-elevated));
		border: 1px solid var(--pill-border, var(--border));
		overflow: hidden;
	}

	.step-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 100%;
		background: transparent;
		border: none;
		color: var(--text-muted);
		cursor: pointer;
		transition: color 0.15s, background 0.15s;
	}

	.step-btn:hover {
		color: var(--text-primary);
		background: rgba(255, 255, 255, 0.05);
	}

	.step-btn:focus-visible {
		outline: none;
		color: var(--accent);
	}

	.step-value {
		min-width: 36px;
		text-align: center;
		font-size: 13px;
		font-weight: 500;
		color: var(--text-primary);
		font-family: var(--font-mono);
		padding: 0 2px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 2px;
	}
</style>
