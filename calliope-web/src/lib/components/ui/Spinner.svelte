<script lang="ts">
	interface Props {
		size?: 'sm' | 'md' | 'lg';
	}

	let { size = 'md' }: Props = $props();
</script>

<span class="spinner spinner-{size}" role="status">
	<span class="sr-only">Loading</span>
</span>

<style>
	.spinner {
		display: inline-block;
		flex-shrink: 0;
		border-radius: 50%;
		border-style: solid;
		border-color: var(--spinner-track, var(--bg-elevated));
		border-top-color: var(--spinner-arc, var(--accent));
		animation: spin 0.7s linear infinite;
	}
	.spinner-sm {
		width: 14px;
		height: 14px;
		border-width: 2px;
	}
	.spinner-md {
		width: 18px;
		height: 18px;
		border-width: 2px;
	}
	.spinner-lg {
		width: 28px;
		height: 28px;
		border-width: 3px;
	}
	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.spinner {
			animation: none;
		}
	}
</style>
