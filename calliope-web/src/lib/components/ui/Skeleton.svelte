<script lang="ts">
	interface Props {
		width?: string;
		height?: string;
		circle?: boolean;
	}

	let { width = '100%', height = '14px', circle = false }: Props = $props();

	const resolvedWidth = $derived(circle && width === '100%' ? height : width);
</script>

<div
	class="skeleton"
	class:circle
	style:width={resolvedWidth}
	style:height
	aria-hidden="true"
></div>

<style>
	.skeleton {
		position: relative;
		overflow: hidden;
		background: var(--bg-elevated);
		border-radius: var(--radius-sm);
	}
	.skeleton.circle {
		border-radius: 50%;
	}
	.skeleton::after {
		content: '';
		position: absolute;
		inset: 0;
		background: linear-gradient(
			90deg,
			transparent,
			rgba(255, 255, 255, 0.06),
			transparent
		);
		transform: translateX(-100%);
		animation: skeleton-shimmer 1.6s ease-in-out infinite;
	}
	@keyframes skeleton-shimmer {
		to {
			transform: translateX(100%);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.skeleton::after {
			display: none;
		}
	}
</style>
