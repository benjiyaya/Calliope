<script lang="ts">
	import type { Snippet } from 'svelte';
	import Spinner from './Spinner.svelte';

	interface Props {
		variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
		size?: 'sm' | 'md';
		disabled?: boolean;
		loading?: boolean;
		type?: 'button' | 'submit' | 'reset';
		title?: string;
		onclick?: (event: MouseEvent) => void;
		children: Snippet;
	}

	let {
		variant = 'secondary',
		size = 'md',
		disabled = false,
		loading = false,
		type = 'button',
		title,
		onclick,
		children,
	}: Props = $props();
</script>

<button
	{type}
	class="btn btn-{variant} btn-{size}"
	class:loading
	disabled={disabled || loading}
	{title}
	{onclick}
>
	<span class="btn-content" aria-hidden={loading || undefined}
		>{@render children()}</span
	>
	{#if loading}
		<span class="btn-spinner">
			<Spinner size="sm" />
		</span>
	{/if}
</button>

<style>
	.btn {
		position: relative;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		border: 1px solid transparent;
		border-radius: var(--radius-sm);
		font-family: inherit;
		font-weight: 500;
		white-space: nowrap;
		cursor: pointer;
		transition:
			background-color 150ms ease,
			border-color 150ms ease,
			color 150ms ease,
			box-shadow 150ms ease,
			transform 100ms ease;
	}
	.btn-sm {
		min-height: 28px;
		padding: 5px 10px;
		font-size: 12px;
	}
	.btn-md {
		min-height: 34px;
		padding: 8px 14px;
		font-size: 13px;
	}

	.btn-primary {
		background: var(--accent);
		color: #fff;
	}
	.btn-primary:hover:not(:disabled) {
		background: var(--accent-hover);
	}

	.btn-secondary {
		background: var(--bg-elevated);
		border-color: var(--border);
		color: var(--text-primary);
	}
	.btn-secondary:hover:not(:disabled) {
		background: #23232b;
		border-color: #3f3f46;
	}

	.btn-ghost {
		background: transparent;
		color: var(--text-secondary);
	}
	.btn-ghost:hover:not(:disabled) {
		background: rgba(255, 255, 255, 0.05);
		color: var(--text-primary);
	}

	.btn-danger {
		background: rgba(239, 68, 68, 0.1);
		border-color: rgba(239, 68, 68, 0.3);
		color: var(--error);
	}
	.btn-danger:hover:not(:disabled) {
		background: rgba(239, 68, 68, 0.18);
		border-color: rgba(239, 68, 68, 0.45);
	}

	.btn:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.btn:active:not(:disabled) {
		transform: scale(0.98);
	}
	.btn:disabled {
		opacity: 0.45;
		cursor: not-allowed;
		pointer-events: none;
	}

	.btn-content {
		display: inline-flex;
		align-items: center;
		gap: inherit;
	}
	.btn.loading .btn-content {
		visibility: hidden;
	}
	.btn-spinner {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
	}
	.btn-primary .btn-spinner,
	.btn-danger .btn-spinner {
		--spinner-track: rgba(255, 255, 255, 0.35);
		--spinner-arc: #fff;
	}
</style>
